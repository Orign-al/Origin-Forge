#!/usr/bin/env python3
"""Route the ordinary-user CLI allowlist from managed Docker bridges to Portal API."""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import re
import signal
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

MAX_BODY = 2 * 1024 * 1024
MAX_RESPONSE = 2 * 1024 * 1024
JOB_ID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89aAbB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}"
JOB_DETAIL_RE = re.compile(rf"^/api/v1/self/jobs/{JOB_ID}$")
JOB_LOGS_RE = re.compile(rf"^/api/v1/self/jobs/{JOB_ID}/logs$")
JOB_CANCEL_RE = re.compile(rf"^/api/v1/self/jobs/{JOB_ID}/cancel$")


class IngressError(RuntimeError):
    pass


UPSTREAM_ERRORS = (OSError, http.client.HTTPException, IngressError)


def _load_config(path: Path, *, allow_untrusted: bool = False) -> dict[str, Any]:
    if path.is_symlink():
        raise IngressError("configuration must not be a symlink")
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise IngressError("configuration is not a regular file")
    if not allow_untrusted and (metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o444):
        raise IngressError("configuration ownership or mode is invalid")
    raw = path.read_bytes()
    if len(raw) > 1024 * 1024:
        raise IngressError("configuration is too large")
    payload = json.loads(raw)
    if (
        not isinstance(payload, dict)
        or payload.get("version") != 1
        or payload.get("service") != "H100 Portal"
        or payload.get("api_compatibility") != "h100.cli.v1"
        or payload.get("listen_port") != 18082
        or payload.get("upstream") != "http://127.0.0.1:18081"
        or not isinstance(payload.get("listeners"), list)
        or not payload["listeners"]
    ):
        raise IngressError("configuration contract is invalid")
    seen: set[str] = set()
    for item in payload["listeners"]:
        if not isinstance(item, dict):
            raise IngressError("listener contract is invalid")
        gateway = ipaddress.ip_address(str(item.get("gateway", "")))
        subnet = ipaddress.ip_network(str(item.get("subnet", "")), strict=True)
        if (
            not isinstance(gateway, ipaddress.IPv4Address)
            or not isinstance(subnet, ipaddress.IPv4Network)
            or gateway not in subnet
            or str(gateway) in seen
            or not re.fullmatch(
                r"br-[0-9a-f]{12}|[A-Za-z0-9_.-]{1,15}", str(item.get("bridge", ""))
            )
        ):
            raise IngressError("listener address contract is invalid")
        seen.add(str(gateway))
    return payload


def _allowed_route(method: str, path: str, query: str) -> bool:
    if method == "GET" and path == "/api/v1/cli/identity":
        return not query
    if path == "/api/v1/self/cli-auth":
        return method == "GET" and not query
    if path == "/api/v1/self/jobs/config":
        return method == "GET" and not query
    if path == "/api/v1/self/jobs":
        if method == "POST":
            return not query
        if method != "GET":
            return False
        parsed = parse_qs(query, keep_blank_values=True)
        return set(parsed).issubset({"limit", "state"}) and all(
            len(values) == 1 for values in parsed.values()
        )
    if JOB_DETAIL_RE.fullmatch(path) or JOB_LOGS_RE.fullmatch(path):
        return method == "GET" and not query
    if JOB_CANCEL_RE.fullmatch(path):
        return method == "POST" and not query
    return False


class PrivateIngressServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    allowed_subnet: ipaddress.IPv4Network
    upstream_host: str
    upstream_port: int


class PrivateIngressHandler(BaseHTTPRequestHandler):
    server: PrivateIngressServer
    protocol_version = "HTTP/1.1"
    server_version = "H100PrivateCLI"
    sys_version = ""

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _error(self, status: int, code: str, message: str) -> None:
        body = json.dumps(
            {"detail": {"code": code, "message": message}}, separators=(",", ":")
        ).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self) -> None:
        try:
            client = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            self._error(403, "PRIVATE_INGRESS_DENIED", "Private CLI ingress denied.")
            return
        if client not in self.server.allowed_subnet:
            self._error(403, "PRIVATE_INGRESS_DENIED", "Private CLI ingress denied.")
            return
        parsed = urlsplit(self.path)
        if (
            parsed.scheme
            or parsed.netloc
            or not _allowed_route(self.command, parsed.path, parsed.query)
        ):
            self._error(404, "CLI_ROUTE_NOT_FOUND", "CLI route not found.")
            return
        authorization = self.headers.get("Authorization")
        identity_request = parsed.path == "/api/v1/cli/identity"
        if identity_request and authorization is not None:
            self._error(
                400, "IDENTITY_CREDENTIAL_FORBIDDEN", "Identity check must not include credentials."
            )
            return
        if not identity_request and (
            authorization is None
            or not authorization.startswith("Bearer ")
            or len(authorization) > 512
        ):
            self._error(401, "AUTH_REQUIRED", "CLI token is required.")
            return
        if self.headers.get("Transfer-Encoding") is not None:
            self._error(400, "REQUEST_ENCODING_INVALID", "Transfer encoding is not supported.")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._error(400, "REQUEST_LENGTH_INVALID", "Request length is invalid.")
            return
        if length < 0 or length > MAX_BODY or (self.command == "GET" and length):
            self._error(413, "REQUEST_TOO_LARGE", "Request body is invalid or too large.")
            return
        body = self.rfile.read(length) if length else None
        target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        headers = {"Host": f"{self.server.upstream_host}:{self.server.upstream_port}"}
        for header in ("Accept", "Authorization", "Content-Type", "User-Agent"):
            value = self.headers.get(header)
            if value is not None:
                headers[header] = value
        connection = http.client.HTTPConnection(
            self.server.upstream_host, self.server.upstream_port, timeout=30
        )
        try:
            connection.request(self.command, target, body=body, headers=headers)
            response = connection.getresponse()
            response_body = response.read(MAX_RESPONSE + 1)
            if len(response_body) > MAX_RESPONSE:
                raise IngressError("upstream response exceeded the safety limit")
            self.send_response(response.status)
            for header in ("Content-Type", "Cache-Control"):
                value = response.getheader(header)
                if value is not None:
                    self.send_header(header, value)
            self.send_header("Content-Length", str(len(response_body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(response_body)
        except UPSTREAM_ERRORS:
            self._error(502, "PORTAL_UPSTREAM_UNAVAILABLE", "Portal upstream is unavailable.")
        finally:
            connection.close()

    do_GET = _proxy
    do_POST = _proxy


def serve(config: dict[str, Any]) -> None:
    servers: list[PrivateIngressServer] = []
    threads: list[threading.Thread] = []
    stop = threading.Event()
    try:
        for listener in config["listeners"]:
            server = PrivateIngressServer(
                (listener["gateway"], config["listen_port"]), PrivateIngressHandler
            )
            server.allowed_subnet = ipaddress.ip_network(listener["subnet"], strict=True)
            server.upstream_host = "127.0.0.1"
            server.upstream_port = 18081
            servers.append(server)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            threads.append(thread)

        def request_stop(_signum: int, _frame: object) -> None:
            stop.set()

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        stop.wait()
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--testing-allow-untrusted-config", action="store_true")
    args = parser.parse_args()
    serve(_load_config(args.config, allow_untrusted=args.testing_allow_untrusted_config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
