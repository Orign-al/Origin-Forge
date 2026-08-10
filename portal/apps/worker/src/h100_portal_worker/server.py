import argparse
import json
import logging
import os
import pwd
import socket
import struct
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, datetime

from pydantic import ValidationError

from h100_portal_worker.handlers import handle
from h100_portal_worker.protocol import MAX_FRAME, ProtocolError, encode_frame
from h100_portal_worker.schemas import WorkerRequest
from h100_portal_worker.terminal import cleanup_orphan_terminals, serve_terminal_connection

LOG = logging.getLogger("h100-portal-worker")
SOCKET_PATH = "/run/h100-portal/worker.sock"
EXPECTED_API_USER = "h100-portal-api"
MAX_WORKER_CONNECTIONS = 32
MAX_TERMINAL_STREAMS = 8
CONTROL_REQUEST_LOCK = threading.Lock()
TERMINAL_STREAM_SLOTS = threading.BoundedSemaphore(MAX_TERMINAL_STREAMS)


def _read_exact(connection: socket.socket, length: int) -> bytes:
    pieces: list[bytes] = []
    remaining = length
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise ProtocolError("connection closed before frame completed")
        pieces.append(chunk)
        remaining -= len(chunk)
    return b"".join(pieces)


def _peer_is_api(connection: socket.socket) -> bool:
    if os.environ.get("H100_PORTAL_WORKER_TESTING") == "1":
        return True
    try:
        raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", raw)
        return bool(uid == pwd.getpwnam(EXPECTED_API_USER).pw_uid)
    except OSError, KeyError, struct.error:
        return False


def process_connection(connection: socket.socket) -> None:
    with connection:
        try:
            if not _peer_is_api(connection):
                connection.sendall(
                    encode_frame({"status": "ERROR", "error": {"code": "PEER_REJECTED"}})
                )
                return
            header = _read_exact(connection, 4)
            length = struct.unpack("!I", header)[0]
            if length <= 0 or length > MAX_FRAME:
                raise ProtocolError("invalid request length")
            body = _read_exact(connection, length)
            request = WorkerRequest.model_validate(json.loads(body))
            if request.operation_type == "self.container.terminal":
                if not TERMINAL_STREAM_SLOTS.acquire(blocking=False):
                    connection.sendall(
                        encode_frame(
                            {
                                "status": "ERROR",
                                "error": {
                                    "code": "TERMINAL_LIMIT_REACHED",
                                    "message": "controlled terminal stream limit reached",
                                },
                            }
                        )
                    )
                    return
                try:
                    serve_terminal_connection(connection, request)
                    LOG.info("worker terminal stream closed")
                finally:
                    TERMINAL_STREAM_SLOTS.release()
                return
            # The original Worker serialized fixed operations. Keep that
            # invariant while terminal streams use their own bounded slots.
            with CONTROL_REQUEST_LOCK:
                result = handle(request)
            result.setdefault("request_id", request.request_id)
            result.setdefault("captured_at", datetime.now(UTC).isoformat())
        except ProtocolError, json.JSONDecodeError, ValidationError:
            result = {
                "status": "ERROR",
                "error": {
                    "code": "PROTOCOL_REJECTED",
                    "message": "request rejected by fixed Worker protocol",
                },
            }
        except Exception:
            result = {
                "status": "ERROR",
                "error": {
                    "code": "WORKER_EXCEPTION",
                    "message": "Worker failed closed while processing the request",
                },
            }
        try:
            connection.sendall(encode_frame(result))
        except OSError:
            return
        LOG.info("worker request completed status=%s", result.get("status"))


def _process_and_release(connection: socket.socket, slots: threading.BoundedSemaphore) -> None:
    try:
        process_connection(connection)
    finally:
        slots.release()


def _socket_from_activation() -> socket.socket | None:
    if os.environ.get("LISTEN_FDS") != "1" or os.environ.get("LISTEN_PID") not in {
        str(os.getpid()),
        None,
    }:
        return None
    try:
        listener = socket.socket(fileno=3)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 0)
        return listener
    except OSError:
        return None


def build_listener() -> socket.socket:
    activated = _socket_from_activation()
    if activated is not None:
        return activated
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    with suppress(FileNotFoundError):
        os.unlink(SOCKET_PATH)
    listener.bind(SOCKET_PATH)
    os.chmod(SOCKET_PATH, 0o660)
    listener.listen(16)
    return listener


def main() -> None:
    parser = argparse.ArgumentParser(description="H100 Portal fixed-operation root worker")
    parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    listener = build_listener()
    try:
        cleaned = cleanup_orphan_terminals()
        if cleaned:
            LOG.warning("worker cleaned %s orphaned container terminal session(s)", cleaned)
    except OSError:
        # The container may legitimately be stopped. Each new terminal still
        # performs a complete running-container preflight before it can start.
        LOG.info("orphan terminal reconciliation skipped while container is unavailable")
    LOG.info("worker listening on controlled Unix socket")
    slots = threading.BoundedSemaphore(MAX_WORKER_CONNECTIONS)
    with (
        listener,
        ThreadPoolExecutor(
            max_workers=MAX_WORKER_CONNECTIONS, thread_name_prefix="portal-worker"
        ) as pool,
    ):
        while True:
            connection, _ = listener.accept()
            if not slots.acquire(blocking=False):
                with connection:
                    connection.sendall(
                        encode_frame(
                            {
                                "status": "ERROR",
                                "error": {
                                    "code": "WORKER_BUSY",
                                    "message": "controlled Worker connection limit reached",
                                },
                            }
                        )
                    )
                continue
            pool.submit(_process_and_release, connection, slots)


if __name__ == "__main__":
    main()
