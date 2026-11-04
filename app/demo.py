"""Synthetic, protocol-valid traffic for the DEMO mode.

The demo feed is generated at the byte level (real Meshtastic AES-CTR frames,
real LoRaWAN MAC frames with CayenneLPP, and a proprietary beacon) so it
exercises exactly the same decoders as the radio path.  It is used for
screenshots and for UI work when no transmitter is nearby.
"""

from __future__ import annotations

import hashlib
import random
import struct
import threading
import time
from typing import Any, Callable

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

DEFAULT_KEY = hashlib.sha256(b"\x01").digest()


def _varint(value: int) -> bytes:
    value &= 0xFFFFFFFFFFFFFFFF
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _key(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _vi(field: int, value: int) -> bytes:
    return _key(field, 0) + _varint(value)


def _ld(field: int, data: bytes) -> bytes:
    return _key(field, 2) + _varint(len(data)) + data


def _f32(field: int, value: float) -> bytes:
    return _key(field, 5) + struct.pack("<f", value)


def _fixed32(field: int, value: int) -> bytes:
    return _key(field, 5) + struct.pack("<I", value)


def _sfixed32(field: int, value: int) -> bytes:
    return _key(field, 5) + struct.pack("<i", value)


def _encrypt(data: bytes, packet_id: int, src: int) -> bytes:
    nonce = struct.pack("<II", packet_id, src) + b"\x00" * 8
    cipher = Cipher(algorithms.AES(DEFAULT_KEY), modes.CTR(nonce))
    enc = cipher.encryptor()
    return enc.update(data) + enc.finalize()


def mesh_packet(src: int, portnum: int, payload: bytes, packet_id: int) -> bytes:
    data = _vi(1, portnum) + _ld(2, payload)
    ciphertext = _encrypt(data, packet_id, src)
    header = struct.pack("<III", 0xFFFFFFFF, src, packet_id)
    header += bytes([0x63, 0x08, 0x00, 0x00])  # hop 3 / hop_start 3, channel hash, next, relay
    return header + ciphertext


def nodeinfo_packet(src: int, long_name: str, short_name: str, hw_model: int, role: int, packet_id: int) -> bytes:
    mac = bytes.fromhex(f"{src & 0xFFFFFFFF:08x}")[:6]
    user = (
        _ld(1, f"!{src & 0xFFFFFFFF:08x}".encode())
        + _ld(2, long_name.encode())
        + _ld(3, short_name.encode())
        + _ld(4, mac)
        + _vi(5, hw_model)
        + _vi(7, role)
    )
    return mesh_packet(src, 4, user, packet_id)


def position_packet(src: int, lat: float, lon: float, alt: int, packet_id: int, stamp: int | None = None) -> bytes:
    body = (
        _sfixed32(1, int(lat * 1e7))
        + _sfixed32(2, int(lon * 1e7))
        + _vi(3, alt & 0xFFFFFFFF)
        + _fixed32(4, stamp or int(time.time()))
    )
    return mesh_packet(src, 3, body, packet_id)


def telemetry_packet(src: int, battery: int, voltage: float, uptime: int, packet_id: int, temp: float | None = None, humidity: float | None = None) -> bytes:
    device = (
        _vi(1, battery)
        + _f32(2, voltage)
        + _f32(3, 7.5)
        + _f32(4, 1.2)
        + _vi(5, uptime)
    )
    body = _fixed32(1, int(time.time())) + _ld(2, device)
    if temp is not None and humidity is not None:
        env = _f32(1, temp) + _f32(2, humidity) + _f32(3, 1013.2)
        body += _ld(3, env)
    return mesh_packet(src, 67, body, packet_id)


def text_packet(src: int, text: str, packet_id: int) -> bytes:
    return mesh_packet(src, 1, text.encode("utf-8"), packet_id)


def lora_sync(text: str) -> bytes:
    # A proprietary beacon: MHDR 0xFF (LoRaWAN proprietary) followed by ASCII.
    # Starting with 0xFF also keeps it out of the LoRaWAN data-frame parser.
    return b"\xff" + text.encode("ascii", "replace")


def lorawan_uplink(dev_addr: int, fcnt: int, fport: int, payload: bytes) -> bytes:
    mhdr = 0x40
    fctrl = 0x00
    body = (
        bytes([mhdr])
        + struct.pack("<I", dev_addr)
        + bytes([fctrl])
        + struct.pack("<H", fcnt & 0xFFFF)
        + bytes([fport])
        + payload
    )
    mic = struct.pack("<I", random.getrandbits(32))
    return body + mic


def cayenne_climate(temp_c: float, humidity_pct: float, voltage: float) -> bytes:
    out = bytearray()
    out += bytes([1, 103]) + struct.pack(">h", int(round(temp_c * 10)))
    out += bytes([2, 104]) + bytes([int(round(humidity_pct * 2)) & 0xFF])
    out += bytes([3, 115]) + struct.pack(">H", int(round(voltage * 100)))
    return bytes(out)


class DemoFeed:
    def __init__(self, ingest_packet: Callable[[dict[str, Any]], Any], ingest_signal: Callable[[dict[str, Any]], Any]) -> None:
        self.ingest_packet = ingest_packet
        self.ingest_signal = ingest_signal
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._packet_counter = 1000
        self._fcnt = 40

    def _pid(self) -> int:
        self._packet_counter += 1
        return self._packet_counter

    def _emit(self, data: bytes, profile: dict[str, Any], rssi: float, snr: float, ts: float | None = None) -> None:
        self.ingest_packet(
            {
                "ts": ts or time.time(),
                "data_hex": data.hex(),
                "rssi_dbm": rssi,
                "snr_db": snr,
                "profile": profile,
            }
        )

    def _profile(self, freq: float, sf: int, bw: float, sync: int, protocol: str, label: str) -> dict[str, Any]:
        return {
            "id": f"{protocol}:US:{freq:.4f}:{sf}/{int(bw)}/5/{sync:02x}/16",
            "label": label,
            "protocol": protocol,
            "region": "US",
            "frequency_mhz": freq,
            "sf": sf,
            "bw_khz": bw,
            "cr": 5,
            "sync_word": sync,
            "preamble": 16,
        }

    def burst(self) -> None:
        now = time.time()
        longfast = self._profile(906.875, 11, 250.0, 0x2B, "meshtastic", "Meshtastic LongFast · 906.875 MHz")
        self._emit(
            nodeinfo_packet(0x0A1B2C3D, "Mesa Ridge", "MESA", 9, 2, self._pid()),
            longfast,
            -67,
            9.5,
            now - 40,
        )
        self._emit(
            position_packet(0x0A1B2C3D, 39.7392, -104.9903, 1608, self._pid()),
            longfast,
            -68,
            9.0,
            now - 39,
        )
        self._emit(
            telemetry_packet(0x0A1B2C3D, 84, 4.06, 183920, self._pid(), 18.4, 38.0),
            longfast,
            -70,
            8.0,
            now - 38,
        )
        self._emit(
            text_packet(0x0A1B2C3D, "Ridge road is clear. Weather turning, expect wind after 18:00.", self._pid()),
            longfast,
            -66,
            10.0,
            now - 30,
        )
        self._emit(
            nodeinfo_packet(0x0B2C3D4E, "Trailhead", "TRL", 43, 0, self._pid()),
            longfast,
            -91,
            4.5,
            now - 25,
        )
        self._emit(
            text_packet(0x0B2C3D4E, "At the north gate. Two bars on the ridge repeater.", self._pid()),
            longfast,
            -90,
            4.0,
            now - 20,
        )
        # LoRaWAN + CayenneLPP weather node
        lora = self._profile(902.3, 9, 125.0, 0x34, "lorawan", "LoRaWAN 902.300 MHz · SF9/BW125")
        self._fcnt += 1
        self._emit(
            lorawan_uplink(0x260B1A2F, self._fcnt, 1, cayenne_climate(21.6, 47.0, 3.21)),
            lora,
            -104,
            6.0,
            now - 15,
        )
        # Proprietary beacon
        raw = self._profile(906.875, 9, 125.0, 0x12, "raw", "Private LoRa 906.875 MHz · SF9/BW125")
        self._emit(lora_sync("SENSOR-7 21.4C 48% 3.20V"), raw, -83, 5.5, now - 10)
        self.ingest_signal(
            {
                "ts": now - 8,
                "rssi_dbm": -109,
                "detected": True,
                "profile": self._profile(903.9, 9, 125.0, 0x34, "lorawan", "LoRaWAN 903.900 MHz · SF9/BW125"),
            }
        )

    def _run(self) -> None:
        try:
            self.burst()
        except Exception:
            pass
        while not self._stop.wait(6.0):
            try:
                self._tick()
            except Exception:
                pass

    def _tick(self) -> None:
        now = time.time()
        longfast = self._profile(906.875, 11, 250.0, 0x2B, "meshtastic", "Meshtastic LongFast · 906.875 MHz")
        roll = random.random()
        if roll < 0.45:
            self._emit(
                text_packet(0x0A1B2C3D, random.choice([
                    "Wind is picking up on the west face.",
                    "Repeater heard us; signal is stable.",
                    "Battery bank at 84%, solar input nominal.",
                ]), self._pid()),
                longfast,
                -68 + random.uniform(-4, 4),
                9.0 + random.uniform(-2, 2),
                now,
            )
        elif roll < 0.70:
            self._emit(
                position_packet(0x0B2C3D4E, 39.7401 + random.uniform(-0.0002, 0.0002), -104.9890, 1612, self._pid()),
                longfast,
                -90 + random.uniform(-5, 3),
                4.0 + random.uniform(-1, 1),
                now,
            )
        elif roll < 0.90:
            self._fcnt += 1
            lora = self._profile(902.3, 9, 125.0, 0x34, "lorawan", "LoRaWAN 902.300 MHz · SF9/BW125")
            self._emit(
                lorawan_uplink(0x260B1A2F, self._fcnt, 1, cayenne_climate(21.0 + random.uniform(-0.4, 0.4), 47.0, 3.21)),
                lora,
                -104 + random.uniform(-6, 4),
                5.5 + random.uniform(-1, 1),
                now,
            )
        else:
            raw = self._profile(906.875, 9, 125.0, 0x12, "raw", "Private LoRa 906.875 MHz · SF9/BW125")
            self._emit(
                lora_sync(f"SENSOR-7 {21.0 + random.uniform(-0.5, 0.5):.1f}C 48% 3.20V"),
                raw,
                -83 + random.uniform(-5, 3),
                5.5 + random.uniform(-1, 1),
                now,
            )
        self.ingest_signal(
            {
                "ts": now,
                "rssi_dbm": -109 + random.uniform(-8, 5),
                "detected": True,
                "profile": self._profile(903.9, 9, 125.0, 0x34, "lorawan", "LoRaWAN 903.900 MHz · SF9/BW125"),
            }
        )

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="lora95-demo", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
