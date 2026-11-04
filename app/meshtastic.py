"""Meshtastic packet decoding (header, AES-CTR payload, protobufs).

The on-air packet is::

    uint32 to | uint32 from | uint32 id | uint8 flags | uint8 channel |
    uint8 next_hop | uint8 relay_node | ciphertext

The ciphertext is AES-256-CTR over a protobuf ``Data`` message.  The nonce is
``packet_id (LE32) || from (LE32) || 8 zero bytes`` and the key is SHA-256 of
the channel PSK (the public/default PSK is the single byte ``0x01``).

Only the default PSK and unencrypted channels can be read without the user's
channel key; for other keys the packet is still shown as an encrypted
Meshtastic frame with source/destination metadata.
"""

from __future__ import annotations

import hashlib
import struct
from typing import Any, Optional

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from . import enums, protobuf

DEFAULT_PSK = b"\x01"
BROADCAST = 0xFFFFFFFF


def _key_from_psk(psk: bytes) -> bytes:
    if len(psk) == 32:
        return psk
    return hashlib.sha256(psk).digest()


DEFAULT_KEY = _key_from_psk(DEFAULT_PSK)


def _decrypt(ciphertext: bytes, packet_id: int, from_node: int, key: bytes) -> bytes:
    nonce = struct.pack("<II", packet_id & 0xFFFFFFFF, from_node & 0xFFFFFFFF) + b"\x00" * 8
    cipher = Cipher(algorithms.AES(key), modes.CTR(nonce))
    decryptor = cipher.decryptor()
    return decryptor.update(ciphertext) + decryptor.finalize()


def _parse_data(payload: bytes) -> Optional[dict[str, Any]]:
    try:
        fields = protobuf.parse(payload)
    except protobuf.ProtobufError:
        return None
    portnum = protobuf.as_int(protobuf.get(fields, 1))
    if portnum is None or not (0 <= portnum <= 511):
        return None
    data = {
        "portnum": portnum,
        "port_name": enums.port_name(portnum),
        "payload": protobuf.as_bytes(protobuf.get(fields, 2)),
        "want_response": protobuf.as_bool(protobuf.get(fields, 3)),
        "dest": protobuf.as_int(protobuf.get(fields, 4), 0) or 0,
        "source": protobuf.as_int(protobuf.get(fields, 5), 0) or 0,
        "request_id": protobuf.as_int(protobuf.get(fields, 6), 0) or 0,
        "reply_id": protobuf.as_int(protobuf.get(fields, 7), 0) or 0,
        "emoji": protobuf.as_int(protobuf.get(fields, 8), 0) or 0,
        "bitfield": protobuf.as_int(protobuf.get(fields, 9), 0) or 0,
    }
    return data


def _printable_ratio(data: bytes) -> float:
    if not data:
        return 0.0
    good = 0
    for byte in data:
        if 32 <= byte < 127 or byte in (9, 10, 13):
            good += 1
        elif byte >= 0xC2:  # rough UTF-8 continuation lead
            good += 0.8
    return good / len(data)


def _valid_text(data: bytes) -> Optional[str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not text:
        return None
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\r\n\t")
    if printable / max(1, len(text)) < 0.85:
        return None
    return text


def _parse_user(payload: bytes) -> Optional[dict[str, Any]]:
    try:
        fields = protobuf.parse(payload)
    except protobuf.ProtobufError:
        return None
    user_id = protobuf.as_str(protobuf.get(fields, 1))
    long_name = protobuf.as_str(protobuf.get(fields, 2))
    short_name = protobuf.as_str(protobuf.get(fields, 3))
    mac = protobuf.as_bytes(protobuf.get(fields, 4))
    hw_model = protobuf.as_int(protobuf.get(fields, 5), 0) or 0
    is_licensed = protobuf.as_bool(protobuf.get(fields, 6))
    role = protobuf.as_int(protobuf.get(fields, 7), 0) or 0
    public_key = protobuf.as_bytes(protobuf.get(fields, 8))
    if not (user_id or long_name or short_name):
        return None
    return {
        "id": user_id or None,
        "long_name": long_name or None,
        "short_name": short_name or None,
        "macaddr": ":".join(f"{b:02x}" for b in mac) if len(mac) == 6 else None,
        "hw_model": hw_model,
        "hw_model_name": enums.hardware_name(hw_model),
        "is_licensed": is_licensed,
        "role": role,
        "role_name": enums.role_name(role),
        "public_key": public_key.hex() if public_key else None,
    }


def _parse_position(payload: bytes) -> Optional[dict[str, Any]]:
    try:
        fields = protobuf.parse(payload)
    except protobuf.ProtobufError:
        return None
    lat = protobuf.as_sfixed32(protobuf.get(fields, 1), 0) or 0
    lon = protobuf.as_sfixed32(protobuf.get(fields, 2), 0) or 0
    alt = protobuf.as_sint32(protobuf.get(fields, 3), 0) or 0
    stamp = protobuf.as_fixed32(protobuf.get(fields, 4), 0) or 0
    if lat == 0 and lon == 0 and alt == 0 and stamp == 0:
        return None
    latitude = lat / 1e7
    longitude = lon / 1e7
    if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
        return None
    out = {
        "latitude": round(latitude, 7),
        "longitude": round(longitude, 7),
        "altitude": alt,
        "time": stamp or None,
        "location_source": protobuf.as_int(protobuf.get(fields, 5), 0),
        "altitude_source": protobuf.as_int(protobuf.get(fields, 6), 0),
        "timestamp": protobuf.as_fixed32(protobuf.get(fields, 7), 0) or None,
        "gps_accuracy": protobuf.as_int(protobuf.get(fields, 13), 0),
        "ground_track": protobuf.as_int(protobuf.get(fields, 14), 0),
        "ground_speed": protobuf.as_int(protobuf.get(fields, 15), 0),
    }
    return out


def _parse_device_metrics(payload: bytes) -> dict[str, Any]:
    try:
        fields = protobuf.parse(payload)
    except protobuf.ProtobufError:
        return {}
    out = {
        "battery_level": protobuf.as_int(protobuf.get(fields, 1), 0),
        "voltage": protobuf.as_float(protobuf.get(fields, 2), 0.0),
        "channel_utilization": protobuf.as_float(protobuf.get(fields, 3), 0.0),
        "air_util_tx": protobuf.as_float(protobuf.get(fields, 4), 0.0),
        "uptime_seconds": protobuf.as_int(protobuf.get(fields, 5), 0),
    }
    return {k: v for k, v in out.items() if v not in (None, 0, 0.0)}


def _parse_environment_metrics(payload: bytes) -> dict[str, Any]:
    try:
        fields = protobuf.parse(payload)
    except protobuf.ProtobufError:
        return {}
    names = {
        1: ("temperature", "°C"),
        2: ("relative_humidity", "%"),
        3: ("barometric_pressure", "hPa"),
        4: ("gas_resistance", "MΩ"),
        5: ("voltage", "V"),
        6: ("current", "A"),
        7: ("iaq", ""),
        8: ("distance", "mm"),
        9: ("lux", "lx"),
        10: ("white_lux", "lx"),
        11: ("ir_lux", "lx"),
        12: ("uv_lux", "lx"),
        13: ("wind_direction", "°"),
        14: ("wind_speed", "m/s"),
        15: ("weight", "kg"),
        16: ("wind_gust", "m/s"),
        17: ("wind_lull", "m/s"),
        18: ("radiation", "µR/h"),
        19: ("rainfall_1h", "mm"),
        20: ("rainfall_24h", "mm"),
        21: ("soil_moisture", "%"),
        22: ("soil_temperature", "°C"),
    }
    out = {}
    for number, (name, unit) in names.items():
        value = protobuf.get(fields, number)
        if value is None:
            continue
        if number == 7 or number == 13 or number == 21:
            parsed = protobuf.as_int(value, 0)
        else:
            parsed = protobuf.as_float(value, 0.0)
        if parsed not in (0, 0.0, None):
            out[name] = {"value": parsed, "unit": unit}
    return out


def _parse_telemetry(payload: bytes) -> Optional[dict[str, Any]]:
    try:
        fields = protobuf.parse(payload)
    except protobuf.ProtobufError:
        return None
    out: dict[str, Any] = {}
    time_value = protobuf.as_fixed32(protobuf.get(fields, 1), 0)
    if time_value:
        out["time"] = time_value
    device = protobuf.get(fields, 2)
    if device:
        metrics = _parse_device_metrics(protobuf.as_bytes(device))
        if metrics:
            out["device_metrics"] = metrics
    environment = protobuf.get(fields, 3)
    if environment:
        metrics = _parse_environment_metrics(protobuf.as_bytes(environment))
        if metrics:
            out["environment_metrics"] = metrics
    power = protobuf.get(fields, 5)
    if power:
        try:
            pfields = protobuf.parse(protobuf.as_bytes(power))
            voltages = [
                protobuf.as_float(protobuf.get(pfields, n), 0.0) for n in (1, 3, 5)
            ]
            currents = [
                protobuf.as_float(protobuf.get(pfields, n), 0.0) for n in (2, 4, 6)
            ]
            out["power_metrics"] = {
                "voltage": [v for v in voltages if v],
                "current": [c for c in currents if c],
            }
        except protobuf.ProtobufError:
            pass
    return out or None


def _parse_waypoint(payload: bytes) -> Optional[dict[str, Any]]:
    try:
        fields = protobuf.parse(payload)
    except protobuf.ProtobufError:
        return None
    lat = protobuf.as_sfixed32(protobuf.get(fields, 1), 0) or 0
    lon = protobuf.as_sfixed32(protobuf.get(fields, 2), 0) or 0
    name = protobuf.as_str(protobuf.get(fields, 5))
    description = protobuf.as_str(protobuf.get(fields, 6))
    if not (name or description or lat or lon):
        return None
    return {
        "latitude": lat / 1e7,
        "longitude": lon / 1e7,
        "name": name or None,
        "description": description or None,
    }


def _parse_routing(payload: bytes) -> dict[str, Any]:
    try:
        fields = protobuf.parse(payload)
    except protobuf.ProtobufError:
        return {}
    error = protobuf.as_int(protobuf.get(fields, 3), 0) or 0
    names = {
        0: "NONE",
        1: "NO_ROUTE",
        2: "GOT_NAK",
        3: "TIMEOUT",
        4: "NO_INTERFACE",
        5: "MAX_RETRANSMIT",
        6: "NO_CHANNEL",
        7: "TOO_LARGE",
        8: "NO_RESPONSE",
        9: "DUTY_CYCLE_LIMIT",
        10: "BAD_REQUEST",
        11: "NOT_AUTHORIZED",
        12: "PKI_FAILED",
        13: "PKI_UNKNOWN_PUBKEY",
        14: "ADMIN_BAD_SESSION_KEY",
        15: "ADMIN_PUBLIC_KEY_UNAUTHORIZED",
        16: "RATE_LIMIT_EXCEEDED",
        17: "PKI_SIGNATURE_INVALID",
    }
    return {"error_reason": error, "error_name": names.get(error, f"ERROR_{error}")}


def _parse_neighbor_info(payload: bytes) -> Optional[dict[str, Any]]:
    try:
        fields = protobuf.parse(payload)
    except protobuf.ProtobufError:
        return None
    node_id = protobuf.as_int(protobuf.get(fields, 1), 0) or 0
    last_by = protobuf.as_int(protobuf.get(fields, 2), 0) or 0
    interval = protobuf.as_int(protobuf.get(fields, 3), 0) or 0
    neighbors = []
    for raw in protobuf.get_all(fields, 4):
        try:
            nf = protobuf.parse(protobuf.as_bytes(raw))
            neighbors.append(
                {
                    "node_id": protobuf.as_int(protobuf.get(nf, 1), 0),
                    "snr": protobuf.as_float(protobuf.get(nf, 2), 0.0),
                    "last_rx_time": protobuf.as_fixed32(protobuf.get(nf, 3), 0),
                }
            )
        except protobuf.ProtobufError:
            continue
    if not (node_id or neighbors):
        return None
    return {
        "node_id": f"!{node_id:08x}" if node_id else None,
        "last_sent_by_id": f"!{last_by:08x}" if last_by else None,
        "broadcast_interval": interval,
        "neighbors": neighbors,
    }


def _parse_traceroute(payload: bytes) -> Optional[dict[str, Any]]:
    try:
        fields = protobuf.parse(payload)
    except protobuf.ProtobufError:
        return None

    def ints(number):
        values = []
        for raw in protobuf.get_all(fields, number):
            if isinstance(raw, (bytes, bytearray)):
                # Packed repeated int32.
                offset = 0
                while offset < len(raw):
                    try:
                        value, offset = protobuf.read_varint(bytes(raw), offset)
                    except protobuf.ProtobufError:
                        break
                    values.append(value)
            elif isinstance(raw, int):
                values.append(raw)
        return values

    route = ints(1)
    snr_towards = ints(2)
    route_back = ints(3)
    snr_back = ints(4)
    if not (route or route_back):
        return None
    return {
        "route": [f"!{x:08x}" for x in route],
        "route_back": [f"!{x:08x}" for x in route_back],
        "snr_towards": [x / 4.0 for x in snr_towards],
        "snr_back": [x / 4.0 for x in snr_back],
    }


def _parse_paxcounter(payload: bytes) -> Optional[dict[str, Any]]:
    try:
        fields = protobuf.parse(payload)
    except protobuf.ProtobufError:
        return None
    out = {
        "wifi": protobuf.as_int(protobuf.get(fields, 1), 0),
        "ble": protobuf.as_int(protobuf.get(fields, 2), 0),
        "uptime": protobuf.as_int(protobuf.get(fields, 3), 0),
    }
    return out if any(out.values()) else None


def _decode_portnum(portnum: int, payload: bytes) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if portnum == 1:  # TEXT_MESSAGE_APP
        text = _valid_text(payload)
        if text is None:
            return {}
        out["text"] = text
    elif portnum == 3:  # POSITION_APP
        position = _parse_position(payload)
        if position:
            out["position"] = position
    elif portnum == 4:  # NODEINFO_APP
        user = _parse_user(payload)
        if user:
            out["nodeinfo"] = user
    elif portnum == 5:  # ROUTING_APP
        out["routing"] = _parse_routing(payload)
    elif portnum == 8:  # WAYPOINT_APP
        waypoint = _parse_waypoint(payload)
        if waypoint:
            out["waypoint"] = waypoint
    elif portnum == 67:  # TELEMETRY_APP
        telemetry = _parse_telemetry(payload)
        if telemetry:
            out["telemetry"] = telemetry
    elif portnum == 70:  # TRACEROUTE_APP
        trace = _parse_traceroute(payload)
        if trace:
            out["traceroute"] = trace
    elif portnum == 71:  # NEIGHBORINFO_APP
        info = _parse_neighbor_info(payload)
        if info:
            out["neighborinfo"] = info
    elif portnum == 34:  # PAXCOUNTER_APP
        pax = _parse_paxcounter(payload)
        if pax:
            out["paxcounter"] = pax
    elif portnum == 66:  # RANGE_TEST_APP
        text = _valid_text(payload)
        if text:
            out["text"] = text
    elif portnum == 77:  # CAYENNE_APP
        out["cayenne_raw"] = payload.hex()
    elif portnum == 10:  # DETECTION_SENSOR_APP
        text = _valid_text(payload)
        if text:
            out["detection"] = text
        else:
            out["detection_raw"] = payload.hex()
    return out


def _plausible_decoded(data: dict[str, Any], decoded: dict[str, Any]) -> bool:
    portnum = decoded.get("portnum", 0)
    if portnum == 1:
        return "text" in decoded
    if portnum == 3:
        return "position" in decoded
    if portnum == 4:
        return "nodeinfo" in decoded
    if portnum == 67:
        return "telemetry" in decoded
    if portnum == 70:
        return "traceroute" in decoded
    if portnum == 71:
        return "neighborinfo" in decoded
    if portnum == 34:
        return "paxcounter" in decoded
    if portnum == 8:
        return "waypoint" in decoded
    if portnum == 5:
        return True
    if portnum in (6, 7, 9, 11, 12, 13, 32, 33, 64, 65, 68, 69, 72, 73, 74, 75, 76, 78, 79):
        return True
    if 256 <= portnum <= 511:
        return True
    return False


def decode(payload: bytes) -> Optional[dict[str, Any]]:
    """Decode a Meshtastic frame, or return ``None`` if it is not one."""
    if len(payload) < 16 or len(payload) > 255:
        return None
    to_node, from_node, packet_id = struct.unpack("<III", payload[:12])
    flags = payload[12]
    channel = payload[13]
    next_hop = payload[14]
    relay = payload[15]
    ciphertext = payload[16:]
    header = {
        "to": to_node,
        "from": from_node,
        "id": packet_id,
        "flags": flags,
        "hop_limit": flags & 0x07,
        "want_ack": bool(flags & 0x08),
        "via_mqtt": bool(flags & 0x10),
        "hop_start": (flags >> 5) & 0x07,
        "channel": channel,
        "next_hop": next_hop,
        "relay": relay,
    }
    # Cheap header sanity.  Most Meshtastic traffic is broadcast; for encrypted
    # traffic we require a broadcast destination so random raw frames are not
    # mistaken for Mesh packets.
    if from_node == 0 or packet_id == 0 or not ciphertext:
        return None

    decoded_data: Optional[dict[str, Any]] = None
    encrypted = True
    decrypted_blob = b""
    for key in (DEFAULT_KEY, None):
        if key is None:
            candidate = ciphertext
        else:
            try:
                candidate = _decrypt(ciphertext, packet_id, from_node, key)
            except Exception:
                continue
        parsed = _parse_data(candidate)
        if parsed is None:
            continue
        port_decoded = _decode_portnum(parsed["portnum"], parsed["payload"])
        parsed.update(port_decoded)
        if _plausible_decoded(parsed, parsed):
            decoded_data = parsed
            decrypted_blob = candidate
            encrypted = key is None
            break

    if decoded_data is None:
        if len(payload) < 18 or len(ciphertext) < 2:
            return None
        # Broadcast encrypted frames are unambiguous enough; direct frames are
        # accepted as "possible Meshtastic" when the hop fields are coherent
        # (hop_start >= hop_limit).  This keeps direct messages visible without
        # treating arbitrary proprietary frames as Mesh traffic.
        candidate = False
        if to_node != BROADCAST:
            hop_start = (flags >> 5) & 0x07
            hop_limit = flags & 0x07
            if hop_start < hop_limit or to_node == from_node:
                return None
            candidate = True
        return {
            "kind": "meshtastic",
            "encrypted": True,
            "decoded": False,
            "candidate": candidate,
            "header": header,
            "src": from_node,
            "dst": to_node,
            "packet_id": packet_id,
            "channel": channel,
            "payload": ciphertext,
            "data": None,
        }

    return {
        "kind": "meshtastic",
        "encrypted": encrypted,
        "decoded": True,
        "header": header,
        "src": from_node,
        "dst": to_node,
        "packet_id": packet_id,
        "channel": channel,
        "payload": decoded_data["payload"],
        "data": decoded_data,
        "decrypted_blob": decrypted_blob,
    }


def node_id(node_num: int) -> str:
    return f"!{node_num & 0xFFFFFFFF:08x}"


def device_key(packet: dict[str, Any]) -> str:
    return f"meshtastic:{packet['src'] & 0xFFFFFFFF:08x}"


def device_name(packet: dict[str, Any]) -> str:
    data = packet.get("data") or {}
    node = data.get("nodeinfo") or {}
    if node.get("long_name"):
        return node["long_name"]
    if node.get("short_name"):
        return node["short_name"]
    return f"Meshtastic {node_id(packet['src'])}"


def device_model(packet: dict[str, Any]) -> str:
    data = packet.get("data") or {}
    node = data.get("nodeinfo") or {}
    if node.get("hw_model_name"):
        return node["hw_model_name"]
    if packet.get("encrypted"):
        return "Meshtastic node (encrypted)"
    return "Meshtastic node"


def describe(packet: dict[str, Any]) -> str:
    data = packet.get("data") or {}
    port = data.get("port_name") or "encrypted"
    if data.get("text"):
        return f"{port}: {data['text'][:80]}"
    if data.get("nodeinfo"):
        node = data["nodeinfo"]
        return f"NODEINFO {node.get('long_name') or ''} ({node.get('hw_model_name')})"
    if data.get("position"):
        pos = data["position"]
        return f"POSITION {pos.get('latitude')}, {pos.get('longitude')} alt {pos.get('altitude')} m"
    if data.get("telemetry"):
        metrics = data["telemetry"].get("device_metrics") or {}
        return f"TELEMETRY battery {metrics.get('battery_level', '?')}%"
    return port
