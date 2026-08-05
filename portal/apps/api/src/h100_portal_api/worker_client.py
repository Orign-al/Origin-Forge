import json
import secrets
import socket
import struct
import uuid
from datetime import UTC, datetime
from typing import Any

from h100_portal_api.config import get_settings

MAX_FRAME = 64 * 1024


class WorkerClientError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _recv_exact(connection: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise WorkerClientError("WORKER_EOF", "Root Worker closed the connection")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def call_worker(
    operation_type: str,
    *,
    payload: dict[str, Any] | None = None,
    requested_by: str = "portal",
    approved_by: str | None = None,
    idempotency_key: str | None = None,
    dry_run: bool = False,
    timeout_seconds: float = 25.0,
) -> dict[str, Any]:
    request_id = str(uuid.uuid4())
    envelope = {
        "protocol_version": 1,
        "request_id": request_id,
        "operation_type": operation_type,
        "payload": payload or {},
        "requested_by": requested_by,
        "approved_by": approved_by,
        "idempotency_key": idempotency_key or secrets.token_urlsafe(18),
        "dry_run": dry_run,
    }
    encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_FRAME:
        raise WorkerClientError("WORKER_REQUEST_TOO_LARGE", "Worker 请求超过大小限制")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout_seconds)
            connection.connect(get_settings().worker_socket)
            connection.sendall(struct.pack("!I", len(encoded)) + encoded)
            length = struct.unpack("!I", _recv_exact(connection, 4))[0]
            if length <= 0 or length > MAX_FRAME:
                raise WorkerClientError("WORKER_RESPONSE_TOO_LARGE", "Worker 响应长度无效")
            response = json.loads(_recv_exact(connection, length))
    except WorkerClientError:
        raise
    except (OSError, TimeoutError, json.JSONDecodeError) as exc:
        raise WorkerClientError("WORKER_UNAVAILABLE", "受控 Root Worker 暂不可用") from exc
    if not isinstance(response, dict):
        raise WorkerClientError("WORKER_PROTOCOL_ERROR", "Worker 响应格式无效")
    response.setdefault("captured_at", datetime.now(UTC).isoformat())
    return response
