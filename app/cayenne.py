"""CayenneLPP payload decoder.

CayenneLPP is a very common compact payload format used by LoRaWAN sensors
(and Meshtastic's CAYENNE_APP).  It is a sequence of::

    channel (1) | type (1) | value (N, big-endian)

Decoding it lets LoRa95 show useful readings from sensor nodes even when the
node is not a Meshtastic/LoRaWAN "communication" device.
"""

from __future__ import annotations

import struct
from typing import Any

# type id -> (name, size, unit, decoder)
TYPES: dict[int, tuple[str, int, str, str]] = {
    0: ("digital_input", 1, "", "u8"),
    1: ("digital_output", 1, "", "u8"),
    2: ("analog_input", 2, "", "s16_100"),
    3: ("analog_output", 2, "", "s16_100"),
    100: ("generic_sensor", 4, "", "u32"),
    101: ("illuminance", 2, "lx", "u16"),
    102: ("presence", 1, "", "u8"),
    103: ("temperature", 2, "°C", "s16_10"),
    104: ("humidity", 1, "%", "u8_2"),
    113: ("accelerometer", 6, "g", "xyz_1000"),
    114: ("barometer", 2, "hPa", "u16_10"),
    115: ("voltage", 2, "V", "u16_100"),
    116: ("current", 2, "A", "u16_1000"),
    117: ("frequency", 4, "Hz", "u32"),
    118: ("percentage", 1, "%", "u8"),
    119: ("altitude", 2, "m", "s16"),
    120: ("concentration", 2, "ppm", "u16"),
    121: ("power", 2, "W", "u16"),
    122: ("distance", 2, "m", "u16"),
    123: ("energy", 4, "kWh", "u32"),
    124: ("direction", 2, "°", "u16"),
    125: ("time", 4, "s", "u32"),
    126: ("gyrometer", 6, "°/s", "xyz_1000"),
    127: ("gps", 9, "", "gps"),
    128: ("switch", 1, "", "u8"),
}


def _decode_value(kind: str, raw: bytes) -> Any:
    if kind == "u8":
        return raw[0]
    if kind == "u8_2":
        return raw[0] / 2.0
    if kind == "s16":
        return struct.unpack(">h", raw)[0]
    if kind == "s16_10":
        return struct.unpack(">h", raw)[0] / 10.0
    if kind == "s16_100":
        return struct.unpack(">h", raw)[0] / 100.0
    if kind == "u16":
        return struct.unpack(">H", raw)[0]
    if kind == "u16_10":
        return struct.unpack(">H", raw)[0] / 10.0
    if kind == "u16_100":
        return struct.unpack(">H", raw)[0] / 100.0
    if kind == "u16_1000":
        return struct.unpack(">H", raw)[0] / 1000.0
    if kind == "u32":
        return struct.unpack(">I", raw)[0]
    if kind == "xyz_1000":
        x, y, z = struct.unpack(">hhh", raw)
        return [round(x / 1000.0, 4), round(y / 1000.0, 4), round(z / 1000.0, 4)]
    if kind == "gps":
        lat_raw = int.from_bytes(raw[0:3], "big", signed=True)
        lon_raw = int.from_bytes(raw[3:6], "big", signed=True)
        alt_raw = int.from_bytes(raw[6:9], "big", signed=True)
        return {
            "latitude": round(lat_raw / 10000.0, 6),
            "longitude": round(lon_raw / 10000.0, 6),
            "altitude": round(alt_raw / 100.0, 2),
        }
    return raw.hex()


def decode(payload: bytes) -> list[dict[str, Any]]:
    """Decode all channels; returns [] when the payload does not fit."""
    out: list[dict[str, Any]] = []
    offset = 0
    while offset + 2 <= len(payload):
        channel = payload[offset]
        type_id = payload[offset + 1]
        offset += 2
        spec = TYPES.get(type_id)
        if spec is None:
            # Unknown type: stop rather than guessing a length.
            break
        name, size, unit, kind = spec
        if offset + size > len(payload):
            break
        raw = payload[offset : offset + size]
        offset += size
        try:
            value = _decode_value(kind, raw)
        except Exception:
            break
        if isinstance(value, dict):
            value["type"] = name
            out.append({"channel": channel, **value, "unit": unit, "type_name": name})
        elif isinstance(value, list):
            out.append(
                {
                    "channel": channel,
                    "type_name": name,
                    "unit": unit,
                    "value": value,
                    "axes": ["x", "y", "z"],
                }
            )
        else:
            out.append(
                {
                    "channel": channel,
                    "type_name": name,
                    "unit": unit,
                    "value": value,
                }
            )
    return out


def summarize(entries: list[dict[str, Any]]) -> str:
    parts = []
    for entry in entries:
        value = entry.get("value")
        unit = entry.get("unit") or ""
        if isinstance(value, float):
            value = f"{value:.2f}"
        parts.append(f"{entry.get('type_name')}={value}{unit}")
    return ", ".join(parts)
