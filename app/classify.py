"""Classify a raw LoRa frame into Meshtastic / LoRaWAN / proprietary."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from . import lorawan, meshtastic


def _printable_text(payload: bytes) -> Optional[str]:
    if len(payload) < 2:
        return None
    # Many proprietary beacons prefix a one-byte type/MHDR marker before an
    # ASCII payload; show the human text if the remainder is clean.
    candidate = payload
    if payload[0] in (0x00, 0xFF) and len(payload) > 3:
        candidate = payload[1:]
    try:
        text = candidate.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not text:
        return None
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\r\n\t")
    if printable / len(text) < 0.9:
        return None
    return text


def _raw(payload: bytes, profile: dict[str, Any]) -> dict[str, Any]:
    digest = hashlib.sha1(payload).hexdigest()[:8]
    text = _printable_text(payload)
    ascii_view = "".join(chr(b) if 32 <= b < 127 else "." for b in payload)
    parsed_json = None
    if text and text.lstrip().startswith(("{", "[")):
        try:
            parsed_json = json.loads(text)
        except Exception:
            parsed_json = None
    prefix = payload[:4].hex().upper()
    profile_id = profile.get("id", "custom")
    fingerprint = f"{profile_id}:{len(payload)}:{prefix}"
    name = "Unknown LoRa device"
    if text:
        first = text.strip().split()[0][:16] if text.strip() else ""
        if first and all(ch.isalnum() or ch in "-_." for ch in first):
            name = f'Unknown "{first}"'
    return {
        "kind": "raw",
        "decoded": False,
        "fingerprint": fingerprint,
        "device_key": f"raw:{hashlib.sha1(fingerprint.encode()).hexdigest()[:12]}",
        "device_name": name,
        "device_model": "Proprietary LoRa (model unknown)",
        "summary": (text or payload.hex().upper())[:96],
        "text": text,
        "ascii": ascii_view,
        "hex": payload.hex().upper(),
        "json": parsed_json,
        "length": len(payload),
        "digest": digest,
        "raw": payload.hex().upper(),
    }


def _meshtastic_packet(packet: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    name = meshtastic.device_name(packet)
    model = meshtastic.device_model(packet)
    data = packet.get("data") or {}
    summary = meshtastic.describe(packet)
    return {
        "kind": "meshtastic",
        "decoded": bool(packet.get("decoded")),
        "encrypted": bool(packet.get("encrypted")),
        "candidate": bool(packet.get("candidate")),
        "device_key": meshtastic.device_key(packet),
        "device_name": name,
        "device_model": model,
        "summary": summary,
        "packet": packet,
        "hex": (packet.get("payload") or b"").hex().upper(),
        "length": len(packet.get("payload") or b""),
        "portnum": (data or {}).get("portnum"),
        "port_name": (data or {}).get("port_name"),
        "text": (data or {}).get("text"),
    }


def _lorawan_frame(frame: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "lorawan",
        "decoded": bool(frame.get("decoded")),
        "device_key": lorawan.device_key(frame),
        "device_name": lorawan.device_name(frame),
        "device_model": lorawan.device_model(frame),
        "summary": lorawan.describe(frame),
        "frame": frame,
        "hex": frame.get("raw", ""),
        "length": len(frame.get("raw", "")) // 2,
        "text": frame.get("text"),
        "cayenne": frame.get("cayenne"),
    }


def classify(payload: bytes, profile: dict[str, Any]) -> dict[str, Any]:
    # LoRaWAN first: its MHDR/FCtrl validation is stricter than the Meshtastic
    # header heuristic, and it avoids misreading a LoRaWAN MAC frame as a
    # direct Meshtastic packet.
    frame = lorawan.decode(payload)
    if frame:
        return _lorawan_frame(frame, profile)
    if len(payload) >= 18:
        packet = meshtastic.decode(payload)
        if packet:
            return _meshtastic_packet(packet, profile)
    return _raw(payload, profile)


def cad_signal(profile: dict[str, Any], rssi_dbm: float, detected: bool) -> dict[str, Any]:
    """A CAD-only observation: energy is present but no frame was decoded."""
    profile_id = profile.get("id", "custom")
    label = profile.get("label", profile_id)
    return {
        "kind": "signal",
        "decoded": False,
        "detected": bool(detected),
        "device_key": f"signal:{profile_id}",
        "device_name": f"Carrier · {profile.get('frequency_mhz', '?')} MHz",
        "device_model": "LoRa carrier (undecoded)",
        "summary": f"Activity on {label} · {rssi_dbm:.0f} dBm",
        "rssi_dbm": rssi_dbm,
        "profile": dict(profile),
    }
