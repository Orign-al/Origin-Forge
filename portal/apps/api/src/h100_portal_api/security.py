import base64
import hashlib
import hmac
import re
import secrets
import time
from collections.abc import Iterable

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from h100_portal_api.config import get_settings

LOGIN_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
SAFE_TARGET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/+\-]{0,127}$")
ALLOWED_KEY_TYPES = {"ssh-ed25519", "ecdsa-sha2-nistp256", "sk-ssh-ed25519@openssh.com"}
WEAK_PASSWORDS = {
    "passwordpassword",
    "changemechangeme",
    "administrator123",
    "qwertyuiopasdf",
    "12345678901234",
    "origin-alorigin-al",
}

PASSWORD_HASHER = PasswordHasher(type=Type.ID)


def normalize_login(value: str) -> str:
    normalized = value.strip().casefold()
    if not LOGIN_RE.fullmatch(normalized):
        raise ValueError("invalid login name")
    return normalized


def validate_password(password: str, normalized_login: str) -> None:
    if not 14 <= len(password) <= 128:
        raise ValueError("password length must be between 14 and 128 characters")
    if not password.strip():
        raise ValueError("password must not be blank")
    if (
        password.casefold() == normalized_login
        or password.casefold() == f"{normalized_login}{normalized_login}"
    ):
        raise ValueError("password must not equal the login name")
    if password.casefold() in WEAK_PASSWORDS:
        raise ValueError("password is too common")


def hash_password(password: str) -> str:
    return PASSWORD_HASHER.hash(password)


def verify_password(encoded_hash: str, password: str) -> bool:
    try:
        return PASSWORD_HASHER.verify(encoded_hash, password)
    except VerifyMismatchError, VerificationError, InvalidHashError:
        return False


def random_token(byte_count: int = 48) -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(byte_count)).rstrip(b"=").decode("ascii")


def digest_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def digest_user_agent(value: str) -> str:
    return digest_secret(value[:512])


def signed_csrf_token(secret_key: str | None = None) -> str:
    secret = (secret_key or get_settings().secret_key).encode("utf-8")
    timestamp = str(int(time.time()))
    nonce = random_token(32)
    body = f"{timestamp}.{nonce}"
    signature = hmac.new(secret, body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def validate_signed_csrf(token: str, secret_key: str | None = None, max_age: int = 3600) -> bool:
    try:
        timestamp, nonce, signature = token.split(".", 2)
        issued = int(timestamp)
        if not nonce or abs(int(time.time()) - issued) > max_age:
            return False
        secret = (secret_key or get_settings().secret_key).encode("utf-8")
        body = f"{timestamp}.{nonce}"
        expected = hmac.new(secret, body.encode("ascii"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)
    except TypeError, ValueError:
        return False


def safe_target(value: str) -> str:
    if not SAFE_TARGET_RE.fullmatch(value):
        raise ValueError("invalid target identifier")
    return value


def safe_metadata(data: object, sensitive_keys: Iterable[str] | None = None) -> object:
    forbidden = {
        "password",
        "current_password",
        "new_password",
        "confirmation",
        "token",
        "secret",
        "session",
        "cookie",
        "private_key",
        "raw_private_key",
        "private_key_password",
        "private_key_passphrase",
        "private_key_path",
        "passphrase",
        "public_key",
        "raw_public_key",
        "public_key_file",
        "public_key_path",
        "authorized_keys",
        "database_url",
        "munge_key",
    }
    if sensitive_keys:
        forbidden.update(item.casefold() for item in sensitive_keys)
    if isinstance(data, dict):
        return {
            str(key): "[REDACTED]"
            if str(key).casefold() in forbidden
            else safe_metadata(value, forbidden)
            for key, value in data.items()
        }
    if isinstance(data, list):
        return [safe_metadata(value, forbidden) for value in data[:100]]
    if isinstance(data, tuple):
        return [safe_metadata(value, forbidden) for value in data[:100]]
    if isinstance(data, str) and "BEGIN " in data and "PRIVATE KEY" in data:
        return "[REDACTED]"
    if isinstance(data, (str, int, float, bool)) or data is None:
        return data
    return str(data)[:256]
