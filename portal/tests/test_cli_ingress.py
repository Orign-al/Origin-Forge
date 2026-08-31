from __future__ import annotations

import importlib.util
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType

import pytest

PORTAL_ROOT = Path(__file__).resolve().parents[1]


def _module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


config_module = _module("cli_ingress_config", PORTAL_ROOT / "deploy/scripts/cli_ingress_config.py")
proxy_module = _module("cli_ingress_proxy", PORTAL_ROOT / "deploy/scripts/cli_ingress_proxy.py")


def _container(username: str, network: str) -> dict[str, object]:
    return {
        "Name": f"/gpu-dev-{username}",
        "Config": {"Labels": {"h100.dev.user": username}},
        "HostConfig": {
            "Privileged": False,
            "NetworkMode": network,
            "PidMode": "",
            "IpcMode": "private",
        },
    }


def _network(username: str, octet: int, identity: str) -> dict[str, object]:
    return {
        "Name": f"h100-dev-{username}_default",
        "Id": identity * 64,
        "Driver": "bridge",
        "Scope": "local",
        "Internal": False,
        "Ingress": False,
        "Options": {},
        "IPAM": {"Config": [{"Subnet": f"172.{octet}.0.0/16", "Gateway": f"172.{octet}.0.1"}]},
    }


def test_inventory_uses_actual_per_container_gateways_and_emits_runtime_contract() -> None:
    containers = [
        _container("alice", "h100-dev-alice_default"),
        _container("bob", "h100-dev-bob_default"),
    ]
    networks = [_network("alice", 26, "a"), _network("bob", 28, "b")]
    interfaces = {"br-" + "a" * 12: {"172.26.0.1"}, "br-" + "b" * 12: {"172.28.0.1"}}

    inventory = config_module.build_inventory(containers, networks, interfaces)

    assert [item["gateway"] for item in inventory["listeners"]] == [
        "172.26.0.1",
        "172.28.0.1",
    ]
    assert inventory["listen_port"] == 18082
    assert inventory["upstream"] == "http://127.0.0.1:18081"
    assert config_module.runtime_config(inventory, "alice") == {
        "version": 1,
        "portal_url": "http://172.26.0.1:18082/api/v1",
        "service": "H100 Portal",
        "api_compatibility": "h100.cli.v1",
    }


def test_inventory_rejects_non_bridge_and_non_managed_subnet() -> None:
    container = _container("alice", "h100-dev-alice_default")
    network = _network("alice", 26, "a")
    network["Driver"] = "host"
    with pytest.raises(config_module.ContractError):
        config_module.build_inventory([container], [network], {"br-" + "a" * 12: {"172.26.0.1"}})

    network = _network("alice", 26, "a")
    network["IPAM"] = {"Config": [{"Subnet": "10.82.36.0/24", "Gateway": "10.82.36.1"}]}
    with pytest.raises(config_module.ContractError):
        config_module.build_inventory([container], [network], {"br-" + "a" * 12: {"10.82.36.1"}})


class UpstreamHandler(BaseHTTPRequestHandler):
    server: UpstreamServer

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _reply(self) -> None:
        self.server.requests.append((self.command, self.path, self.headers.get("Authorization")))
        if self.path == "/api/v1/cli/identity":
            payload = {
                "service": "H100 Portal",
                "api_compatibility": "h100.cli.v1",
                "cli_versions": ["1.0.0"],
            }
        else:
            payload = {"status": "OK"}
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _reply
    do_POST = _reply


class UpstreamServer(ThreadingHTTPServer):
    requests: list[tuple[str, str, str | None]]


@pytest.fixture
def ingress_pair():  # type: ignore[no-untyped-def]
    upstream = UpstreamServer(("127.0.0.1", 0), UpstreamHandler)
    upstream.requests = []
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    ingress = proxy_module.PrivateIngressServer(
        ("127.0.0.1", 0), proxy_module.PrivateIngressHandler
    )
    ingress.allowed_subnet = proxy_module.ipaddress.ip_network("127.0.0.0/8")
    ingress.upstream_host = "127.0.0.1"
    ingress.upstream_port = upstream.server_port
    ingress_thread = threading.Thread(target=ingress.serve_forever, daemon=True)
    ingress_thread.start()
    try:
        yield upstream, f"http://127.0.0.1:{ingress.server_port}"
    finally:
        ingress.shutdown()
        upstream.shutdown()
        ingress_thread.join()
        upstream_thread.join()
        ingress.server_close()
        upstream.server_close()


def _get(url: str, *, authorization: str | None = None) -> tuple[int, dict[str, object]]:
    headers = {"Authorization": authorization} if authorization else {}
    request = urllib.request.Request(url, headers=headers)  # noqa: S310 -- loopback fixture.
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_proxy_exposes_only_cli_allowlist_and_identity_probe_has_no_token(ingress_pair) -> None:  # type: ignore[no-untyped-def]
    upstream, base = ingress_pair
    status, identity = _get(f"{base}/api/v1/cli/identity")
    assert status == 200
    assert identity["service"] == "H100 Portal"

    status, _ = _get(f"{base}/api/v1/self/cli-auth", authorization="Bearer h100_cli_" + "A" * 64)
    assert status == 200
    assert upstream.requests == [
        ("GET", "/api/v1/cli/identity", None),
        ("GET", "/api/v1/self/cli-auth", "Bearer h100_cli_" + "A" * 64),
    ]

    for path in (
        "/api/v1/admin/users",
        "/api/v1/internal/worker",
        "/docs",
        "/openapi.json",
        "/api/v1/self/storage",
        "/api/v1/self/jobs?token=forbidden",
    ):
        status, _ = _get(base + path, authorization="Bearer h100_cli_" + "A" * 64)
        assert status == 404
    assert len(upstream.requests) == 2


def test_proxy_rejects_credentials_on_identity_route(ingress_pair) -> None:  # type: ignore[no-untyped-def]
    upstream, base = ingress_pair
    status, payload = _get(
        f"{base}/api/v1/cli/identity", authorization="Bearer h100_cli_" + "A" * 64
    )
    assert status == 400
    assert payload["detail"]["code"] == "IDENTITY_CREDENTIAL_FORBIDDEN"  # type: ignore[index]
    assert upstream.requests == []
