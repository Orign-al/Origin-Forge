import hashlib
import json
from pathlib import Path

PORTAL_ROOT = Path(__file__).resolve().parents[1]
PLATFORM_ROOT = PORTAL_ROOT.parent


def test_portal3f_acceptance_tool_is_integrity_bound_and_installed() -> None:
    source = PLATFORM_ROOT / "scripts/h100-origin-pilot-acceptance"
    gpu_probe_source = PLATFORM_ROOT / "tests/gpu-device-mapping/gpu-device-context-probe.c"
    installer = PORTAL_ROOT / "deploy/scripts/install-runtime.sh"
    manifest = json.loads((PORTAL_ROOT / "deploy/worker-scripts.json").read_text())

    assert source.is_file()
    acceptance_text = source.read_text()
    assert (
        manifest["h100-origin-pilot-acceptance"] == hashlib.sha256(source.read_bytes()).hexdigest()
    )
    assert "git -c safe.directory=/srv/gpu-platform/platform" in acceptance_text
    assert (
        "read -r assoc_user assoc_account assoc_qos assoc_default_qos assoc_extra"
        in acceptance_text
    )
    assert "read -r qos_name qos_max_tres qos_extra" in acceptance_text
    assert "${QOS}|${QOS}|" not in acceptance_text
    assert "${QOS}|gres/gpu=1|" not in acceptance_text
    assert '--unit="${cpu_launcher_unit}"' in acceptance_text
    assert '--unit="${gpu_launcher_unit}"' in acceptance_text
    assert "/usr/sbin/runuser --user" not in acceptance_text
    assert "prepare_enroot_user_paths" in acceptance_text
    assert '"${PILOT_UID}:${PILOT_GID}:700"' in acceptance_text
    assert '"${log_dir}/gpu-compute-cgroup.txt"' in acceptance_text
    assert "slurmstepd.scope/job_${gpu_job_id}" in acceptance_text
    assert (
        "'/system.slice/slurmstepd.scope/job_' \"${log_dir}/gpu-stdout.txt\"" not in acceptance_text
    )
    assert (
        hashlib.sha256(gpu_probe_source.read_bytes()).hexdigest()
        == "120fc85413226ba4c106e5e1a291882900a20ec40f31bebea21643f152fcf4d1"
    )

    install_text = installer.read_text()
    assert 'PORTAL3F_ACCEPTANCE_SOURCE="${PLATFORM_DIR}/scripts/' in install_text
    assert '"$PORTAL3F_ACCEPTANCE_SOURCE"' in install_text
    assert '"$RUNTIME_DIR/scripts/h100-origin-pilot-acceptance"' in install_text
    assert 'PORTAL3F_GPU_PROBE_SOURCE="${PLATFORM_DIR}/tests/' in install_text
    assert '"$PORTAL3F_GPU_PROBE_SOURCE"' in install_text
    assert '"$RUNTIME_DIR/tests/gpu-device-mapping/gpu-device-context-probe.c"' in install_text


def test_container_stop_is_integrity_bound_and_installed() -> None:
    source = PLATFORM_ROOT / "scripts/h100-container-stop"
    installer = PORTAL_ROOT / "deploy/scripts/install-runtime.sh"
    manifest = json.loads((PORTAL_ROOT / "deploy/worker-scripts.json").read_text())

    assert source.is_file()
    assert manifest["h100-container-stop"] == hashlib.sha256(source.read_bytes()).hexdigest()
    install_text = installer.read_text()
    assert 'CONTAINER_STOP_SOURCE="${PLATFORM_DIR}/scripts/h100-container-stop"' in install_text
    assert '"$CONTAINER_STOP_SOURCE"' in install_text
    assert "/usr/local/sbin/h100-container-stop" in install_text


def test_container_start_is_integrity_bound_and_supports_pre_lease_activation() -> None:
    source = PLATFORM_ROOT / "scripts/h100-container-start"
    installer = PORTAL_ROOT / "deploy/scripts/install-runtime.sh"
    manifest = json.loads((PORTAL_ROOT / "deploy/worker-scripts.json").read_text())

    assert source.is_file()
    assert manifest["h100-container-start"] == hashlib.sha256(source.read_bytes()).hexdigest()
    source_text = source.read_text()
    assert "ACTIVATING or ACTIVE state" in source_text
    assert "Lease must remain NOT_STARTED during activation" in source_text
    assert "Lease timestamps started before activation succeeded" in source_text
    assert "lease_expires_epoch - lease_start_epoch == 345600" in source_text
    assert "Host authorized_keys" in source_text
    install_text = installer.read_text()
    assert 'CONTAINER_START_SOURCE="${PLATFORM_DIR}/scripts/h100-container-start"' in install_text
    assert '"$CONTAINER_START_SOURCE"' in install_text
    assert "/usr/local/sbin/h100-container-start" in install_text


def test_portal3f_worker_can_write_only_the_guard_metrics_directory() -> None:
    unit = (PORTAL_ROOT / "deploy/systemd/h100-portal-worker.service").read_text()

    assert "ProtectSystem=strict" in unit
    assert "ReadWritePaths=/var/lib/node_exporter/textfile_collector" in unit
    ambient = [line for line in unit.splitlines() if line.startswith("AmbientCapabilities=")]
    assert ambient == ["AmbientCapabilities=CAP_SETUID"]


def test_web_binds_user_ingress_without_exposing_internal_services() -> None:
    service = (PORTAL_ROOT / "deploy/systemd/h100-portal-web.service").read_text()
    environment = (PORTAL_ROOT / "deploy/portal.env.example").read_text()
    installer = (PORTAL_ROOT / "deploy/scripts/install-runtime.sh").read_text()

    assert "Environment=HOSTNAME=0.0.0.0" in service.splitlines()
    assert "IPAddressDeny=any" in service.splitlines()
    assert "IPAddressAllow=localhost" in service.splitlines()
    assert "IPAddressAllow=10.10.10.0/24" in service.splitlines()
    assert "IPAddressAllow=20.10.10.0/24" in service.splitlines()
    assert "IPAddressAllow=10.82.36.0/24" in service.splitlines()
    assert "h100-portal-web-tun1.service" not in installer
    assert not (PORTAL_ROOT / "deploy/systemd/h100-portal-web-tun1.service").exists()
    assert "PORTAL_PUBLIC_ACCESS_HOST=20.10.10.3" in environment
    assert ",http://10.10.10.2," in environment
    assert "http://10.10.10.2:18080" in environment
    assert "http://10.10.10.220:18080" in environment
    assert "http://20.10.10.3:18080" in environment
    assert "http://20.10.10.3" in environment
    assert "https://20.10.10.3" in environment


def test_legacy_http_entry_is_specific_and_proxies_only_to_portal_web() -> None:
    socket = (PORTAL_ROOT / "deploy/systemd/h100-portal-legacy-http.socket").read_text()
    service = (PORTAL_ROOT / "deploy/systemd/h100-portal-legacy-http.service").read_text()
    installer = (PORTAL_ROOT / "deploy/scripts/install-runtime.sh").read_text()

    assert "ListenStream=10.10.10.2:80" in socket.splitlines()
    assert "ListenStream=0.0.0.0:80" not in socket.splitlines()
    assert "FreeBind=yes" in socket.splitlines()
    assert "systemd-socket-proxyd 127.0.0.1:18080" in service
    assert "127.0.0.1:18081" not in service
    assert "IPAddressDeny=any" in service.splitlines()
    assert "IPAddressAllow=localhost" in service.splitlines()
    assert "IPAddressAllow=10.10.10.0/24" in service.splitlines()
    assert "h100-portal-legacy-http.socket" in installer
    assert "h100-portal-legacy-http.service" in installer


def test_runtime_version_and_automatic_provision_reconciliation_are_system_bound() -> None:
    installer = (PORTAL_ROOT / "deploy/scripts/install-runtime.sh").read_text()
    service = (PORTAL_ROOT / "deploy/systemd/h100-portal-provision-reconcile.service").read_text()
    timer = (PORTAL_ROOT / "deploy/systemd/h100-portal-provision-reconcile.timer").read_text()

    assert 'git -c safe.directory="$PLATFORM_DIR"' in installer
    assert "rev-parse --verify 'HEAD^{commit}'" in installer
    assert "h100-portal-provision-reconcile.service" in installer
    assert "h100-portal-provision-reconcile.timer" in installer
    assert "User=h100-portal-api" in service
    assert (
        "ExecStart=/opt/h100-portal/venv/bin/h100-portal-provision-reconcile --pending" in service
    )
    assert "RestrictAddressFamilies=AF_UNIX" in service
    assert "CapabilityBoundingSet=" in service
    assert "OnUnitActiveSec=5min" in timer


def test_runtime_publishes_local_oci_base_before_installing_stage_handler() -> None:
    installer = (PORTAL_ROOT / "deploy/scripts/install-runtime.sh").read_text()

    publish = '"$RUNTIME_DIR/venv/bin/h100-portal-local-image" publish'
    stage_install = "/usr/local/sbin/h100-provision-stage"
    assert publish in installer
    assert '--deployment-version "$deployment_version"' in installer
    assert installer.index(publish) < installer.index(stage_install)


def test_compute_stage_publishes_ssh_on_ipv4_ingress_and_keeps_public_address_separate() -> None:
    source = PLATFORM_ROOT / "scripts/h100-provision-stage"
    worker = PORTAL_ROOT / "apps/worker/src/h100_portal_worker/handlers.py"
    manifest = json.loads((PORTAL_ROOT / "deploy/worker-scripts.json").read_text())
    source_text = source.read_text()
    worker_text = worker.read_text()

    assert manifest["h100-provision-stage"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert "readonly CONTAINER_PUBLISH_HOST=0.0.0.0" in source_text
    assert "host_ip: ${CONTAINER_PUBLISH_HOST}" in source_text
    assert '.HostConfig.PortBindings["22/tcp"][0].HostIp == $host_ip' in source_text
    assert 'CONTAINER_PUBLISH_HOST = "0.0.0.0"' in worker_text
    assert 'PUBLIC_ACCESS_HOST = "20.10.10.3"' in worker_text
    assert 'container.get("ssh_host_ip") == CONTAINER_PUBLISH_HOST' in worker_text
