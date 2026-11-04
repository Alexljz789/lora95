"""Flask web UI and JSON API for LoRa95.

The web process runs as the desktop user.  It owns the SQLite device store and
talks to the root radio daemon over a Unix socket.  All decoding happens here
so the radio daemon stays a small, robust hardware service.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import socket
import subprocess
import threading
import time
from collections import deque
from typing import Any, Optional

from flask import Flask, Response, jsonify, request, send_from_directory

from . import __version__, profiles
from .demo import DemoFeed
from .store import Store

LOG = logging.getLogger("lora95.web")
APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, "static")
DEFAULT_SOCKET = os.environ.get("LORA95_SOCKET", "/run/lora95/radio.sock")
DEFAULT_CONFIG = os.path.expanduser("~/.config/lora95/config.json")
DEFAULT_DB = os.path.expanduser("~/.local/share/lora95/lora95.db")

DEFAULT_SETTINGS: dict[str, Any] = {
    "region": "US",
    "mode": "quick",
    "dwell": 1.2,
    "min_rssi": -140,
    "cad": False,
    "include_raw": True,
    "custom_frequency_mhz": 906.875,
    "custom_sf": 11,
    "custom_bw_khz": 250,
    "custom_cr": 5,
    "custom_sync_word": 0x2B,
    "custom_preamble": 16,
    "sort": "last",
    "filter": "",
    "theme": "classic",
    "auto_scan": True,
}


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = json.loads(json.dumps(base))
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_settings(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return _deep_merge(DEFAULT_SETTINGS, json.load(fh))
    except Exception:
        return dict(DEFAULT_SETTINGS)


def save_settings(path: str, settings: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(settings, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


class Notifier:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._version = 0
        self._lock = threading.Lock()

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    def notify(self) -> None:
        with self._lock:
            self._version += 1
        self._event.set()

    def wait(self, version: int, timeout: float = 20.0) -> bool:
        if self.version != version:
            return True
        self._event.wait(timeout)
        self._event.clear()
        return self.version != version


class RadioLink:
    """Persistent client for the root radio daemon's Unix socket."""

    def __init__(self, socket_path: str, on_event) -> None:
        self.socket_path = socket_path
        self.on_event = on_event
        self._sock: Optional[socket.socket] = None
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._pending: dict[int, tuple[threading.Event, dict[str, Any]]] = {}
        self._next_id = 1
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.connected = False
        self.last_error: Optional[str] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="lora95-link", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            if self._sock:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self.connected:
                try:
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    sock.settimeout(5.0)
                    sock.connect(self.socket_path)
                    sock.settimeout(None)
                    with self._lock:
                        self._sock = sock
                        self.connected = True
                        self.last_error = None
                    self.on_event({"type": "link", "connected": True})
                    self._reader(sock)
                except Exception as exc:
                    self.last_error = str(exc)
                    with self._lock:
                        self._sock = None
                        if self.connected:
                            self.on_event({"type": "link", "connected": False, "error": str(exc)})
                        self.connected = False
                    self._fail_pending(str(exc))
                finally:
                    with self._lock:
                        if self._sock:
                            try:
                                self._sock.close()
                            except OSError:
                                pass
                        self._sock = None
                    if self.connected:
                        self.connected = False
                        self.on_event({"type": "link", "connected": False})
            self._stop.wait(1.5)

    def _reader(self, sock: socket.socket) -> None:
        buffer = b""
        while not self._stop.is_set():
            chunk = sock.recv(262144)
            if not chunk:
                return
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    event = json.loads(line.decode("utf-8"))
                except Exception:
                    continue
                if event.get("type") == "result" and event.get("id") in self._pending:
                    with self._lock:
                        pending = self._pending.pop(event["id"], None)
                    if pending:
                        pending[1].update(event)
                        pending[0].set()
                    continue
                try:
                    self.on_event(event)
                except Exception:
                    LOG.exception("event handler failed")

    def _fail_pending(self, error: str) -> None:
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for wait, response in pending:
            response.update({"ok": False, "error": error})
            wait.set()

    def request(self, command: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
        with self._lock:
            if not self.connected or self._sock is None:
                return {"ok": False, "error": self.last_error or "radio daemon is not connected"}
            ident = self._next_id
            self._next_id += 1
            event = threading.Event()
            response: dict[str, Any] = {}
            self._pending[ident] = (event, response)
            sock = self._sock
        payload = dict(command)
        payload["id"] = ident
        try:
            with self._write_lock:
                sock.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        except Exception as exc:
            with self._lock:
                self._pending.pop(ident, None)
            return {"ok": False, "error": f"send failed: {exc}"}
        if not event.wait(timeout):
            with self._lock:
                self._pending.pop(ident, None)
            return {"ok": False, "error": "radio daemon did not answer"}
        return response


class AppContext:
    def __init__(self, socket_path: str, config_path: str, db_path: str, demo: bool = False) -> None:
        self.demo = demo
        self.socket_path = socket_path
        self.config_path = config_path
        self.settings = load_settings(config_path)
        self.store = Store(db_path, demo=demo)
        self.notifier = Notifier()
        self.logs: deque[dict[str, Any]] = deque(maxlen=200)
        self.scan: dict[str, Any] = {
            "running": False,
            "monitor": False,
            "mode": self.settings.get("mode", "quick"),
            "region": self.settings.get("region", "US"),
            "index": 0,
            "total": 0,
            "round": 0,
            "profile": None,
            "packets": 0,
            "signals": 0,
            "dwell": self.settings.get("dwell", 1.2),
        }
        self.radio: dict[str, Any] = {
            "open": False,
            "meshtasticd": "unknown",
            "radio": None,
        }
        self.link = RadioLink(socket_path, self._on_event)
        self.demo_feed: Optional[DemoFeed] = None
        if demo:
            self.radio = {
                "open": True,
                "meshtasticd": "inactive",
                "radio": {"chip": "SX1262 (DEMO)", "max_power_dbm": 22, "mode": "rx"},
            }
            self.scan.update({
                "running": True,
                "total": 0,
                "profile": {
                    "id": "demo:longfast",
                    "label": "DEMO · Meshtastic LongFast 906.875 MHz",
                    "frequency_mhz": 906.875,
                    "sf": 11,
                    "bw_khz": 250,
                    "cr": 5,
                    "sync_word": 0x2B,
                },
            })
            self.demo_feed = DemoFeed(self.store.ingest_packet, self.store.ingest_signal)
            self.demo_feed.start()
            self.log("DEMO mode: synthetic Meshtastic, LoRaWAN and proprietary traffic.", "warn")
        else:
            self.link.start()
            self.log("LoRa95 web UI started; waiting for the radio daemon.")
        self.notifier.notify()

    # -- logging -----------------------------------------------------------
    def log(self, message: str, level: str = "info") -> None:
        entry = {"ts": time.time(), "level": level, "message": message}
        self.logs.append(entry)
        LOG.log(
            {"error": logging.ERROR, "warn": logging.WARNING, "ok": logging.INFO}.get(level, logging.INFO),
            "%s",
            message,
        )
        self.notifier.notify()

    # -- daemon events -----------------------------------------------------
    def _on_event(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "link":
            if not event.get("connected"):
                self.radio["open"] = False
            self.log(
                "Radio daemon connected." if event.get("connected") else f"Radio daemon offline: {event.get('error', '')}",
                "ok" if event.get("connected") else "warn",
            )
            self.notifier.notify()
            return
        if kind == "hello":
            self.log(f"Radio daemon {event.get('version')} (pid {event.get('pid')})", "ok")
            return
        if kind == "status":
            self.radio = event.get("radio") or self.radio
            self.scan = event.get("scan") or self.scan
            self.notifier.notify()
            return
        if kind == "scan":
            self.scan = event.get("scan") or self.scan
            self.notifier.notify()
            return
        if kind == "packet":
            packet = event.get("packet") or {}
            try:
                self.store.ingest_packet(packet)
            except Exception:
                LOG.exception("failed to store packet")
            self.notifier.notify()
            return
        if kind == "signal":
            signal = event.get("signal") or {}
            try:
                self.store.ingest_signal(signal)
            except Exception:
                LOG.exception("failed to store signal")
            self.notifier.notify()
            return
        if kind == "log":
            self.log(str(event.get("message") or ""), str(event.get("level") or "info"))
            return
        if kind == "error":
            self.log(str(event.get("error") or "radio error"), "error")
            return

    # -- operations --------------------------------------------------------
    def ensure_radio(self) -> Optional[str]:
        if self.demo:
            return None
        if self.link.connected:
            return None
        try:
            proc = subprocess.run(
                ["sudo", "-n", "systemctl", "start", "lora95-radio.service"],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            if proc.returncode != 0:
                return (proc.stderr or proc.stdout or "could not start lora95-radio.service").strip()
        except Exception as exc:
            return str(exc)
        for _ in range(30):
            if self.link.connected:
                return None
            time.sleep(0.3)
        return self.link.last_error or "radio daemon did not become ready"

    def start_scan(self, patch: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        self.settings = _deep_merge(self.settings, patch or {})
        self.settings["dwell"] = max(0.3, min(8.0, float(self.settings.get("dwell", 1.2))))
        self.settings["min_rssi"] = max(-150.0, min(-20.0, float(self.settings.get("min_rssi", -140))))
        save_settings(self.config_path, self.settings)
        if self.demo:
            self.scan = {
                "running": True,
                "monitor": False,
                "mode": self.settings["mode"],
                "region": self.settings["region"],
                "index": 0,
                "total": 0,
                "round": 0,
                "profile": {
                    "id": "demo:longfast",
                    "label": "DEMO · Meshtastic LongFast 906.875 MHz",
                    "frequency_mhz": 906.875,
                    "sf": 11,
                    "bw_khz": 250,
                    "cr": 5,
                    "sync_word": 0x2B,
                },
                "packets": self.scan.get("packets", 0),
                "dwell": self.settings["dwell"],
            }
            if not self.demo_feed:
                self.demo_feed = DemoFeed(self.store.ingest_packet, self.store.ingest_signal)
            self.demo_feed.start()
            self.log("DEMO scan started.")
            self.notifier.notify()
            return True, self.scan
        error = self.ensure_radio()
        if error:
            return False, {"error": error}
        response = self.link.request({"cmd": "scan", "settings": self.settings}, timeout=15)
        if not response.get("ok"):
            return False, {"error": response.get("error", "scan failed")}
        self.scan = (response.get("data") or {}).get("scan") or self.scan
        return True, self.scan

    def stop_scan(self) -> dict[str, Any]:
        if self.demo:
            if self.demo_feed:
                self.demo_feed.stop()
            self.scan["running"] = False
            self.notifier.notify()
            return self.scan
        if not self.link.connected:
            self.scan["running"] = False
            return self.scan
        response = self.link.request({"cmd": "stop"}, timeout=15)
        if response.get("ok"):
            self.scan = (response.get("data") or {}).get("scan") or self.scan
        self.notifier.notify()
        return self.scan

    def monitor(self, profile: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        if self.demo:
            self.scan["monitor"] = True
            self.scan["profile"] = profile
            self.notifier.notify()
            return True, self.scan
        error = self.ensure_radio()
        if error:
            return False, {"error": error}
        response = self.link.request({"cmd": "monitor", "profile": profile}, timeout=15)
        if not response.get("ok"):
            return False, {"error": response.get("error", "monitor failed")}
        self.scan = (response.get("data") or {}).get("scan") or self.scan
        return True, self.scan

    def state(self) -> dict[str, Any]:
        stats = self.store.stats()
        scan = dict(self.scan)
        radio = dict(self.radio or {})
        if self.demo:
            scan["packets"] = stats.get("packets", 0)
            scan["signals"] = int((stats.get("kinds") or {}).get("signal", 0))
        elif scan.get("running"):
            radio["open"] = True
            radio["meshtasticd"] = "inactive"
            if not radio.get("radio"):
                radio["radio"] = {"chip": "SX126x", "max_power_dbm": 15, "mode": "rx"}
        return {
            "version": __version__,
            "demo": self.demo,
            "connected": self.demo or self.link.connected,
            "link_error": self.link.last_error,
            "radio": radio,
            "scan": scan,
            "settings": self.settings,
            "stats": stats,
            "devices": self.store.snapshot(),
            "logs": list(self.logs)[-60:],
            "server_time": time.time(),
        }


def create_app(ctx: AppContext) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["JSON_AS_ASCII"] = False

    @app.after_request
    def _headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/static/<path:filename>")
    def static_files(filename: str):
        return send_from_directory(STATIC_DIR, filename, max_age=3600)

    @app.get("/api/health")
    def health():
        return jsonify(
            {
                "ok": True,
                "version": __version__,
                "demo": ctx.demo,
                "connected": ctx.demo or ctx.link.connected,
            }
        )

    @app.get("/api/state")
    def state():
        return jsonify(ctx.state())

    @app.get("/api/device/<path:device_id>")
    def device(device_id: str):
        result = ctx.store.device(device_id)
        if not result:
            return jsonify({"ok": False, "error": "device not found"}), 404
        return jsonify({"ok": True, "device": result})

    @app.post("/api/scan/start")
    def scan_start():
        patch = request.get_json(silent=True) or {}
        ok, data = ctx.start_scan(patch)
        return (jsonify({"ok": True, "scan": data}) if ok else (jsonify({"ok": False, "error": data.get("error")}), 502))

    @app.post("/api/scan/stop")
    def scan_stop():
        return jsonify({"ok": True, "scan": ctx.stop_scan()})

    @app.post("/api/monitor")
    def monitor():
        body = request.get_json(silent=True) or {}
        profile = body.get("profile") or {}
        if not profile.get("frequency_mhz"):
            return jsonify({"ok": False, "error": "profile is required"}), 400
        ok, data = ctx.monitor(profile)
        return (jsonify({"ok": True, "scan": data}) if ok else (jsonify({"ok": False, "error": data.get("error")}), 502))

    @app.post("/api/radio/start")
    def radio_start():
        error = ctx.ensure_radio()
        return (jsonify({"ok": False, "error": error}), 502) if error else jsonify({"ok": True, "connected": True})

    @app.post("/api/radio/stop")
    def radio_stop():
        if ctx.demo:
            return jsonify({"ok": True, "demo": True})
        ctx.stop_scan()
        try:
            subprocess.run(
                ["sudo", "-n", "systemctl", "stop", "lora95-radio.service"],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500
        return jsonify({"ok": True})

    @app.post("/api/settings")
    def update_settings():
        body = request.get_json(silent=True) or {}
        ctx.settings = _deep_merge(ctx.settings, body)
        ctx.settings["dwell"] = max(0.3, min(8.0, float(ctx.settings.get("dwell", 1.2))))
        ctx.settings["min_rssi"] = max(-150.0, min(-20.0, float(ctx.settings.get("min_rssi", -140))))
        save_settings(ctx.config_path, ctx.settings)
        ctx.notifier.notify()
        return jsonify({"ok": True, "settings": ctx.settings})

    @app.post("/api/clear")
    def clear():
        ctx.store.clear()
        ctx.log("Device list cleared.")
        ctx.notifier.notify()
        return jsonify({"ok": True})

    @app.delete("/api/device/<path:device_id>")
    def remove_device(device_id: str):
        ok = ctx.store.remove(device_id)
        ctx.notifier.notify()
        return jsonify({"ok": ok})

    @app.get("/api/profiles")
    def profile_preview():
        region = request.args.get("region", ctx.settings.get("region", "US"))
        mode = request.args.get("mode", ctx.settings.get("mode", "quick"))
        items = profiles.build_profiles(region=region, mode=mode)
        return jsonify(
            {
                "ok": True,
                "count": len(items),
                "items": [p.to_dict() for p in items[:600]],
                "regions": profiles.region_options(),
            }
        )

    @app.get("/api/export.json")
    def export_json():
        payload = ctx.store.export()
        data = json.dumps(payload, ensure_ascii=False, indent=2)
        return Response(
            data,
            mimetype="application/json",
            headers={"Content-Disposition": "attachment; filename=lora95-export.json"},
        )

    @app.get("/api/export.csv")
    def export_csv():
        rows = ctx.store.snapshot()
        buffer = io.StringIO()
        buffer.write("\ufeff")
        writer = csv.writer(buffer)
        writer.writerow([
            "id", "kind", "name", "model", "node_id", "dev_addr", "dev_eui",
            "rssi_dbm", "snr_db", "packet_count", "first_seen", "last_seen",
            "latitude", "longitude", "summary",
        ])
        for row in rows:
            writer.writerow([
                row.get("id"), row.get("kind"), row.get("name"), row.get("model"),
                row.get("node_id"), row.get("dev_addr"), row.get("dev_eui"),
                row.get("rssi_dbm"), row.get("snr_db"), row.get("packet_count"),
                row.get("first_seen"), row.get("last_seen"),
                row.get("latitude"), row.get("longitude"), row.get("summary"),
            ])
        return Response(
            buffer.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=lora95-devices.csv"},
        )

    @app.get("/api/events")
    def events():
        def generate():
            last = -1
            while True:
                version = ctx.notifier.version
                if version != last:
                    payload = json.dumps(ctx.state(), ensure_ascii=False, separators=(",", ":"))
                    yield f"data: {payload}\n\n"
                    last = version
                if not ctx.notifier.wait(last, 20.0):
                    yield ": keepalive\n\n"

        return Response(
            generate(),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )

    return app


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="LoRa95 web UI")
    parser.add_argument("--host", default=os.environ.get("LORA95_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("LORA95_PORT", "8766")))
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument("--config", default=os.environ.get("LORA95_CONFIG", DEFAULT_CONFIG))
    parser.add_argument("--db", default=os.environ.get("LORA95_DB", DEFAULT_DB))
    parser.add_argument("--demo", action="store_true", default=os.environ.get("LORA95_DEMO") == "1")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    db_path = args.db
    if args.demo:
        root, ext = os.path.splitext(args.db)
        db_path = f"{root}-demo{ext or '.db'}"
    ctx = AppContext(args.socket, args.config, db_path, demo=args.demo)
    app = create_app(ctx)
    LOG.info("LoRa95 UI on http://%s:%s (demo=%s)", args.host, args.port, args.demo)
    app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
