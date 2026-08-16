import re
from pathlib import Path

DEPLOYMENT_VERSION_PATH = Path("/opt/h100-portal/DEPLOYMENT_VERSION")
SOURCE_VERSION = "SOURCE_WORKTREE"


def deployment_version() -> str:
    """Return the installer-bound runtime version without accepting request input."""
    try:
        value = DEPLOYMENT_VERSION_PATH.read_text(encoding="ascii").strip()
    except OSError:
        return SOURCE_VERSION
    if re.fullmatch(r"[0-9a-f]{40}", value) is None:
        return "INVALID_RUNTIME_VERSION"
    return value
