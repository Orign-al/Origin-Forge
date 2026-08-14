from __future__ import annotations

import base64
import binascii
import json
import logging
import socket
import struct
import threading
import time
import uuid
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from h100_portal_api.audit import record_audit
from h100_portal_api.config import get_settings
from h100_portal_api.database import SessionLocal
from h100_portal_api.enums import OperationStatus
from h100_portal_api.models import PortalOperation, utcnow

LOG = logging.getLogger("h100-portal-terminal")
MAX_FRAME = 64 * 1024
OUTPUT_BUFFER_BYTES = 1024 * 1024
OUTPUT_RESPONSE_BYTES = 256 * 1024
OUTPUT_LONG_POLL_SECONDS = 12.0
MAX_TERMINALS_PER_MANAGED_USER = 2
MAX_TERMINALS_GLOBAL = 8
ENDED_SESSION_RETENTION_SECONDS = 5 * 60


class TerminalServiceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _read_exact(connection: socket.socket, length: int) -> bytes:
    pieces: list[bytes] = []
    remaining = length
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise TerminalServiceError("TERMINAL_WORKER_EOF", "终端 Worker 已断开")
        pieces.append(chunk)
        remaining -= len(chunk)
    return b"".join(pieces)


def _receive_frame(connection: socket.socket) -> dict[str, Any]:
    length = struct.unpack("!I", _read_exact(connection, 4))[0]
    if length <= 0 or length > MAX_FRAME:
        raise TerminalServiceError("TERMINAL_PROTOCOL_ERROR", "终端响应长度无效")
    try:
        value = json.loads(_read_exact(connection, length))
    except json.JSONDecodeError as exc:
        raise TerminalServiceError("TERMINAL_PROTOCOL_ERROR", "终端响应格式无效") from exc
    if not isinstance(value, dict):
        raise TerminalServiceError("TERMINAL_PROTOCOL_ERROR", "终端响应格式无效")
    return value


def _send_frame(connection: socket.socket, value: dict[str, Any]) -> None:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if not encoded or len(encoded) > MAX_FRAME:
        raise TerminalServiceError("TERMINAL_PROTOCOL_ERROR", "终端请求长度无效")
    try:
        connection.sendall(struct.pack("!I", len(encoded)) + encoded)
    except OSError as exc:
        raise TerminalServiceError("TERMINAL_WORKER_EOF", "终端 Worker 已断开") from exc


class WorkerTerminal:
    def __init__(self, connection: socket.socket, ready: dict[str, Any]) -> None:
        self.ready = ready
        self._connection = connection
        self._send_lock = threading.Lock()
        self._condition = threading.Condition()
        self._chunks: deque[tuple[int, bytes]] = deque()
        self._buffer_bytes = 0
        self._buffer_start = 0
        self._cursor = 0
        self.state = "RUNNING"
        self.reason: str | None = None
        self.exit_code: int | None = None
        self.ended_monotonic: float | None = None
        self._reader = threading.Thread(
            target=self._read_loop,
            name=f"terminal-output-{str(ready.get('request_id', 'unknown'))[:8]}",
            daemon=True,
        )
        self._reader.start()

    @classmethod
    def open(
        cls,
        *,
        payload: dict[str, Any],
        requested_by: str,
        idempotency_key: str,
    ) -> WorkerTerminal:
        request_id = str(uuid.uuid4())
        envelope = {
            "protocol_version": 1,
            "request_id": request_id,
            "operation_type": "self.container.terminal",
            "payload": payload,
            "requested_by": requested_by,
            "approved_by": requested_by,
            "idempotency_key": idempotency_key,
            "dry_run": False,
        }
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(15.0)
        try:
            connection.connect(get_settings().worker_socket)
            _send_frame(connection, envelope)
            ready = _receive_frame(connection)
            if ready.get("status") != "READY" or ready.get("type") != "ready":
                error = ready.get("error", {})
                if not isinstance(error, dict):
                    error = {}
                raise TerminalServiceError(
                    str(error.get("code", "TERMINAL_START_FAILED")),
                    str(error.get("message", "开发容器终端启动失败")),
                )
            if (
                ready.get("request_id") != request_id
                or ready.get("handler") != "self.container.terminal"
                or ready.get("container") != payload["name"]
                or ready.get("username") != payload["username"]
                or ready.get("uid") != payload["uid"]
                or ready.get("gid") != payload["gid"]
                or ready.get("gpu") != "NONE"
                or ready.get("host_access") != "DISABLED"
            ):
                raise TerminalServiceError(
                    "TERMINAL_BINDING_REJECTED", "终端 Worker 身份绑定验证失败"
                )
            connection.settimeout(None)
            return cls(connection, ready)
        except TerminalServiceError:
            connection.close()
            raise
        except OSError as exc:
            connection.close()
            raise TerminalServiceError(
                "TERMINAL_WORKER_UNAVAILABLE", "受控终端 Worker 暂不可用"
            ) from exc

    def _finish(self, state: str, reason: str, exit_code: int | None = None) -> None:
        with self._condition:
            if self.state in {"CLOSED", "ERROR"}:
                return
            self.state = state
            self.reason = reason[:64]
            self.exit_code = exit_code
            self.ended_monotonic = time.monotonic()
            self._condition.notify_all()
        with suppress(OSError):
            self._connection.close()

    def _append_output(self, output: bytes) -> None:
        with self._condition:
            start = self._cursor
            self._chunks.append((start, output))
            self._cursor += len(output)
            self._buffer_bytes += len(output)
            while self._buffer_bytes > OUTPUT_BUFFER_BYTES and self._chunks:
                old_start, old = self._chunks.popleft()
                self._buffer_bytes -= len(old)
                self._buffer_start = old_start + len(old)
            self._condition.notify_all()

    def _read_loop(self) -> None:
        try:
            while True:
                frame = _receive_frame(self._connection)
                frame_type = frame.get("type")
                if frame_type == "output" and set(frame) == {"type", "data_b64"}:
                    encoded = frame.get("data_b64")
                    if not isinstance(encoded, str) or len(encoded) > 24 * 1024:
                        raise TerminalServiceError("TERMINAL_PROTOCOL_ERROR", "终端输出帧无效")
                    try:
                        output = base64.b64decode(encoded, validate=True)
                    except (binascii.Error, ValueError) as exc:
                        raise TerminalServiceError(
                            "TERMINAL_PROTOCOL_ERROR", "终端输出帧无效"
                        ) from exc
                    if not output or len(output) > 16 * 1024:
                        raise TerminalServiceError("TERMINAL_PROTOCOL_ERROR", "终端输出帧无效")
                    self._append_output(output)
                    continue
                if frame_type == "exit" and set(frame) == {"type", "reason", "exit_code"}:
                    raw_exit = frame.get("exit_code")
                    exit_code = raw_exit if isinstance(raw_exit, int) else None
                    reason = str(frame.get("reason", "PROCESS_EXITED"))
                    self._finish("CLOSED", reason, exit_code)
                    return
                if frame_type == "error":
                    self._finish("ERROR", str(frame.get("code", "TERMINAL_WORKER_ERROR")))
                    return
                raise TerminalServiceError("TERMINAL_PROTOCOL_ERROR", "终端输出协议无效")
        except TerminalServiceError as exc:
            state = "CLOSED" if self.state == "CLOSING" else "ERROR"
            reason = "CLIENT_CLOSED" if state == "CLOSED" else exc.code
            self._finish(state, reason)
        except OSError:
            state = "CLOSED" if self.state == "CLOSING" else "ERROR"
            reason = "CLIENT_CLOSED" if state == "CLOSED" else "TERMINAL_WORKER_EOF"
            self._finish(state, reason)

    def send_input(self, data: bytes) -> None:
        if not data or len(data) > 8 * 1024:
            raise TerminalServiceError("TERMINAL_INPUT_REJECTED", "终端输入长度无效")
        with self._condition:
            if self.state != "RUNNING":
                raise TerminalServiceError("TERMINAL_NOT_RUNNING", "终端会话已结束")
        with self._send_lock:
            _send_frame(
                self._connection,
                {"type": "input", "data_b64": base64.b64encode(data).decode("ascii")},
            )

    def resize(self, cols: int, rows: int) -> None:
        with self._condition:
            if self.state != "RUNNING":
                raise TerminalServiceError("TERMINAL_NOT_RUNNING", "终端会话已结束")
        with self._send_lock:
            _send_frame(self._connection, {"type": "resize", "cols": cols, "rows": rows})

    def request_close(self) -> None:
        with self._condition:
            if self.state in {"CLOSED", "ERROR", "CLOSING"}:
                return
            self.state = "CLOSING"
            self._condition.notify_all()
        try:
            with self._send_lock:
                _send_frame(self._connection, {"type": "close"})
        except TerminalServiceError:
            self._finish("CLOSED", "CLIENT_CLOSED")

    def output(self, cursor: int) -> dict[str, Any]:
        with self._condition:
            if cursor < self._buffer_start or cursor > self._cursor:
                raise TerminalServiceError(
                    "TERMINAL_OUTPUT_CURSOR_EXPIRED", "终端输出游标已失效，请重新打开终端"
                )
            deadline = time.monotonic() + OUTPUT_LONG_POLL_SECONDS
            while cursor == self._cursor and self.state in {"RUNNING", "CLOSING"}:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            pieces: list[bytes] = []
            size = 0
            next_cursor = cursor
            for start, chunk in self._chunks:
                end = start + len(chunk)
                if end <= cursor:
                    continue
                offset = max(0, cursor - start)
                available = chunk[offset:]
                take = min(len(available), OUTPUT_RESPONSE_BYTES - size)
                if take <= 0:
                    break
                pieces.append(available[:take])
                size += take
                next_cursor += take
                if size >= OUTPUT_RESPONSE_BYTES:
                    break
            output = b"".join(pieces)
            return {
                "data_b64": base64.b64encode(output).decode("ascii"),
                "cursor": next_cursor,
                "state": self.state,
                "reason": self.reason,
                "exit_code": self.exit_code,
            }

    def wait_closed(self) -> None:
        with self._condition:
            while self.state not in {"CLOSED", "ERROR"}:
                self._condition.wait(2.0)


@dataclass
class TerminalRecord:
    id: uuid.UUID
    owner_managed_user_id: uuid.UUID
    portal_user_id: uuid.UUID
    portal_session_id: uuid.UUID
    operation_id: uuid.UUID
    container_id: uuid.UUID
    container_name: str
    actor: str
    actor_role: str
    source_ip: str
    user_agent: str
    created_at: datetime
    expires_at: datetime
    worker: WorkerTerminal
    finalized: bool = False


class TerminalRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[uuid.UUID, TerminalRecord] = {}
        self._opening_by_owner: dict[uuid.UUID, int] = {}
        self._opening_total = 0

    def _prune_locked(self) -> None:
        now = time.monotonic()
        stale = [
            key
            for key, record in self._records.items()
            if record.worker.ended_monotonic is not None
            and now - record.worker.ended_monotonic > ENDED_SESSION_RETENTION_SECONDS
        ]
        for key in stale:
            self._records.pop(key, None)

    def open(
        self,
        *,
        payload: dict[str, Any],
        owner_managed_user_id: uuid.UUID,
        portal_user_id: uuid.UUID,
        portal_session_id: uuid.UUID,
        operation_id: uuid.UUID,
        container_id: uuid.UUID,
        container_name: str,
        actor: str,
        actor_role: str,
        source_ip: str,
        user_agent: str,
        idempotency_key: str,
    ) -> TerminalRecord:
        with self._lock:
            self._prune_locked()
            live_for_owner = sum(
                1
                for record in self._records.values()
                if record.owner_managed_user_id == owner_managed_user_id
                and record.worker.state in {"RUNNING", "CLOSING"}
            ) + self._opening_by_owner.get(owner_managed_user_id, 0)
            live_global = (
                sum(
                    1
                    for record in self._records.values()
                    if record.worker.state in {"RUNNING", "CLOSING"}
                )
                + self._opening_total
            )
            if live_for_owner >= MAX_TERMINALS_PER_MANAGED_USER:
                raise TerminalServiceError(
                    "TERMINAL_LIMIT_REACHED", "每个用户最多同时打开2个网页终端"
                )
            if live_global >= MAX_TERMINALS_GLOBAL:
                raise TerminalServiceError("TERMINAL_LIMIT_REACHED", "网页终端容量已满")
            self._opening_by_owner[owner_managed_user_id] = (
                self._opening_by_owner.get(owner_managed_user_id, 0) + 1
            )
            self._opening_total += 1
        try:
            worker = WorkerTerminal.open(
                payload=payload,
                requested_by=actor,
                idempotency_key=idempotency_key,
            )
        finally:
            with self._lock:
                remaining = self._opening_by_owner.get(owner_managed_user_id, 1) - 1
                if remaining:
                    self._opening_by_owner[owner_managed_user_id] = remaining
                else:
                    self._opening_by_owner.pop(owner_managed_user_id, None)
                self._opening_total -= 1
        max_duration = int(worker.ready.get("max_duration_seconds", 3600))
        lease_expiry = datetime.fromisoformat(str(payload["lease_expires_at"])).astimezone(UTC)
        now = utcnow()
        record = TerminalRecord(
            id=uuid.uuid4(),
            owner_managed_user_id=owner_managed_user_id,
            portal_user_id=portal_user_id,
            portal_session_id=portal_session_id,
            operation_id=operation_id,
            container_id=container_id,
            container_name=container_name,
            actor=actor,
            actor_role=actor_role,
            source_ip=source_ip,
            user_agent=user_agent,
            created_at=now,
            expires_at=min(lease_expiry, now + timedelta(seconds=max_duration)),
            worker=worker,
        )
        with self._lock:
            self._records[record.id] = record
        threading.Thread(
            target=self._watch,
            args=(record,),
            name=f"terminal-watch-{str(record.id)[:8]}",
            daemon=True,
        ).start()
        return record

    def _watch(self, record: TerminalRecord) -> None:
        record.worker.wait_closed()
        self._finalize(record)

    def _finalize(self, record: TerminalRecord) -> None:
        with self._lock:
            if record.finalized:
                return
            record.finalized = True
        try:
            with SessionLocal() as db:
                operation = db.get(PortalOperation, record.operation_id)
                if operation is not None and operation.status == OperationStatus.RUNNING:
                    failed = record.worker.state == "ERROR"
                    operation.status = (
                        OperationStatus.FAILED if failed else OperationStatus.SUCCEEDED
                    )
                    operation.finished_at = utcnow()
                    operation.result_summary = (
                        f"Container web terminal closed: {record.worker.reason or 'UNKNOWN'}"
                    )
                    operation.error_code = (
                        (record.worker.reason or "TERMINAL_ERROR")[:64] if failed else None
                    )
                    record_audit(
                        db,
                        event_type="SELF_CONTAINER_TERMINAL_CLOSED",
                        actor=record.actor,
                        actor_role=record.actor_role,
                        source_ip=record.source_ip,
                        user_agent=record.user_agent,
                        object_type="container",
                        object_id=str(record.container_id),
                        operation_id=record.operation_id,
                        result="FAILED" if failed else "SUCCESS",
                        metadata={
                            "terminal_session_id": str(record.id),
                            "reason": record.worker.reason or "UNKNOWN",
                            "exit_code": record.worker.exit_code,
                            "container": record.container_name,
                            "gpu": "NONE",
                            "host_access": "DISABLED",
                            "input_logged": False,
                        },
                    )
                    db.commit()
        except Exception:
            LOG.exception("terminal audit finalization failed")

    def owned(
        self,
        terminal_id: uuid.UUID,
        *,
        owner_managed_user_id: uuid.UUID,
        portal_session_id: uuid.UUID,
    ) -> TerminalRecord:
        with self._lock:
            self._prune_locked()
            record = self._records.get(terminal_id)
            if (
                record is None
                or record.owner_managed_user_id != owner_managed_user_id
                or record.portal_session_id != portal_session_id
            ):
                raise TerminalServiceError("TERMINAL_NOT_FOUND", "网页终端不存在")
            return record

    def close_for_portal_session(self, portal_session_id: uuid.UUID) -> None:
        with self._lock:
            records = [
                record
                for record in self._records.values()
                if record.portal_session_id == portal_session_id
            ]
        for record in records:
            record.worker.request_close()

    def close_for_user(
        self, portal_user_id: uuid.UUID, *, except_session_id: uuid.UUID | None = None
    ) -> None:
        with self._lock:
            records = [
                record
                for record in self._records.values()
                if record.portal_user_id == portal_user_id
                and record.portal_session_id != except_session_id
            ]
        for record in records:
            record.worker.request_close()


terminal_registry = TerminalRegistry()
