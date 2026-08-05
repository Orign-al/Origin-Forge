import json
import struct
from typing import Any

MAX_FRAME = 64 * 1024


class ProtocolError(ValueError):
    pass


def encode_frame(value: dict[str, Any]) -> bytes:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_FRAME:
        raise ProtocolError("frame exceeds maximum size")
    return struct.pack("!I", len(encoded)) + encoded


def decode_frame(data: bytes) -> dict[str, Any]:
    if len(data) < 4:
        raise ProtocolError("missing frame length")
    length = struct.unpack("!I", data[:4])[0]
    if length <= 0 or length > MAX_FRAME or len(data) != length + 4:
        raise ProtocolError("invalid frame length")
    value = json.loads(data[4:])
    if not isinstance(value, dict):
        raise ProtocolError("frame must contain an object")
    return value
