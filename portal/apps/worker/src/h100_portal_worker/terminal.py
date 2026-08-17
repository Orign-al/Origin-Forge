import base64
import binascii
import errno
import fcntl
import json
import os
import pty
import queue
import re
import select
import signal
import socket
import struct
import subprocess
import termios
import threading
import time
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from h100_portal_worker.handlers import (
    BINARIES,
    FIXED_ENV,
    PILOT_DATA_ROOT,
    LifecycleValidationError,
    _installed_key_fingerprints,
    _managed_account,
    _managed_container_security,
)
from h100_portal_worker.protocol import MAX_FRAME, encode_frame
from h100_portal_worker.schemas import WorkerRequest, validate_payload

TERMINAL_IDLE_TIMEOUT_SECONDS = 15 * 60
TERMINAL_MAX_DURATION_SECONDS = 60 * 60
TERMINAL_INPUT_MAX_BYTES = 8 * 1024
TERMINAL_OUTPUT_CHUNK_BYTES = 16 * 1024
TERMINAL_CLOSE_GRACE_SECONDS = 2.0
TERMINAL_PREFLIGHT_INTERVAL_SECONDS = 30.0
DOCKER_DETACH_BYTE = b"\x1d"
TERMINAL_PROCESS_PREFIX = "h100-portal-terminal-"
TERMINAL_PROCESS_PATTERN = re.compile(
    rf"{TERMINAL_PROCESS_PREFIX}[0-9a-f]{{8}}(?:-[0-9a-f]{{4}}){{3}}-[0-9a-f]{{12}}"
)


def _read_exact(connection: socket.socket, length: int) -> bytes:
    pieces: list[bytes] = []
    remaining = length
    while remaining:
        try:
            chunk = connection.recv(remaining)
        except TimeoutError:
            continue
        if not chunk:
            raise EOFError("terminal transport closed")
        pieces.append(chunk)
        remaining -= len(chunk)
    return b"".join(pieces)


def _receive_frame(connection: socket.socket) -> dict[str, Any]:
    length = struct.unpack("!I", _read_exact(connection, 4))[0]
    if length <= 0 or length > MAX_FRAME:
        raise ValueError("invalid terminal frame length")
    value = json.loads(_read_exact(connection, length))
    if not isinstance(value, dict):
        raise ValueError("terminal frame must be an object")
    return value


def _send_frame(connection: socket.socket, value: dict[str, Any]) -> None:
    connection.sendall(encode_frame(value))


def _window_size(descriptor: int, cols: int, rows: int) -> None:
    fcntl.ioctl(descriptor, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _terminal_preflight(payload: dict[str, Any]) -> None:
    account = _managed_account(payload)
    if account.pw_shell != "/usr/sbin/nologin":
        raise LifecycleValidationError(
            "HOST_ACCESS_POLICY_REJECTED",
            "web terminal requires managed-user host login to remain disabled",
        )
    host_authorized_keys = Path(account.pw_dir) / ".ssh/authorized_keys"
    if os.path.lexists(host_authorized_keys):
        raise LifecycleValidationError(
            "HOST_ACCESS_POLICY_REJECTED",
            "web terminal refuses a managed user with host SSH authorization",
        )
    container = _managed_container_security(payload, require_running=True)
    state = container.get("state", {})
    health = state.get("Health", {}) if isinstance(state, dict) else {}
    if isinstance(health, dict) and health and health.get("Status") != "healthy":
        raise LifecycleValidationError(
            "CONTAINER_STATE_REJECTED", "managed development container is not healthy"
        )
    authorized_keys = PILOT_DATA_ROOT / str(payload["username"]) / "home/.ssh/authorized_keys"
    fingerprints = _installed_key_fingerprints(
        authorized_keys, int(payload["uid"]), int(payload["gid"])
    )
    if sorted(fingerprints) != payload["expected_key_fingerprints"]:
        raise LifecycleValidationError(
            "CONTAINER_KEY_BINDING_REJECTED",
            "container SSH authorization differs from approved Portal key records",
        )


def _terminal_marker(request_id: str) -> str:
    return f"{TERMINAL_PROCESS_PREFIX}{uuid.UUID(request_id)}"


def _terminal_argv(payload: dict[str, Any], marker: str) -> list[str]:
    username = str(payload["username"])
    return [
        BINARIES["docker"],
        "exec",
        "--detach-keys=ctrl-]",
        "--interactive",
        "--tty",
        "--user",
        f"{payload['uid']}:{payload['gid']}",
        "--workdir",
        "/workspace",
        "--env",
        f"HOME=/home/{username}",
        "--env",
        f"USER={username}",
        "--env",
        f"LOGNAME={username}",
        "--env",
        "TERM=xterm-256color",
        str(payload["name"]),
        "/bin/bash",
        "-c",
        'exec -a "$1" /bin/bash --login',
        "h100-portal-terminal-wrapper",
        marker,
    ]


def _running_managed_containers() -> list[str]:
    try:
        result = subprocess.run(
            [
                BINARIES["docker"],
                "ps",
                "--format",
                "{{.Names}}",
            ],
            cwd="/",
            env=FIXED_ENV,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError("managed container discovery failed") from exc
    if result.returncode != 0:
        raise OSError("managed container discovery was rejected")
    return sorted(
        name
        for name in result.stdout.splitlines()
        if re.fullmatch(r"gpu-dev-[a-z][a-z0-9-]{0,31}", name) is not None
    )


def _marked_host_processes(marker: str | None = None) -> list[tuple[int, str]]:
    processes: list[tuple[int, str]] = []
    for container_name in _running_managed_containers():
        try:
            result = subprocess.run(
                [
                    BINARIES["docker"],
                    "top",
                    container_name,
                    "-eo",
                    "pid,ppid,user,args",
                ],
                cwd="/",
                env=FIXED_ENV,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise OSError("container terminal process discovery failed") from exc
        if result.returncode != 0:
            continue
        for line in result.stdout.splitlines()[1:]:
            fields = line.split(maxsplit=3)
            if len(fields) != 4 or not fields[0].isdigit():
                continue
            argv0 = fields[3].split(maxsplit=1)[0]
            if (marker is not None and argv0 == marker) or (
                marker is None and TERMINAL_PROCESS_PATTERN.fullmatch(argv0) is not None
            ):
                processes.append((int(fields[0]), argv0))
    return processes


def _terminate_marked_process(marker: str) -> None:
    for signal_value, wait_seconds in (
        (signal.SIGHUP, 0.5),
        (signal.SIGTERM, 0.5),
        (signal.SIGKILL, 0.5),
    ):
        processes = _marked_host_processes(marker)
        if not processes:
            return
        for process_id, _argv0 in processes:
            with suppress(ProcessLookupError):
                os.kill(process_id, signal_value)
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if not _marked_host_processes(marker):
                return
            time.sleep(0.05)
    if _marked_host_processes(marker):
        raise OSError("marked container terminal process could not be stopped")


def cleanup_orphan_terminals() -> int:
    markers = {marker for _process_id, marker in _marked_host_processes()}
    for marker in markers:
        _terminate_marked_process(marker)
    return len(markers)


def _input_reader(
    connection: socket.socket,
    incoming: queue.Queue[dict[str, Any]],
    stopped: threading.Event,
) -> None:
    while not stopped.is_set():
        try:
            frame = _receive_frame(connection)
        except EOFError:
            frame = {"type": "close", "reason": "CLIENT_DISCONNECTED"}
        except OSError, ValueError, json.JSONDecodeError:
            frame = {"type": "protocol_error"}
        while not stopped.is_set():
            try:
                incoming.put(frame, timeout=0.2)
                break
            except queue.Full:
                continue
        if frame.get("type") in {"close", "protocol_error"}:
            return


def _decode_input(frame: dict[str, Any]) -> bytes:
    if set(frame) != {"type", "data_b64"} or frame.get("type") != "input":
        raise ValueError("invalid terminal input frame")
    encoded = frame.get("data_b64")
    if not isinstance(encoded, str) or len(encoded) > 12 * 1024:
        raise ValueError("terminal input is too large")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("terminal input is not valid base64") from exc
    if not decoded or len(decoded) > TERMINAL_INPUT_MAX_BYTES:
        raise ValueError("terminal input is too large")
    if DOCKER_DETACH_BYTE in decoded:
        raise ValueError("terminal input contains the reserved detach control")
    return decoded


def _write_input(descriptor: int, data: bytes) -> None:
    written = 0
    while written < len(data):
        count = os.write(descriptor, data[written:])
        if count <= 0:
            raise OSError("terminal input write made no progress")
        written += count


def _resize(frame: dict[str, Any], descriptor: int, process: subprocess.Popen[bytes]) -> None:
    if set(frame) != {"type", "cols", "rows"} or frame.get("type") != "resize":
        raise ValueError("invalid terminal resize frame")
    cols = frame.get("cols")
    rows = frame.get("rows")
    if (
        not isinstance(cols, int)
        or not 20 <= cols <= 300
        or not isinstance(rows, int)
        or not 5 <= rows <= 120
    ):
        raise ValueError("invalid terminal dimensions")
    _window_size(descriptor, cols, rows)
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGWINCH)


def _stop_process(process: subprocess.Popen[bytes], master: int, marker: str) -> None:
    with suppress(OSError):
        os.write(master, b"exit\r")
    deadline = time.monotonic() + TERMINAL_CLOSE_GRACE_SECONDS
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if process.poll() is None:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGHUP)
    try:
        process.wait(timeout=TERMINAL_CLOSE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=TERMINAL_CLOSE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=TERMINAL_CLOSE_GRACE_SECONDS)
    _terminate_marked_process(marker)


def _serve_started_terminal(
    connection: socket.socket,
    request: WorkerRequest,
    payload: dict[str, Any],
    process: subprocess.Popen[bytes],
    master: int,
    marker: str,
) -> None:
    incoming: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=256)
    stopped = threading.Event()
    reader = threading.Thread(
        target=_input_reader,
        args=(connection, incoming, stopped),
        name=f"terminal-input-{request.request_id[:8]}",
        daemon=True,
    )
    reader.start()
    lease_expiry = datetime.fromisoformat(str(payload["lease_expires_at"])).astimezone(UTC)
    hard_deadline = min(
        time.monotonic() + TERMINAL_MAX_DURATION_SECONDS,
        time.monotonic() + max(0.0, (lease_expiry - datetime.now(UTC)).total_seconds()),
    )
    last_input = time.monotonic()
    next_preflight = last_input + TERMINAL_PREFLIGHT_INTERVAL_SECONDS
    reason = "PROCESS_EXITED"
    _send_frame(
        connection,
        {
            "status": "READY",
            "type": "ready",
            "handler": "self.container.terminal",
            "request_id": request.request_id,
            "container": payload["name"],
            "username": payload["username"],
            "uid": payload["uid"],
            "gid": payload["gid"],
            "gpu": "NONE",
            "host_access": "DISABLED",
            "idle_timeout_seconds": TERMINAL_IDLE_TIMEOUT_SECONDS,
            "max_duration_seconds": TERMINAL_MAX_DURATION_SECONDS,
        },
    )
    try:
        while process.poll() is None:
            now = time.monotonic()
            if now >= hard_deadline:
                reason = (
                    "LEASE_EXPIRED" if datetime.now(UTC) >= lease_expiry else "MAX_DURATION_REACHED"
                )
                break
            if now - last_input >= TERMINAL_IDLE_TIMEOUT_SECONDS:
                reason = "IDLE_TIMEOUT"
                break
            if now >= next_preflight:
                try:
                    _terminal_preflight(payload)
                except (LifecycleValidationError, OSError) as exc:
                    reason = getattr(exc, "code", "TERMINAL_SECURITY_GATE_FAILED")
                    break
                next_preflight = now + TERMINAL_PREFLIGHT_INTERVAL_SECONDS
            while True:
                try:
                    frame = incoming.get_nowait()
                except queue.Empty:
                    break
                frame_type = frame.get("type")
                try:
                    if frame_type == "input":
                        _write_input(master, _decode_input(frame))
                        last_input = time.monotonic()
                    elif frame_type == "resize":
                        _resize(frame, master, process)
                    elif frame_type == "close" and set(frame).issubset({"type", "reason"}):
                        reason = str(frame.get("reason", "CLIENT_CLOSED"))
                        if reason not in {"CLIENT_CLOSED", "CLIENT_DISCONNECTED"}:
                            reason = "CLIENT_CLOSED"
                        return
                    else:
                        raise ValueError("terminal transport frame was rejected")
                except OSError, ValueError:
                    reason = "PROTOCOL_REJECTED"
                    _send_frame(
                        connection,
                        {
                            "type": "error",
                            "code": reason,
                            "message": "terminal transport frame was rejected",
                        },
                    )
                    return
            readable, _, _ = select.select([master], [], [], 0.2)
            if readable:
                try:
                    output = os.read(master, TERMINAL_OUTPUT_CHUNK_BYTES)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise
                if not output:
                    break
                _send_frame(
                    connection,
                    {
                        "type": "output",
                        "data_b64": base64.b64encode(output).decode("ascii"),
                    },
                )
    finally:
        stopped.set()
        try:
            _stop_process(process, master, marker)
        except OSError:
            reason = "TERMINAL_CLEANUP_FAILED"
        exit_code = process.poll()
        with suppress(OSError):
            _send_frame(
                connection,
                {
                    "type": "exit",
                    "reason": reason,
                    "exit_code": exit_code,
                },
            )


def serve_terminal_connection(connection: socket.socket, request: WorkerRequest) -> None:
    process: subprocess.Popen[bytes] | None = None
    master: int | None = None
    slave: int | None = None
    marker: str | None = None
    try:
        if request.dry_run:
            raise LifecycleValidationError(
                "TERMINAL_DRY_RUN_REJECTED", "interactive terminal cannot run as a dry-run"
            )
        payload = validate_payload(request.operation_type, request.payload)
        expected_idempotency = re.fullmatch(
            rf"self-container-terminal:{re.escape(str(payload['managed_user_id']))}:"
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            request.idempotency_key,
        )
        if (
            request.requested_by != payload["username"]
            or request.approved_by != payload["username"]
            or expected_idempotency is None
        ):
            raise LifecycleValidationError(
                "RESOURCE_OWNERSHIP_REJECTED",
                "terminal request is not bound to its managed identity",
            )
        _terminal_preflight(payload)
        marker = _terminal_marker(request.request_id)
        master, slave = pty.openpty()
        _window_size(slave, int(payload["cols"]), int(payload["rows"]))
        process = subprocess.Popen(
            _terminal_argv(payload, marker),
            cwd="/",
            env={**FIXED_ENV, "TERM": "xterm-256color"},
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
            start_new_session=True,
            shell=False,
        )
        os.close(slave)
        slave = None
        connection.settimeout(1.0)
        _serve_started_terminal(connection, request, payload, process, master, marker)
    except (LifecycleValidationError, OSError, ValueError) as exc:
        code = getattr(exc, "code", "TERMINAL_START_FAILED")
        with suppress(OSError):
            _send_frame(
                connection,
                {
                    "status": "ERROR",
                    "error": {"code": code, "message": str(exc)[:512]},
                },
            )
    finally:
        if (
            process is not None
            and process.poll() is None
            and master is not None
            and marker is not None
        ):
            with suppress(OSError):
                _stop_process(process, master, marker)
        if slave is not None:
            with suppress(OSError):
                os.close(slave)
        if master is not None:
            with suppress(OSError):
                os.close(master)
