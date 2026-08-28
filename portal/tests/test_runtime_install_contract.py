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
    assert "lease_expires_epoch - lease_start_epoch <= 345600" in source_text
    assert "lease_start_epoch <= now_epoch" in source_text
    assert "Host authorized_keys" in source_text
    install_text = installer.read_text()
    assert 'CONTAINER_START_SOURCE="${PLATFORM_DIR}/scripts/h100-container-start"' in install_text
    assert '"$CONTAINER_START_SOURCE"' in install_text
    assert "/usr/local/sbin/h100-container-start" in install_text


def test_gpu_development_runtime_and_fail_safe_epilog_are_integrity_bound() -> None:
    runtime = PLATFORM_ROOT / "scripts/h100-container-gpu-runtime"
    epilog = PLATFORM_ROOT / "scripts/h100-gpu-development-epilog"
    installer = PORTAL_ROOT / "deploy/scripts/install-runtime.sh"
    manifest = json.loads((PORTAL_ROOT / "deploy/worker-scripts.json").read_text())
    slurm = (PLATFORM_ROOT / "config/slurm.conf").read_text()

    assert runtime.is_file() and epilog.is_file()
    assert (
        manifest["h100-container-gpu-runtime"] == hashlib.sha256(runtime.read_bytes()).hexdigest()
    )
    assert (
        manifest["h100-gpu-development-epilog"] == hashlib.sha256(epilog.read_bytes()).hexdigest()
    )
    assert epilog.stat().st_mode & 0o111 == 0o111

    install_text = installer.read_text()
    assert 'GPU_DEVELOPMENT_EPILOG_SOURCE="${PLATFORM_DIR}/scripts/' in install_text
    assert '"$GPU_DEVELOPMENT_EPILOG_SOURCE"' in install_text
    assert "/usr/local/sbin/h100-gpu-development-epilog" in install_text
    assert "Epilog=/usr/local/sbin/h100-gpu-development-epilog" in slurm.splitlines()

    runtime_text = runtime.read_text()
    epilog_text = epilog.read_text()
    assert "ACTIVATING or ACTIVE state" in runtime_text
    assert "Lease must remain NOT_STARTED during activation" in runtime_text
    assert "Lease timestamps started before activation succeeded" in runtime_text
    assert '-v expected_index="${gpu_indexes[0]}"' in runtime_text
    assert "-v index=" not in runtime_text
    assert "remove_gpu_container_fail_closed" in runtime_text
    assert 'expected_cgroup="/system.slice/docker-${container_id}.scope"' in runtime_text
    assert 'observed_cgroup="$(awk' in runtime_text
    assert "printf '1\\n' >\"${cgroup_dir}/cgroup.kill\"" in runtime_text
    assert runtime_text.index("docker rm --force") < runtime_text.index("cgroup.kill")
    assert runtime_text.index("cgroup.kill") < runtime_text.index("create --no-build")
    assert (
        runtime_text.index("h100_acquire_lock")
        < runtime_text.index("validate_live_slurm_allocation")
        < runtime_text.index("docker compose")
    )
    assert epilog_text.index("h100_acquire_lock") < epilog_text.index("docker ps --all")
    assert epilog_text.index("docker rm --force") < epilog_text.index("create --no-build")


def test_installer_publishes_every_root_allowlisted_script_from_the_manifest() -> None:
    installer = (PORTAL_ROOT / "deploy/scripts/install-runtime.sh").read_text()
    manifest = json.loads((PORTAL_ROOT / "deploy/worker-scripts.json").read_text())
    root_scripts = {
        name: PLATFORM_ROOT / "scripts" / name
        for name in manifest
        if name not in {"h100-platform-common", "h100-origin-pilot-acceptance"}
    }

    assert root_scripts
    for name, source in root_scripts.items():
        assert source.is_file(), name
        assert manifest[name] == hashlib.sha256(source.read_bytes()).hexdigest(), name
        assert str(source.relative_to(PLATFORM_ROOT)) in installer, name
        assert f"/usr/local/sbin/{name}" in installer, name


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

    assert "Environment=HOSTNAME=20.10.10.3" in service.splitlines()
    assert "IPAddressDeny=any" in service.splitlines()
    assert "IPAddressAllow=localhost" in service.splitlines()
    assert "IPAddressAllow=20.10.10.0/24" in service.splitlines()
    assert "IPAddressAllow=10.10.10.0/24" not in service.splitlines()
    assert "IPAddressAllow=10.82.36.0/24" not in service.splitlines()
    assert "h100-portal-web-tun1.service" not in installer
    assert not (PORTAL_ROOT / "deploy/systemd/h100-portal-web-tun1.service").exists()
    assert "PORTAL_PUBLIC_ACCESS_HOST=20.10.10.3" in environment
    assert "http://10.10.10.220:18080" in environment
    assert "http://20.10.10.3:18080" in environment
    assert "http://20.10.10.3" in environment
    assert "https://20.10.10.3" in environment
    assert "http://10.10.10.2:18080" not in environment


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
    common_text = (PLATFORM_ROOT / "scripts/h100-platform-common.sh").read_text()

    assert manifest["h100-provision-stage"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert "readonly H100_PUBLIC_ACCESS_IP=20.10.10.3" in common_text
    assert "readonly H100_LEGACY_PUBLIC_ACCESS_IP=10.10.10.220" in common_text
    assert "readonly -a H100_CONTAINER_PUBLISH_IPS=(" in common_text
    assert "readonly CONTAINER_PUBLISH_HOST=${H100_PUBLIC_ACCESS_IP}" in source_text
    assert "readonly CONTAINER_LEGACY_PUBLISH_HOST=${H100_LEGACY_PUBLIC_ACCESS_IP}" in source_text
    assert "host_ip: ${CONTAINER_PUBLISH_HOST}" in source_text
    assert "host_ip: ${CONTAINER_LEGACY_PUBLISH_HOST}" in source_text
    assert "map([.HostIp, .HostPort]) | sort" in source_text
    assert "CONTAINER_PUBLISH_HOST = PUBLIC_ACCESS_HOST" in worker_text
    assert 'PUBLIC_ACCESS_HOST = "20.10.10.3"' in worker_text
    assert 'LEGACY_PUBLIC_ACCESS_HOST = "10.10.10.220"' in worker_text
    assert (
        "CONTAINER_PUBLISH_HOSTS = (PUBLIC_ACCESS_HOST, LEGACY_PUBLIC_ACCESS_HOST)" in worker_text
    )
    assert "_container_ssh_ingress_matches(" in worker_text


def test_legacy_easytier_ingress_and_compose_reconciliation_are_installed() -> None:
    installer = (PORTAL_ROOT / "deploy/scripts/install-runtime.sh").read_text()
    ingress = (PLATFORM_ROOT / "scripts/h100-easytier-legacy-ingress").read_text()
    reconcile = (PLATFORM_ROOT / "scripts/h100-reconcile-dual-easytier-ingress").read_text()
    unit = (PORTAL_ROOT / "deploy/systemd/h100-easytier-legacy-ingress.service").read_text()

    assert '-i "${LEGACY_INTERFACE}"' in ingress
    assert "${H100_LEGACY_PUBLIC_ACCESS_IP}/32" in ingress
    assert "22023:22999" in ingress
    assert '--to-destination "${H100_PUBLIC_ACCESS_IP}"' in ingress
    assert "10.82.36.1" not in ingress
    assert "docker compose" in reconcile
    assert "H100_LEGACY_PUBLIC_ACCESS_IP" in reconcile
    assert "config --format json" in reconcile
    assert "/usr/local/sbin/h100-easytier-legacy-ingress" in installer
    assert "/usr/local/sbin/h100-reconcile-dual-easytier-ingress" in installer
    assert "h100-easytier-legacy-ingress.service" in installer
    assert "ExecStart=/usr/local/sbin/h100-easytier-legacy-ingress apply" in unit
