import hashlib
import subprocess
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
POLICY = REPOSITORY_ROOT / "config/ssh/70-h100-managed-compute-users.conf"
DEPLOYER = REPOSITORY_ROOT / "scripts/h100-managed-ssh-policy"
EXPECTED_POLICY = """# Managed compute SSH policy. Global and management-account policy is unchanged.
Match User origin-pilot
    PubkeyAuthentication yes
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    AuthenticationMethods publickey
Match all
"""
EXPECTED_SHA256 = "0a6b3037bd059f36b43a6a27e9c554af5378f42c283a30a863b359369e7ba6ed"


def test_managed_compute_policy_is_exact_and_scoped() -> None:
    content = POLICY.read_text(encoding="utf-8")

    assert content == EXPECTED_POLICY
    assert hashlib.sha256(content.encode()).hexdigest() == EXPECTED_SHA256
    assert "Match Group" not in content
    assert "PermitRootLogin" not in content
    assert "Port " not in content
    assert "ListenAddress" not in content
    assert "AllowUsers" not in content
    assert "DenyUsers" not in content


def test_managed_compute_policy_deployer_is_syntax_valid_and_non_reloading() -> None:
    subprocess.run(["/usr/bin/bash", "-n", str(DEPLOYER)], check=True)
    content = DEPLOYER.read_text(encoding="utf-8")

    assert "systemctl reload" not in content
    assert "systemctl restart" not in content
    assert "sshd_config.d/70-h100-managed-compute-users.conf" in content
    assert "origin-pilot|origin-al|codexops" in content
    assert '"${SSHD}" -t' in content
    assert '"${SSHD}" -T -C' in content
