"""Device/packet store for LoRa95.

The radio daemon is deliberately stateless: it emits raw frames.  The web
process owns this store (SQLite + an in-memory view), which keeps decoding and
history independent of the root-owned radio service and lets the UI survive a
daemon restart.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections import deque
from typing import Any, Optional

from . import classify

MAX_DEVICE_PACKETS = 400
MAX_MESSAGES = 250
MAX_DB_PACKETS = 30000


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _default_device(classified: dict[str, Any], ts: float) -> dict[str, Any]:
    return {
        "id": classified["device_key"],
        "kind": classified["kind"],
        "name": classified.get("device_name") or "Unknown device",
        "model": classified.get("device_model") or "Unknown model",
        "first_seen": ts,
        "last_seen": ts,
        "packet_count": 0,
        "decoded_count": 0,
        "encrypted_count": 0,
        "rssi_dbm": None,
        "snr_db": None,
        "best_rssi": None,
        "worst_rssi": None,
        "last_profile": None,
        "profiles": [],
        "node_id": None,
        "short_name": None,
        "role": None,
        "hw_model": None,
        "macaddr": None,
        "public_key": None,
        "firmware": None,
        "dev_addr": None,
        "dev_eui": None,
        "join_eui": None,
        "fcnt": None,
        "fport": None,
        "cayenne": [],
        "battery": None,
        "voltage": None,
        "uptime": None,
        "latitude": None,
        "longitude": None,
        "altitude": None,
        "position_time": None,
        "telemetry": {},
        "messages": [],
        "packets": [],
        "latest": None,
        "summary": classified.get("summary") or "",
        "text": classified.get("text"),
        "encrypted": bool(classified.get("encrypted")),
        "candidate": bool(classified.get("candidate")),
        "first_seen_iso": _iso(ts),
        "last_seen_iso": _iso(ts),
    }


class Store:
    def __init__(self, path: str, demo: bool = False) -> None:
        self.path = path
        self.demo = demo
        self._lock = threading.RLock()
        self._devices: dict[str, dict[str, Any]] = {}
        self._packet_times: deque[float] = deque(maxlen=600)
        self._started = time.time()
        self._total_packets = 0
        self._signals = 0
        self._db: Optional[sqlite3.Connection] = None
        self._open_db()
        self._load()

    # -- persistence -------------------------------------------------------
    def _open_db(self) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY,
                json TEXT NOT NULL,
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL,
                packet_count INTEGER NOT NULL DEFAULT 0
            )"""
        )
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS packets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                ts REAL NOT NULL,
                json TEXT NOT NULL
            )"""
        )
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_packets_ts ON packets(ts)")
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_packets_device ON packets(device_id, ts)"
        )
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                try:
                    self._db.commit()
                    self._db.close()
                except Exception:
                    pass
                self._db = None

    def _load(self) -> None:
        assert self._db is not None
        rows = self._db.execute(
            "SELECT id, json FROM devices ORDER BY last_seen DESC LIMIT 500"
        ).fetchall()
        for device_id, blob in rows:
            try:
                device = json.loads(blob)
            except Exception:
                continue
            device["packets"] = []
            self._devices[device_id] = device
        self._total_packets = self._db.execute("SELECT COUNT(*) FROM packets").fetchone()[0]
        # The rate meter starts empty after a restart.

    def _persist_device(self, device: dict[str, Any]) -> None:
        assert self._db is not None
        # Packets live in their own table; do not duplicate up to 400 of them
        # in the device blob on every update.
        lean = dict(device)
        lean["packets"] = []
        blob = json.dumps(lean, ensure_ascii=False)
        self._db.execute(
            "INSERT INTO devices(id, json, first_seen, last_seen, packet_count) "
            "VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "json=excluded.json, last_seen=excluded.last_seen, packet_count=excluded.packet_count",
            (
                device["id"],
                blob,
                device["first_seen"],
                device["last_seen"],
                device["packet_count"],
            ),
        )

    def _persist_packet(self, device_id: str, ts: float, packet: dict[str, Any]) -> None:
        assert self._db is not None
        self._db.execute(
            "INSERT INTO packets(device_id, ts, json) VALUES(?,?,?)",
            (device_id, ts, json.dumps(packet, ensure_ascii=False)),
        )
        self._total_packets += 1
        if self._total_packets % 200 == 0:
            self._db.execute(
                "DELETE FROM packets WHERE id IN "
                "(SELECT id FROM packets ORDER BY id ASC LIMIT 2000)"
            )

    # -- ingestion ---------------------------------------------------------
    def ingest_packet(self, event: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
        """Classify and store one raw packet event from the radio daemon."""
        with self._lock:
            try:
                payload = bytes.fromhex(event.get("data_hex") or "")
            except ValueError:
                payload = b""
            profile = event.get("profile") or {
                "id": "custom",
                "frequency_mhz": event.get("frequency_mhz"),
                "sf": event.get("sf"),
                "bw_khz": event.get("bw_khz"),
                "cr": event.get("cr"),
                "sync_word": event.get("sync_word"),
                "label": "Custom",
                "protocol": "auto",
            }
            classified = classify.classify(payload, profile)
            ts = float(event.get("ts") or time.time())
            key = classified["device_key"]
            device = self._devices.get(key)
            if device is None:
                device = _default_device(classified, ts)
                self._devices[key] = device
            self._update_device(device, classified, event, payload, profile, ts)
            packet = self._packet_entry(classified, event, payload, profile, ts)
            device["packets"].append(packet)
            if len(device["packets"]) > MAX_DEVICE_PACKETS:
                device["packets"] = device["packets"][-MAX_DEVICE_PACKETS:]
            device["latest"] = packet
            device["summary"] = packet.get("summary") or device["summary"]
            device["last_seen"] = ts
            device["last_seen_iso"] = _iso(ts)
            device["packet_count"] += 1
            if classified.get("decoded"):
                device["decoded_count"] += 1
            if classified.get("encrypted"):
                device["encrypted_count"] += 1
            if persist:
                self._persist_device(device)
                self._persist_packet(key, ts, packet)
                assert self._db is not None
                self._db.commit()
            self._packet_times.append(ts)
            return device

    def ingest_signal(self, event: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
        with self._lock:
            profile = event.get("profile") or {}
            ts = float(event.get("ts") or time.time())
            signal = classify.cad_signal(
                profile,
                float(event.get("rssi_dbm") or -120.0),
                bool(event.get("detected", True)),
            )
            key = signal["device_key"]
            device = self._devices.get(key)
            if device is None:
                device = _default_device(signal, ts)
                self._devices[key] = device
            device["kind"] = "signal"
            device["name"] = signal["device_name"]
            device["model"] = signal["device_model"]
            device["last_seen"] = ts
            device["last_seen_iso"] = _iso(ts)
            device["packet_count"] += 1
            device["rssi_dbm"] = round(float(event.get("rssi_dbm") or -120.0), 1)
            best = device.get("best_rssi")
            if best is None or device["rssi_dbm"] > best:
                device["best_rssi"] = device["rssi_dbm"]
            device["last_profile"] = profile
            if profile:
                device["profiles"] = [profile]
            device["summary"] = signal["summary"]
            packet = {
                "ts": ts,
                "ts_iso": _iso(ts),
                "kind": "signal",
                "summary": signal["summary"],
                "rssi_dbm": device["rssi_dbm"],
                "snr_db": None,
                "profile": profile,
                "profile_id": profile.get("id"),
                "frequency_mhz": profile.get("frequency_mhz"),
                "sf": profile.get("sf"),
                "bw_khz": profile.get("bw_khz"),
                "length": 0,
                "hex": "",
                "decoded": False,
            }
            device["packets"].append(packet)
            if len(device["packets"]) > MAX_DEVICE_PACKETS:
                device["packets"] = device["packets"][-MAX_DEVICE_PACKETS:]
            device["latest"] = packet
            if persist:
                self._persist_device(device)
                self._persist_packet(key, ts, packet)
                assert self._db is not None
                self._db.commit()
            self._signals += 1
            return device

    def _update_device(
        self,
        device: dict[str, Any],
        classified: dict[str, Any],
        event: dict[str, Any],
        payload: bytes,
        profile: dict[str, Any],
        ts: float,
    ) -> None:
        device["last_profile"] = profile
        if profile:
            existing = {p.get("id"): p for p in device.get("profiles", []) if isinstance(p, dict)}
            existing[profile.get("id")] = profile
            device["profiles"] = list(existing.values())[-12:]
        rssi = event.get("rssi_dbm")
        snr = event.get("snr_db")
        if rssi is not None:
            device["rssi_dbm"] = round(float(rssi), 1)
            if device.get("best_rssi") is None or device["rssi_dbm"] > device["best_rssi"]:
                device["best_rssi"] = device["rssi_dbm"]
            if device.get("worst_rssi") is None or device["rssi_dbm"] < device["worst_rssi"]:
                device["worst_rssi"] = device["rssi_dbm"]
        if snr is not None:
            device["snr_db"] = round(float(snr), 1)
        device["text"] = classified.get("text")
        kind = classified.get("kind")
        if kind == "meshtastic":
            packet = classified.get("packet") or {}
            data = packet.get("data") or {}
            device["node_id"] = f"!{packet.get('src', 0):08x}"
            device["candidate"] = bool(classified.get("candidate"))
            node = data.get("nodeinfo")
            if node:
                if node.get("long_name"):
                    device["name"] = node["long_name"]
                if node.get("short_name"):
                    device["short_name"] = node["short_name"]
                if node.get("hw_model_name"):
                    device["model"] = node["hw_model_name"]
                device["role"] = node.get("role_name")
                device["hw_model"] = node.get("hw_model_name")
                device["macaddr"] = node.get("macaddr")
                device["public_key"] = node.get("public_key")
            if data.get("position"):
                pos = data["position"]
                device["latitude"] = pos.get("latitude")
                device["longitude"] = pos.get("longitude")
                device["altitude"] = pos.get("altitude")
                device["position_time"] = pos.get("timestamp") or pos.get("time")
            if data.get("telemetry"):
                telemetry = data["telemetry"]
                merged = dict(device.get("telemetry") or {})
                merged.update(telemetry)
                device["telemetry"] = merged
                metrics = telemetry.get("device_metrics") or {}
                if metrics.get("battery_level") is not None:
                    device["battery"] = metrics["battery_level"]
                if metrics.get("voltage") is not None:
                    device["voltage"] = metrics["voltage"]
                if metrics.get("uptime_seconds") is not None:
                    device["uptime"] = metrics["uptime_seconds"]
            if data.get("text"):
                message = {
                    "ts": ts,
                    "ts_iso": _iso(ts),
                    "text": data["text"],
                    "rssi_dbm": device.get("rssi_dbm"),
                    "snr_db": device.get("snr_db"),
                    "packet_id": packet.get("packet_id"),
                    "from": f"!{packet.get('src', 0):08x}",
                    "to": f"!{packet.get('dst', 0):08x}" if packet.get("dst") != 0xFFFFFFFF else "broadcast",
                    "channel": packet.get("channel"),
                }
                device["messages"].append(message)
                if len(device["messages"]) > MAX_MESSAGES:
                    device["messages"] = device["messages"][-MAX_MESSAGES:]
            if data.get("neighborinfo"):
                device["neighbors"] = data["neighborinfo"].get("neighbors")
            if data.get("traceroute"):
                device["traceroute"] = data["traceroute"]
        elif kind == "lorawan":
            frame = classified.get("frame") or {}
            device["dev_addr"] = frame.get("dev_addr")
            if frame.get("dev_eui"):
                device["dev_eui"] = frame["dev_eui"]
            if frame.get("join_eui"):
                device["join_eui"] = frame["join_eui"]
            if frame.get("fcnt") is not None:
                device["fcnt"] = frame["fcnt"]
            if frame.get("fport") is not None:
                device["fport"] = frame["fport"]
            if frame.get("cayenne"):
                device["cayenne"] = frame["cayenne"]
                device["cayenne_at"] = _iso(ts)
            if frame.get("dev_addr") and not frame.get("dev_eui"):
                device["name"] = f"LoRaWAN {frame['dev_addr']}"
            elif frame.get("dev_eui"):
                device["name"] = f"LoRaWAN join {frame['dev_eui']}"
        elif kind == "raw":
            device["raw_kind"] = "proprietary"
            device["text"] = classified.get("text")
            device["json"] = classified.get("json")
        if classified.get("kind") == "meshtastic" and not classified.get("decoded"):
            device["encrypted"] = True

    def _packet_entry(
        self,
        classified: dict[str, Any],
        event: dict[str, Any],
        payload: bytes,
        profile: dict[str, Any],
        ts: float,
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "ts": ts,
            "ts_iso": _iso(ts),
            "kind": classified.get("kind"),
            "decoded": bool(classified.get("decoded")),
            "summary": classified.get("summary") or "",
            "rssi_dbm": round(float(event.get("rssi_dbm")), 1) if event.get("rssi_dbm") is not None else None,
            "snr_db": round(float(event.get("snr_db")), 1) if event.get("snr_db") is not None else None,
            "length": len(payload),
            "hex": payload.hex().upper(),
            "ascii": "".join(chr(b) if 32 <= b < 127 else "." for b in payload),
            "profile": profile,
            "profile_id": profile.get("id"),
            "profile_label": profile.get("label"),
            "frequency_mhz": profile.get("frequency_mhz"),
            "sf": profile.get("sf"),
            "bw_khz": profile.get("bw_khz"),
            "cr": profile.get("cr"),
            "sync_word": profile.get("sync_word"),
            "text": classified.get("text"),
            "cayenne": classified.get("cayenne"),
        }
        if classified.get("kind") == "meshtastic":
            packet = classified.get("packet") or {}
            data = packet.get("data") or {}
            entry["node"] = f"!{packet.get('src', 0):08x}"
            entry["dst"] = packet.get("dst")
            entry["portnum"] = data.get("portnum")
            entry["port_name"] = data.get("port_name")
            entry["packet_id"] = packet.get("packet_id")
            entry["encrypted"] = packet.get("encrypted")
            if data.get("position"):
                entry["position"] = data["position"]
            if data.get("telemetry"):
                entry["telemetry"] = data["telemetry"]
            if data.get("nodeinfo"):
                entry["nodeinfo"] = data["nodeinfo"]
        elif classified.get("kind") == "lorawan":
            frame = classified.get("frame") or {}
            entry["dev_addr"] = frame.get("dev_addr")
            entry["dev_eui"] = frame.get("dev_eui")
            entry["mtype"] = frame.get("mtype_name")
            entry["fcnt"] = frame.get("fcnt")
            entry["fport"] = frame.get("fport")
            entry["cayenne"] = frame.get("cayenne")
            entry["summary"] = classified.get("summary") or ""
        return entry

    # -- reading -----------------------------------------------------------
    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            devices = []
            for device in self._devices.values():
                devices.append(
                    {
                        "id": device["id"],
                        "kind": device["kind"],
                        "name": device["name"],
                        "model": device["model"],
                        "summary": device.get("summary"),
                        "last_seen": device["last_seen"],
                        "last_seen_iso": device.get("last_seen_iso"),
                        "first_seen": device.get("first_seen"),
                        "packet_count": device.get("packet_count", 0),
                        "rssi_dbm": device.get("rssi_dbm"),
                        "snr_db": device.get("snr_db"),
                        "best_rssi": device.get("best_rssi"),
                        "node_id": device.get("node_id"),
                        "dev_addr": device.get("dev_addr"),
                        "dev_eui": device.get("dev_eui"),
                        "encrypted": device.get("encrypted"),
                        "candidate": device.get("candidate"),
                        "battery": device.get("battery"),
                        "latitude": device.get("latitude"),
                        "longitude": device.get("longitude"),
                        "profile": device.get("last_profile"),
                    }
                )
            devices.sort(key=lambda d: (d.get("last_seen") or 0), reverse=True)
            return devices

    def device(self, device_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            device = self._devices.get(device_id)
            if not device:
                return None
            out = dict(device)
            packets = list(device.get("packets", []))
            if not packets and self._db is not None:
                rows = self._db.execute(
                    "SELECT json FROM packets WHERE device_id=? ORDER BY id DESC LIMIT ?",
                    (device_id, MAX_DEVICE_PACKETS),
                ).fetchall()
                packets = []
                for (blob,) in rows:
                    try:
                        packets.append(json.loads(blob))
                    except Exception:
                        continue
                packets.reverse()
            out["packets"] = packets
            out["messages"] = list(device.get("messages", []))
            return out

    def recent_packets(self, limit: int = 100) -> list[dict[str, Any]]:
        assert self._db is not None
        with self._lock:
            rows = self._db.execute(
                "SELECT json FROM packets ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            packets = []
            for (blob,) in rows:
                try:
                    packets.append(json.loads(blob))
                except Exception:
                    continue
            return packets

    def stats(self) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            recent = [t for t in self._packet_times if now - t <= 60.0]
            kinds: dict[str, int] = {}
            for device in self._devices.values():
                kinds[device["kind"]] = kinds.get(device["kind"], 0) + 1
            return {
                "devices": len(self._devices),
                "kinds": kinds,
                "packets": self._total_packets,
                "packets_per_min": len(recent),
                "signals": self._signals,
                "uptime": int(now - self._started),
                "demo": self.demo,
                "db_path": self.path,
            }

    def clear(self) -> None:
        with self._lock:
            self._devices.clear()
            self._packet_times.clear()
            self._total_packets = 0
            self._signals = 0
            assert self._db is not None
            self._db.execute("DELETE FROM packets")
            self._db.execute("DELETE FROM devices")
            self._db.commit()

    def remove(self, device_id: str) -> bool:
        with self._lock:
            if device_id not in self._devices:
                return False
            del self._devices[device_id]
            assert self._db is not None
            self._db.execute("DELETE FROM devices WHERE id=?", (device_id,))
            self._db.execute("DELETE FROM packets WHERE device_id=?", (device_id,))
            self._db.commit()
            return True

    def export(self) -> dict[str, Any]:
        with self._lock:
            return {
                "version": 1,
                "exported_at": _iso(time.time()),
                "demo": self.demo,
                "devices": [dict(d) for d in self._devices.values()],
                "stats": self.stats(),
            }
