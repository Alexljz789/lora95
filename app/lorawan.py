"""LoRaWAN PHY/MAC frame inspection.

LoRa95 does not have the network session keys, so it cannot decrypt LoRaWAN
application payloads or verify the MIC.  It can, however, read everything that
is sent in clear: MType, DevAddr, FCnt, FPort, FOpts, ADR/ACK flags, JoinEUI
and DevEUI, and it can decode CayenneLPP payloads (the most common sensor
format) when present.

This is enough to list "non-Meshtastic" LoRaWAN sensors and show their
broadcasts, which is exactly what a passive scanner can legitimately do.
"""

from __future__ import annotations

import struct
from typing import Any, Optional

from . import cayenne

MTYPE_NAMES = {
    0: "JOIN_REQUEST",
    1: "JOIN_ACCEPT",
    2: "UNCONFIRMED_DATA_UP",
    3: "UNCONFIRMED_DATA_DOWN",
    4: "CONFIRMED_DATA_UP",
    5: "CONFIRMED_DATA_DOWN",
    6: "RFU",
    7: "PROPRIETARY",
}


def _hex(data: bytes) -> str:
    return data.hex().upper()


def _eui(data: bytes) -> str:
    return ":".join(f"{byte:02X}" for byte in reversed(data))


def _printable(data: bytes) -> Optional[str]:
    if not data:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not text:
        return None
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\r\n\t")
    if printable / len(text) < 0.9:
        return None
    return text


def _decode_join_request(payload: bytes) -> dict[str, Any]:
    join_eui = payload[1:9]
    dev_eui = payload[9:17]
    dev_nonce = struct.unpack("<H", payload[17:19])[0]
    mic = payload[19:23]
    return {
        "join_eui": _eui(join_eui),
        "dev_eui": _eui(dev_eui),
        "dev_nonce": dev_nonce,
        "mic": _hex(mic),
    }


def _decode_data(payload: bytes, mtype: int) -> Optional[dict[str, Any]]:
    if len(payload) < 12:
        return None
    dev_addr_raw = payload[1:5]
    dev_addr = struct.unpack("<I", dev_addr_raw)[0]
    fctrl = payload[5]
    fcnt = struct.unpack("<H", payload[6:8])[0]
    fopts_len = fctrl & 0x0F
    if fopts_len > 15:
        return None
    # Bit 4 is the Class B flag in downlinks and RFU (must be 0) in uplinks.
    if mtype in (2, 4) and (fctrl & 0x10):
        return None
    fopts_end = 8 + fopts_len
    if fopts_end + 4 > len(payload):
        return None
    fopts = payload[8:fopts_end]
    fport: Optional[int] = None
    frm_payload = b""
    if fopts_len == 15:
        # No FPort field; the payload is on port 0 (MAC commands).
        fport = 0
        frm_payload = payload[fopts_end : len(payload) - 4]
    else:
        if fopts_end < len(payload) - 4:
            fport = payload[fopts_end]
            frm_payload = payload[fopts_end + 1 : len(payload) - 4]
        else:
            frm_payload = b""
    if fport == 0xFF:
        return None
    mic = payload[-4:]
    if dev_addr in (0, 0xFFFFFFFF):
        return None
    out: dict[str, Any] = {
        "dev_addr": f"{dev_addr:08X}",
        "dev_addr_raw": _hex(dev_addr_raw),
        "fctrl": fctrl,
        "adr": bool(fctrl & 0x80),
        "adr_ack_req": bool(fctrl & 0x40),
        "ack": bool(fctrl & 0x20),
        "class_b": bool(fctrl & 0x10),
        "fcnt": fcnt,
        "fopts": _hex(fopts) if fopts else "",
        "fport": fport,
        "frm_payload": _hex(frm_payload),
        "mic": _hex(mic),
        "payload_bytes": frm_payload,
    }
    if frm_payload:
        text = _printable(frm_payload)
        if text:
            out["text"] = text
        if fport is not None and 1 <= fport <= 223:
            lpp = cayenne.decode(frm_payload)
            if lpp:
                out["cayenne"] = lpp
                out["cayenne_summary"] = cayenne.summarize(lpp)
    return out


def decode(payload: bytes) -> Optional[dict[str, Any]]:
    """Return a LoRaWAN description, or None if the frame is not LoRaWAN."""
    if len(payload) < 12:
        return None
    mhdr = payload[0]
    mtype = (mhdr >> 5) & 0x07
    major = mhdr & 0x03
    if major not in (0, 1):
        return None
    # A frame that is entirely printable ASCII is almost certainly a text
    # beacon, not a binary LoRaWAN MAC frame.
    printable = sum(1 for byte in payload if 32 <= byte < 127)
    if printable / max(1, len(payload)) > 0.88:
        return None
    base: dict[str, Any] = {
        "kind": "lorawan",
        "mhdr": mhdr,
        "mtype": mtype,
        "mtype_name": MTYPE_NAMES.get(mtype, f"MTYPE_{mtype}"),
        "major": major,
        "raw": _hex(payload),
    }
    if mtype == 0:
        if len(payload) != 23:
            return None
        base.update(_decode_join_request(payload))
        base["decoded"] = True
        return base
    if mtype == 1:
        if len(payload) < 17:
            return None
        base["decoded"] = False
        base["note"] = "JoinAccept is encrypted with the network AppKey"
        base["payload"] = _hex(payload[1:-4])
        base["mic"] = _hex(payload[-4:])
        return base
    if mtype in (2, 3, 4, 5):
        data = _decode_data(payload, mtype)
        if data is None:
            return None
        # Reject frames whose FOpts/payload arithmetic is inconsistent with a
        # real LoRaWAN data frame; this keeps random proprietary traffic out.
        if data["fport"] is None and not data["frm_payload"] and len(payload) > 12:
            return None
        base.update(data)
        base["decoded"] = True
        return base
    return None


def _join_key(dev_eui: str) -> str:
    return f"lorawan:join:{dev_eui.replace(':', '').upper()}"


def device_key(frame: dict[str, Any]) -> str:
    if frame.get("dev_eui"):
        return _join_key(frame["dev_eui"])
    if frame.get("dev_addr"):
        return f"lorawan:{frame['dev_addr']}"
    return "lorawan:unknown"


def device_name(frame: dict[str, Any]) -> str:
    if frame.get("dev_eui"):
        return f"LoRaWAN join {frame['dev_eui']}"
    if frame.get("dev_addr"):
        return f"LoRaWAN {frame['dev_addr']}"
    return "LoRaWAN device"


def device_model(frame: dict[str, Any]) -> str:
    if frame.get("dev_eui"):
        return "LoRaWAN OTAA join request"
    return "LoRaWAN device (model unknown)"


def describe(frame: dict[str, Any]) -> str:
    if frame.get("dev_eui"):
        return f"JOIN DevEUI {frame['dev_eui']}"
    if frame.get("cayenne_summary"):
        return frame["cayenne_summary"]
    if frame.get("text"):
        return frame["text"][:80]
    if frame.get("fport") is not None:
        return f"FPort {frame['fport']} · {frame.get('frm_payload', '')[:48]}"
    return frame.get("mtype_name", "LoRaWAN")
