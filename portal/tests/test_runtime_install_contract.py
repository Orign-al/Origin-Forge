import hashlib
import json
from pathlib import Path

PORTAL_ROOT = Path(__file__).resolve().parents[1]
PLATFORM_ROOT = PORTAL_ROOT.parent


def test_portal3f_acceptance_tool_is_integrity_bound_and_installed() -> None:
    source = PLATFORM_ROOT / "scripts/h100-origin-pilot-acceptance"
    installer = PORTAL_ROOT / "deploy/scripts/install-runtime.sh"
    manifest = json.loads((PORTAL_ROOT / "deploy/worker-scripts.json").read_text())

    assert source.is_file()
    assert (
        manifest["h100-origin-pilot-acceptance"] == hashlib.sha256(source.read_bytes()).hexdigest()
    )

    install_text = installer.read_text()
    assert 'PORTAL3F_ACCEPTANCE_SOURCE="${PLATFORM_DIR}/scripts/' in install_text
    assert '"$PORTAL3F_ACCEPTANCE_SOURCE"' in install_text
    assert '"$RUNTIME_DIR/scripts/h100-origin-pilot-acceptance"' in install_text
