import base64
import binascii
import hashlib
import struct
import subprocess
import unicodedata
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from h100_portal_api.models import PortalManagedUser, PortalSshKey, PortalUser
from h100_portal_api.security import ALLOWED_KEY_TYPES

MAX_PUBLIC_KEY_BYTES = 16 * 1024
SSH_KEYGEN = "/usr/bin/ssh-keygen"
PRIVATE_KEY_MARKERS = ("PRIVATE KEY",)


class SshPublicKeyValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ValidatedSshPublicKey:
    key_type: str
    fingerprint_sha256: str
    public_key: str
    comment: str
    content_sha256: str


def contains_private_key_material(value: object) -> bool:
    if not isinstance(value, str):
        return False
    upper = value.upper()
    return any(marker in upper for marker in PRIVATE_KEY_MARKERS)


def _read_ssh_string(blob: bytes, offset: int) -> tuple[bytes, int]:
    if offset + 4 > len(blob):
        raise SshPublicKeyValidationError(
            "SSH_PUBLIC_KEY_MALFORMED", "SSH public-key blob is truncated"
        )
    size = struct.unpack(">I", blob[offset : offset + 4])[0]
    start = offset + 4
    end = start + size
    if size > MAX_PUBLIC_KEY_BYTES or end > len(blob):
        raise SshPublicKeyValidationError(
            "SSH_PUBLIC_KEY_MALFORMED", "SSH public-key field has an invalid length"
        )
    return blob[start:end], end


def _validate_blob(key_type: str, blob: bytes) -> None:
    encoded_type, offset = _read_ssh_string(blob, 0)
    try:
        embedded_type = encoded_type.decode("ascii", errors="strict")
    except UnicodeDecodeError as exc:
        raise SshPublicKeyValidationError(
            "SSH_PUBLIC_KEY_MALFORMED", "SSH public-key type is not ASCII"
        ) from exc
    if embedded_type != key_type:
        raise SshPublicKeyValidationError(
            "SSH_PUBLIC_KEY_TYPE_MISMATCH", "SSH public-key type does not match its key blob"
        )
    if key_type == "ssh-ed25519":
        public_bytes, offset = _read_ssh_string(blob, offset)
        if len(public_bytes) != 32:
            raise SshPublicKeyValidationError(
                "SSH_PUBLIC_KEY_MALFORMED", "ED25519 public key must be 32 bytes"
            )
    elif key_type == "ecdsa-sha2-nistp256":
        curve, offset = _read_ssh_string(blob, offset)
        point, offset = _read_ssh_string(blob, offset)
        if curve != b"nistp256" or len(point) != 65 or point[:1] != b"\x04":
            raise SshPublicKeyValidationError(
                "SSH_PUBLIC_KEY_MALFORMED", "ECDSA P-256 public key is malformed"
            )
    elif key_type == "sk-ssh-ed25519@openssh.com":
        public_bytes, offset = _read_ssh_string(blob, offset)
        application, offset = _read_ssh_string(blob, offset)
        if len(public_bytes) != 32 or not application:
            raise SshPublicKeyValidationError(
                "SSH_PUBLIC_KEY_MALFORMED", "security-key ED25519 public key is malformed"
            )
    else:
        raise SshPublicKeyValidationError(
            "SSH_PUBLIC_KEY_TYPE_REJECTED", "SSH public-key type is not approved"
        )
    if offset != len(blob):
        raise SshPublicKeyValidationError(
            "SSH_PUBLIC_KEY_MALFORMED", "SSH public-key blob contains trailing data"
        )


def _fingerprint_with_ssh_keygen(public_key: str) -> str:
    try:
        completed = subprocess.run(
            [SSH_KEYGEN, "-lf", "-", "-E", "sha256"],
            cwd="/",
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "LANG": "C"},
            input=f"{public_key}\n",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            check=False,
            shell=False,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SshPublicKeyValidationError(
            "SSH_PUBLIC_KEY_VALIDATION_UNAVAILABLE", "SSH public-key validation is unavailable"
        ) from exc
    if completed.returncode != 0:
        raise SshPublicKeyValidationError(
            "SSH_PUBLIC_KEY_MALFORMED", "ssh-keygen rejected the SSH public key"
        )
    fields = completed.stdout.split()
    if len(fields) < 2 or not fields[1].startswith("SHA256:"):
        raise SshPublicKeyValidationError(
            "SSH_PUBLIC_KEY_FINGERPRINT_FAILED", "SSH public key has no SHA-256 fingerprint"
        )
    return fields[1]


def _safe_comment(value: str) -> str:
    comment = value.strip()
    if len(comment) > 128 or any(
        unicodedata.category(character).startswith("C") for character in comment
    ):
        raise SshPublicKeyValidationError(
            "SSH_PUBLIC_KEY_COMMENT_REJECTED", "SSH public-key comment is invalid"
        )
    return comment


def validate_ssh_public_key(
    raw_public_key: str, requested_comment: str = ""
) -> ValidatedSshPublicKey:
    if contains_private_key_material(raw_public_key):
        raise SshPublicKeyValidationError(
            "SSH_PRIVATE_KEY_UPLOAD_REJECTED", "private-key material is forbidden"
        )
    encoded = raw_public_key.encode("utf-8", errors="strict")
    if not 0 < len(encoded) <= MAX_PUBLIC_KEY_BYTES:
        raise SshPublicKeyValidationError(
            "SSH_PUBLIC_KEY_SIZE_REJECTED", "SSH public key has an invalid size"
        )
    lines = [line.strip() for line in raw_public_key.splitlines() if line.strip()]
    if len(lines) != 1:
        raise SshPublicKeyValidationError(
            "SSH_PUBLIC_KEY_COUNT_REJECTED", "exactly one SSH public key is required"
        )
    parts = lines[0].split(maxsplit=2)
    if len(parts) < 2 or parts[0] not in ALLOWED_KEY_TYPES:
        raise SshPublicKeyValidationError(
            "SSH_PUBLIC_KEY_TYPE_REJECTED", "SSH public-key type is not approved"
        )
    key_type, encoded_blob = parts[:2]
    try:
        blob = base64.b64decode(encoded_blob, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SshPublicKeyValidationError(
            "SSH_PUBLIC_KEY_MALFORMED", "SSH public-key Base64 is malformed"
        ) from exc
    _validate_blob(key_type, blob)
    inline_comment = parts[2] if len(parts) == 3 else ""
    comment = _safe_comment(requested_comment or inline_comment)
    canonical = f"{key_type} {base64.b64encode(blob).decode('ascii')}"
    if comment:
        canonical = f"{canonical} {comment}"
    fingerprint = _fingerprint_with_ssh_keygen(canonical)
    return ValidatedSshPublicKey(
        key_type=key_type,
        fingerprint_sha256=fingerprint,
        public_key=canonical,
        comment=comment,
        content_sha256=hashlib.sha256(f"{canonical}\n".encode()).hexdigest(),
    )


def ssh_key_response(record: PortalSshKey) -> dict[str, Any]:
    """Serialize public metadata only; the full public-key line stays out of routine views."""
    return {
        "id": str(record.id),
        "managed_user_id": str(record.managed_user_id),
        "key_type": record.key_type,
        "fingerprint_sha256": record.fingerprint_sha256,
        "comment": record.comment,
        "scope": record.scope,
        "state": record.state,
        "generation_method": record.generation_method,
        "created_at": record.created_at,
        "created_by": str(record.created_by),
        "validated_at": record.validated_at,
        "installed_at": record.installed_at,
        "revoked_at": record.revoked_at,
    }


def ssh_enrollment_status(db: Session, user: PortalUser) -> dict[str, Any]:
    managed = db.scalar(
        select(PortalManagedUser).where(PortalManagedUser.portal_user_id == user.id)
    )
    if managed is None:
        return {
            "required": False,
            "managed_user_id": None,
            "compute_identity": None,
            "compute_state": "NOT_ENROLLED",
            "validated_key_count": 0,
            "ssh_key_state": "NOT_APPLICABLE",
            "setup_path": None,
        }
    validated_count = int(
        db.scalar(
            select(func.count(PortalSshKey.id)).where(
                PortalSshKey.managed_user_id == managed.id,
                PortalSshKey.state.in_({"VALIDATED", "INSTALLED"}),
                PortalSshKey.active.is_(True),
            )
        )
        or 0
    )
    state = (
        managed.onboarding_state.value
        if hasattr(managed.onboarding_state, "value")
        else str(managed.onboarding_state)
    )
    return {
        "required": state == "STAGED" and validated_count == 0,
        "managed_user_id": str(managed.id),
        "compute_identity": managed.unix_username,
        "compute_state": state,
        "validated_key_count": validated_count,
        "ssh_key_state": managed.ssh_key_state,
        "setup_path": f"/users/{user.id}?tab=ssh",
    }
