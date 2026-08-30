from __future__ import annotations

import json
import os
import stat
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

CLI = Path(__file__).parents[1] / "apps/cli/h100"
TOKEN = "h100_cli_" + "A" * 64
PORTAL_JOB_ID = "10000000-0000-4000-8000-000000000149"


class PortalHandler(BaseHTTPRequestHandler):
    server: PortalServer

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _json(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        return self.headers.get("Authorization") == f"Bearer {TOKEN}"

    def do_GET(self) -> None:
        if not self._authorized():
            self._json(401, {"detail": {"code": "AUTH_REQUIRED", "message": "required"}})
            return
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == "/api/v1/self/cli-auth":
            if self.server.redirect_auth:
                self.send_response(307)
                self.send_header("Location", f"{self.server.redirect_target}/capture")
                self.end_headers()
                return
            self._json(
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
                        "label": "test container",
                        "expires_at": None,
                        "last_used_at": "2026-08-30T12:00:00+00:00",
                    },
                },
            )
            return
        if path == "/api/v1/self/jobs/config":
            self._json(
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
                    "allowed_script_roots": [str(self.server.workspace), str(self.server.home)],
                    "username": "cli-user",
                    "cli_version": "1.0.0",
                },
            )
            return
        if path == "/api/v1/self/jobs":
            query = parse_qs(parsed.query)
            assert int(query.get("limit", [20])[0]) <= 200
            self._json(200, {"status": "OK", "jobs": [self.server.job], "count": 1})
            return
        if path == f"/api/v1/self/jobs/{PORTAL_JOB_ID}":
            self._json(200, {"status": "OK", "job": self.server.job})
            return
        if path == f"/api/v1/self/jobs/{PORTAL_JOB_ID}/logs":
            self.server.log_reads += 1
            terminal = self.server.log_reads > 1
            job = {**self.server.job, "state": "COMPLETED" if terminal else "RUNNING"}
            self._json(
                200,
                {
                    "status": "OK",
                    "stdout": "hello\nworld\n" if terminal else "hello\n",
                    "stderr": "",
                    "job": job,
                },
            )
            return
        self._json(404, {"detail": {"code": "JOB_NOT_FOUND", "message": "not found"}})

    def do_POST(self) -> None:
        if not self._authorized():
            self._json(401, {"detail": {"code": "AUTH_REQUIRED", "message": "required"}})
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/api/v1/self/jobs":
            self.server.submit_requests.append(body)
            if body.get("gpu_count") == 2:
                self._json(
                    422,
                    {
                        "detail": {
                            "code": "GPU_LIMIT_EXCEEDED",
                            "message": "GPU request must be 0 or 1",
                        }
                    },
                )
                return
            self.server.worker_calls += 1
            self.server.job = {
                **self.server.job,
                "name": body["name"],
                "gpu_count": body["gpu_count"],
                "cpus": body["cpus"],
                "memory_mb": body["memory_mb"],
                "source_path": body["source_path"],
            }
            self._json(200, {"status": "SUBMITTED", "job": self.server.job})
            return
        if self.path == f"/api/v1/self/jobs/{PORTAL_JOB_ID}/cancel":
            self.server.job = {**self.server.job, "state": "CANCELLED"}
            self._json(200, {"status": "CANCELLED", "job": self.server.job})
            return
        self._json(404, {"detail": {"code": "JOB_NOT_FOUND", "message": "not found"}})


class PortalServer(ThreadingHTTPServer):
    workspace: Path
    home: Path
    submit_requests: list[dict[str, object]]
    worker_calls: int
    log_reads: int
    redirect_auth: bool
    redirect_target: str
    job: dict[str, object]


@pytest.fixture
def portal(tmp_path: Path):  # type: ignore[no-untyped-def]
    server = PortalServer(("127.0.0.1", 0), PortalHandler)
    server.workspace = tmp_path / "workspace"
    server.home = tmp_path / "home" / "cli-user"
    server.workspace.mkdir()
    server.home.mkdir(parents=True)
    server.submit_requests = []
    server.worker_calls = 0
    server.log_reads = 0
    server.redirect_auth = False
    server.redirect_target = ""
    server.job = {
        "id": PORTAL_JOB_ID,
        "slurm_job_id": 149,
        "name": "train",
        "state": "PENDING",
        "reason": "Resources",
        "cpus": 2,
        "memory_mb": 4096,
        "gpu_count": 0,
        "time_limit_seconds": 1800,
        "script_path": ".portal/job-scripts/snapshot.sh",
        "script_snapshot_path": "/workspace/.portal/job-scripts/snapshot.sh",
        "source_path": str(server.workspace / "projects/train.sh"),
        "workdir": str(server.workspace / "projects"),
        "stdout_path": "outputs/job.out",
        "stderr_path": "outputs/job.err",
        "lease_deadline_at": "2026-08-31T00:00:00+00:00",
        "created_at": "2026-08-30T12:00:00+00:00",
        "submitted_at": "2026-08-30T12:00:01+00:00",
        "started_at": None,
        "finished_at": None,
        "elapsed_seconds": 0,
        "exit_code": None,
        "authoritative": True,
    }
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(server=server, url=f"http://127.0.0.1:{server.server_port}")
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _run(portal, config: Path, *args: str, stdin: str | None = None):  # type: ignore[no-untyped-def]
    env = {
        **os.environ,
        "H100_CONFIG_HOME": str(config),
        "H100_PORTAL_URL": portal.url,
    }
    return subprocess.run(
        [str(CLI), *args],
        input=stdin,
        text=True,
        capture_output=True,
        check=False,
        env=env,
        timeout=15,
    )


def test_cli_auth_submit_list_status_logs_cancel_and_json_contract(portal, tmp_path: Path) -> None:
    config = tmp_path / "config"
    login = _run(portal, config, "auth", "login", "--token-stdin", "--json", stdin=TOKEN + "\n")
    assert login.returncode == 0
    assert json.loads(login.stdout)["command"] == "auth.login"
    assert TOKEN not in login.stdout + login.stderr
    credentials = config / "credentials"
    assert stat.S_IMODE(config.stat().st_mode) == 0o700
    assert stat.S_IMODE(credentials.stat().st_mode) == 0o600

    script = portal.server.workspace / "projects/train.sh"
    script.parent.mkdir()
    script.write_text("set -euo pipefail\necho train\n")
    submitted = _run(
        portal,
        config,
        "job",
        "submit",
        str(script),
        "--gpus",
        "1",
        "--memory",
        "32G",
        "--time",
        "01:00:00",
        "--json",
    )
    assert submitted.returncode == 0
    submit_json = json.loads(submitted.stdout)
    assert submit_json["schema_version"] == "h100.cli.v1"
    assert submit_json["job"]["id"] == PORTAL_JOB_ID
    assert portal.server.submit_requests[-1]["memory_mb"] == 32768
    assert portal.server.submit_requests[-1]["time_limit_seconds"] == 3600
    assert portal.server.submit_requests[-1]["script"] == script.read_text()
    assert "token" not in portal.server.submit_requests[-1]

    listed = _run(portal, config, "squeue", "--json")
    assert listed.returncode == 0
    assert json.loads(listed.stdout)["count"] == 1
    listed_human = _run(portal, config, "job", "list")
    assert listed_human.returncode == 0
    assert PORTAL_JOB_ID in listed_human.stdout
    status_result = _run(portal, config, "job", "status", "149", "--json")
    assert status_result.returncode == 0
    assert json.loads(status_result.stdout)["job"]["slurm_job_id"] == 149
    logs = _run(portal, config, "job", "logs", PORTAL_JOB_ID, "--follow")
    assert logs.returncode == 0
    assert logs.stdout == "hello\nworld\n"
    cancelled = _run(portal, config, "scancel", "149", "--json")
    assert cancelled.returncode == 0
    assert json.loads(cancelled.stdout)["cancellation_requested"] is True

    status_auth = _run(portal, config, "auth", "status", "--json")
    assert status_auth.returncode == 0
    assert json.loads(status_auth.stdout)["authenticated"] is True
    logout = _run(portal, config, "auth", "logout", "--json")
    assert logout.returncode == 0
    assert not credentials.exists()


def test_cli_exit_codes_policy_rejection_paths_and_directives(portal, tmp_path: Path) -> None:
    config = tmp_path / "config"
    missing = _run(portal, config, "job", "list", "--json")
    assert missing.returncode == 3
    assert json.loads(missing.stdout)["ok"] is False
    assert _run(portal, config, "auth", "login", "--token", "not-a-token").returncode == 2
    assert (
        _run(portal, config, "auth", "login", "--token-stdin", stdin=TOKEN + "\n").returncode == 0
    )

    script = portal.server.workspace / "gpu-test.sh"
    script.write_text("set -euo pipefail\nnvidia-smi -L\n")
    rejected = _run(portal, config, "job", "submit", str(script), "--gpus", "2", "--json")
    assert rejected.returncode == 5
    assert json.loads(rejected.stdout)["error"]["http_status"] == 422
    assert "at most one H100" in rejected.stderr
    assert portal.server.worker_calls == 0

    outside = tmp_path / "outside.sh"
    outside.write_text("echo denied\n")
    assert _run(portal, config, "job", "submit", str(outside)).returncode == 2
    directives = portal.server.workspace / "directives.sh"
    directives.write_text("#!/bin/bash\n#SBATCH --gres=gpu:4\necho denied\n")
    directive_result = _run(portal, config, "job", "submit", str(directives))
    assert directive_result.returncode == 2
    assert "rejects #SBATCH" in directive_result.stderr
    oversized = portal.server.workspace / "oversized.sh"
    oversized.write_bytes(b"x" * 8193)
    oversized_result = _run(portal, config, "job", "submit", str(oversized))
    assert oversized_result.returncode == 2
    assert "up to 8192 bytes" in oversized_result.stderr
    script_directory = portal.server.workspace / "script-directory"
    script_directory.mkdir()
    directory_result = _run(portal, config, "job", "submit", str(script_directory))
    assert directory_result.returncode == 2
    assert "regular file" in directory_result.stderr


def test_cli_never_forwards_token_across_http_redirect(portal, tmp_path: Path) -> None:
    captured: list[str | None] = []

    class CaptureHandler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_GET(self) -> None:
            captured.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"unexpected"}')

    capture = ThreadingHTTPServer(("127.0.0.1", 0), CaptureHandler)
    thread = threading.Thread(target=capture.serve_forever, daemon=True)
    thread.start()
    portal.server.redirect_auth = True
    portal.server.redirect_target = f"http://127.0.0.1:{capture.server_port}"
    try:
        result = _run(
            portal,
            tmp_path / "config",
            "auth",
            "login",
            "--token-stdin",
            "--json",
            stdin=TOKEN + "\n",
        )
    finally:
        capture.shutdown()
        thread.join()
        capture.server_close()

    assert result.returncode == 1
    assert json.loads(result.stdout)["error"]["http_status"] == 307
    assert TOKEN not in result.stdout + result.stderr
    assert captured == []
