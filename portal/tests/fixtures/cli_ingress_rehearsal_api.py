#!/usr/bin/env python3
"""Disposable Portal contract used only by the container-origin ingress rehearsal."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

TOKEN = "h100_cli_" + "R" * 64
JOB_ID = "10000000-0000-4000-8000-000000000149"


class RehearsalServer(ThreadingHTTPServer):
    job: dict[str, object]


class Handler(BaseHTTPRequestHandler):
    server: RehearsalServer

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def reply(self, status: int, payload: object) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self) -> bool:
        return self.headers.get("Authorization") == f"Bearer {TOKEN}"

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/v1/cli/identity":
            self.reply(
                200,
                {
                    "service": "H100 Portal",
                    "api_compatibility": "h100.cli.v1",
                    "cli_versions": ["1.0.0"],
                },
            )
            return
        if not self.authorized():
            self.reply(401, {"detail": {"code": "AUTH_REQUIRED", "message": "required"}})
            return
        if path == "/api/v1/self/cli-auth":
            self.reply(
                200,
                {
                    "status": "AUTHENTICATED",
                    "user": {
                        "id": "20000000-0000-4000-8000-000000000001",
                        "login_name": "cli-user",
                        "unix_username": "cli-user",
                        "role": "user",
                    },
                    "credential": {
                        "id": "30000000-0000-4000-8000-000000000001",
                        "label": "network rehearsal",
                        "expires_at": None,
                    },
                },
            )
            return
        if path == "/api/v1/self/jobs/config":
            self.reply(
                200,
                {
                    "status": "OK",
                    "defaults": {
                        "cpus": 2,
                        "memory_mb": 4096,
                        "gpu_count": 0,
                        "time_limit_seconds": 1800,
                    },
                    "limits": {"script_bytes": {"maximum": 8192}},
                    "allowed_script_roots": ["/workspace", "/home/cli-user"],
                    "username": "cli-user",
                    "cli_version": "1.0.0",
                },
            )
            return
        if path == "/api/v1/self/jobs":
            self.reply(200, {"status": "OK", "jobs": [self.server.job], "count": 1})
            return
        if path == f"/api/v1/self/jobs/{JOB_ID}":
            self.reply(200, {"status": "OK", "job": self.server.job})
            return
        if path == f"/api/v1/self/jobs/{JOB_ID}/logs":
            self.reply(
                200,
                {
                    "status": "OK",
                    "stdout": "container-origin ingress rehearsal\n",
                    "stderr": "",
                    "job": {**self.server.job, "state": "COMPLETED"},
                },
            )
            return
        self.reply(404, {"detail": {"code": "NOT_FOUND", "message": "not found"}})

    def do_POST(self) -> None:
        if not self.authorized():
            self.reply(401, {"detail": {"code": "AUTH_REQUIRED", "message": "required"}})
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/api/v1/self/jobs":
            if body.get("gpu_count") == 2:
                self.reply(
                    422,
                    {
                        "detail": {
                            "code": "GPU_LIMIT_EXCEEDED",
                            "message": "GPU request must be 0 or 1",
                        }
                    },
                )
                return
            self.server.job = {
                **self.server.job,
                "name": body["name"],
                "gpu_count": body["gpu_count"],
                "source_path": body["source_path"],
                "state": "PENDING",
            }
            self.reply(200, {"status": "SUBMITTED", "job": self.server.job})
            return
        if self.path == f"/api/v1/self/jobs/{JOB_ID}/cancel":
            self.server.job = {**self.server.job, "state": "CANCELLED"}
            self.reply(200, {"status": "CANCELLED", "job": self.server.job})
            return
        self.reply(404, {"detail": {"code": "NOT_FOUND", "message": "not found"}})


def main() -> None:
    server = RehearsalServer(("127.0.0.1", 18081), Handler)
    server.job = {
        "id": JOB_ID,
        "slurm_job_id": 149,
        "name": "rehearsal",
        "state": "PENDING",
        "reason": "Resources",
        "cpus": 2,
        "memory_mb": 4096,
        "gpu_count": 0,
        "time_limit_seconds": 1800,
        "source_path": "/workspace/rehearsal.sh",
        "submitted_at": "2026-08-31T12:00:00+00:00",
        "started_at": None,
        "finished_at": None,
        "elapsed_seconds": 0,
        "exit_code": None,
        "authoritative": True,
    }
    server.serve_forever()


if __name__ == "__main__":
    main()
