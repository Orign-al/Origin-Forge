#!/usr/bin/env python3
"""Build the root-owned private CLI ingress and per-container endpoint contracts."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import stat
import subprocess
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

DOCKER = "/usr/bin/docker"
IP = "/usr/sbin/ip"
LISTEN_PORT = 18082
UPSTREAM = "http://127.0.0.1:18081"
SERVICE = "H100 Portal"
API_COMPATIBILITY = "h100.cli.v1"
MANAGED_DOCKER_POOL = ipaddress.ip_network("172.16.0.0/12")
USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


class ContractError(RuntimeError):
    pass


def _run_json(argv: list[str]) -> Any:
    result = subprocess.run(
        argv,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"},
    )
    return json.loads(result.stdout)


def _discover() -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, set[str]]]:
    identifiers = subprocess.run(
        [DOCKER, "ps", "--all", "--quiet", "--filter", "label=h100.dev.user"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
    ).stdout.split()
    if not identifiers:
        raise ContractError("no managed Development Containers were found")
    containers = _run_json([DOCKER, "inspect", *identifiers])
    network_names = sorted(
        {str(item.get("HostConfig", {}).get("NetworkMode", "")) for item in containers}
    )
    if not all(network_names):
        raise ContractError("a managed container has no Docker network mode")
    networks = _run_json([DOCKER, "network", "inspect", *network_names])
    interfaces: dict[str, set[str]] = {}
    for network in networks:
        options = network.get("Options") or {}
        network_id = str(network.get("Id", ""))
        bridge = str(options.get("com.docker.network.bridge.name") or f"br-{network_id[:12]}")
        addresses = _run_json([IP, "-j", "address", "show", "dev", bridge])
        interfaces[bridge] = {
            str(item.get("local"))
            for record in addresses
            for item in record.get("addr_info", [])
            if item.get("family") == "inet"
        }
    return containers, networks, interfaces


def build_inventory(
    containers: list[dict[str, Any]],
    networks: list[dict[str, Any]],
    interface_addresses: dict[str, set[str]],
) -> dict[str, Any]:
    network_by_name = {str(item.get("Name", "")): item for item in networks}
    listeners_by_network: dict[str, dict[str, str]] = {}
    user_network: dict[str, str] = {}
    observed_subnets: list[ipaddress.IPv4Network] = []

    for container in containers:
        labels = container.get("Config", {}).get("Labels") or {}
        username = str(labels.get("h100.dev.user", ""))
        if USERNAME_RE.fullmatch(username) is None:
            raise ContractError("managed container user label is invalid")
        name = str(container.get("Name", "")).removeprefix("/")
        if name != f"gpu-dev-{username}":
            raise ContractError(f"managed container name is invalid for {username}")
        host = container.get("HostConfig") or {}
        if (
            host.get("Privileged") is not False
            or host.get("NetworkMode") in {"", "host", "none", "default"}
            or host.get("PidMode") == "host"
            or host.get("IpcMode") == "host"
        ):
            raise ContractError(f"managed container isolation is invalid for {username}")
        network_name = str(host["NetworkMode"])
        expected_network = f"h100-dev-{username}_default"
        if network_name != expected_network:
            raise ContractError(f"managed container network is unexpected for {username}")
        network = network_by_name.get(network_name)
        if network is None:
            raise ContractError(f"managed Docker network is missing for {username}")
        if (
            network.get("Driver") != "bridge"
            or network.get("Scope") != "local"
            or network.get("Internal") is not False
            or network.get("Ingress") is not False
        ):
            raise ContractError(f"managed Docker network contract is invalid for {username}")
        ipam = (network.get("IPAM") or {}).get("Config") or []
        ipv4 = [item for item in ipam if item.get("Subnet") and ":" not in str(item["Subnet"])]
        if len(ipv4) != 1 or not ipv4[0].get("Gateway"):
            raise ContractError(f"managed Docker IPv4 contract is invalid for {username}")
        try:
            subnet = ipaddress.ip_network(str(ipv4[0]["Subnet"]), strict=True)
            gateway = ipaddress.ip_address(str(ipv4[0]["Gateway"]))
        except ValueError as exc:
            raise ContractError(f"managed Docker IPAM is invalid for {username}") from exc
        if (
            not isinstance(subnet, ipaddress.IPv4Network)
            or not isinstance(gateway, ipaddress.IPv4Address)
            or not subnet.subnet_of(MANAGED_DOCKER_POOL)
            or gateway not in subnet
            or gateway in {subnet.network_address, subnet.broadcast_address}
        ):
            raise ContractError(
                f"managed Docker gateway is outside the approved pool for {username}"
            )
        options = network.get("Options") or {}
        network_id = str(network.get("Id", ""))
        if not re.fullmatch(r"[0-9a-f]{64}", network_id):
            raise ContractError(f"managed Docker network identity is invalid for {username}")
        bridge = str(options.get("com.docker.network.bridge.name") or f"br-{network_id[:12]}")
        if str(gateway) not in interface_addresses.get(bridge, set()):
            raise ContractError(f"host bridge gateway is unavailable for {username}")
        listener = {
            "network": network_name,
            "bridge": bridge,
            "subnet": str(subnet),
            "gateway": str(gateway),
        }
        listeners_by_network[network_name] = listener
        user_network[username] = network_name
        observed_subnets.append(subnet)

    for index, subnet in enumerate(observed_subnets):
        for other in observed_subnets[index + 1 :]:
            if subnet.overlaps(other):
                raise ContractError("managed Development Container networks overlap")

    listeners = sorted(
        listeners_by_network.values(), key=lambda item: int(ipaddress.ip_address(item["gateway"]))
    )
    return {
        "version": 1,
        "service": SERVICE,
        "api_compatibility": API_COMPATIBILITY,
        "listen_port": LISTEN_PORT,
        "upstream": UPSTREAM,
        "listeners": listeners,
        "users": {key: user_network[key] for key in sorted(user_network)},
    }


def runtime_config(inventory: dict[str, Any], username: str) -> dict[str, Any]:
    if USERNAME_RE.fullmatch(username) is None:
        raise ContractError("managed username is invalid")
    network_name = (inventory.get("users") or {}).get(username)
    listener = next(
        (item for item in inventory.get("listeners", []) if item.get("network") == network_name),
        None,
    )
    if listener is None:
        raise ContractError(f"private CLI ingress is not configured for {username}")
    return {
        "version": 1,
        "portal_url": f"http://{listener['gateway']}:{inventory['listen_port']}/api/v1",
        "service": SERVICE,
        "api_compatibility": API_COMPATIBILITY,
    }


def _atomic_write(path: Path, payload: dict[str, Any]) -> bool:
    if path.is_symlink():
        raise ContractError("CLI ingress configuration target must not be a symlink")
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if path.exists() and path.read_bytes() == encoded:
        metadata = path.stat()
        if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o444:
            raise ContractError("existing CLI ingress configuration metadata is invalid")
        return False
    descriptor, temporary = tempfile.mkstemp(prefix=".cli-ingress.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o444)
        os.write(descriptor, encoded)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        os.chown(path, 0, 0)
        os.chmod(path, 0o444)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with suppress(FileNotFoundError):
            os.unlink(temporary)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--inventory", action="store_true")
    action.add_argument("--write", type=Path)
    action.add_argument("--container")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("h100-cli-ingress-config must run as root")
    inventory = build_inventory(*_discover())
    if args.inventory:
        print(json.dumps(inventory, sort_keys=True, separators=(",", ":")))
    elif args.container:
        print(
            json.dumps(
                runtime_config(inventory, args.container), sort_keys=True, separators=(",", ":")
            )
        )
    else:
        changed = _atomic_write(args.write, inventory)
        print(
            f"CLI INGRESS CONFIG {'UPDATED' if changed else 'UNCHANGED'} listeners={len(inventory['listeners'])}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
