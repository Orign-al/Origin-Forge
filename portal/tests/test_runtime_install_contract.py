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


def test_portal3f_worker_can_write_only_the_guard_metrics_directory() -> None:
    unit = (PORTAL_ROOT / "deploy/systemd/h100-portal-worker.service").read_text()

    assert "ProtectSystem=strict" in unit
    assert "ReadWritePaths=/var/lib/node_exporter/textfile_collector" in unit
