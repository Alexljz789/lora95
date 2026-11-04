"""A tiny, dependency-free protobuf wire-format reader.

Meshtastic payloads are protobuf messages.  Pulling in ``protobuf`` (and its
generated stubs) for a handful of fields would add a large dependency to the
uConsole image, so LoRa95 decodes the wire format directly.  We only need
varints, length-delimited messages, 32-bit floats and fixed32 timestamps.
"""

from __future__ import annotations

import struct
from typing import Any, Iterable


class ProtobufError(ValueError):
    pass


def read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise ProtobufError("truncated varint")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
        if shift > 70:
            raise ProtobufError("varint too long")


def parse(data: bytes) -> list[tuple[int, int, Any]]:
    """Return ``[(field_number, wire_type, value), ...]`` for a message."""
    fields: list[tuple[int, int, Any]] = []
    offset = 0
    while offset < len(data):
        key, offset = read_varint(data, offset)
        field = key >> 3
        wire = key & 0x07
        if field == 0:
            raise ProtobufError("field number 0")
        if wire == 0:
            value, offset = read_varint(data, offset)
        elif wire == 1:
            if offset + 8 > len(data):
                raise ProtobufError("truncated fixed64")
            value = data[offset : offset + 8]
            offset += 8
        elif wire == 2:
            length, offset = read_varint(data, offset)
            if length < 0 or offset + length > len(data):
                raise ProtobufError("truncated length-delimited field")
            value = data[offset : offset + length]
            offset += length
        elif wire == 5:
            if offset + 4 > len(data):
                raise ProtobufError("truncated fixed32")
            value = data[offset : offset + 4]
            offset += 4
        elif wire == 3:
            # Deprecated group start: skip recursively.
            depth = 1
            while depth and offset < len(data):
                key2, offset = read_varint(data, offset)
                w2 = key2 & 7
                if w2 == 3:
                    depth += 1
                elif w2 == 4:
                    depth -= 1
                elif w2 == 0:
                    _, offset = read_varint(data, offset)
                elif w2 == 1:
                    offset += 8
                elif w2 == 2:
                    ln, offset = read_varint(data, offset)
                    offset += ln
                elif w2 == 5:
                    offset += 4
                else:
                    raise ProtobufError("unknown group wire type")
            value = b""
        elif wire == 4:
            raise ProtobufError("unexpected group end")
        else:
            raise ProtobufError(f"unknown wire type {wire}")
        fields.append((field, wire, value))
    return fields


def get(fields: Iterable[tuple[int, int, Any]], number: int, default: Any = None) -> Any:
    for field, _wire, value in fields:
        if field == number:
            return value
    return default


def get_all(fields: Iterable[tuple[int, int, Any]], number: int) -> list[Any]:
    return [value for field, _wire, value in fields if field == number]


def as_int(value: Any, default: int | None = None) -> int | None:
    if value is None:
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, (bytes, bytearray)) and len(value) == 4:
        return int.from_bytes(value, "little")
    if isinstance(value, (bytes, bytearray)) and len(value) == 8:
        return int.from_bytes(value, "little")
    return default


def as_sint32(value: Any, default: int | None = None) -> int | None:
    """Decode a protobuf int32 (two's complement varint)."""
    if value is None:
        return default
    if isinstance(value, int):
        value &= 0xFFFFFFFF
        return value - 0x100000000 if value & 0x80000000 else value
    return default


def as_sfixed32(value: Any, default: int | None = None) -> int | None:
    if value is None:
        return default
    if isinstance(value, (bytes, bytearray)) and len(value) == 4:
        return struct.unpack("<i", bytes(value))[0]
    return default


def as_fixed32(value: Any, default: int | None = None) -> int | None:
    if value is None:
        return default
    if isinstance(value, (bytes, bytearray)) and len(value) == 4:
        return struct.unpack("<I", bytes(value))[0]
    return default


def as_float(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    if isinstance(value, (bytes, bytearray)) and len(value) == 4:
        return struct.unpack("<f", bytes(value))[0]
    return default


def as_double(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    if isinstance(value, (bytes, bytearray)) and len(value) == 8:
        return struct.unpack("<d", bytes(value))[0]
    return default


def as_bytes(value: Any, default: bytes = b"") -> bytes:
    if value is None:
        return default
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return default


def as_str(value: Any, default: str = "") -> str:
    return as_bytes(value).decode("utf-8", "replace").strip("\x00") or default


def as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, int):
        return bool(value)
    return default


def as_message(value: Any) -> list[tuple[int, int, Any]]:
    try:
        return parse(as_bytes(value))
    except ProtobufError:
        return []


def looks_like_message(data: bytes) -> bool:
    """Conservative sanity check used before trusting a decrypted payload."""
    if not data or len(data) > 512:
        return False
    try:
        fields = parse(data)
    except ProtobufError:
        return False
    if not fields:
        return False
    # A Data message always has a portnum in field 1.  Allow messages with no
    # fields 0..15 (that would be very unusual) but require at least one field.
    return any(1 <= field <= 64 for field, _wire, _value in fields)
