"""LoRa95 radio daemon.

Runs as root because the SX1262 shares SPI1/GPIO with meshtasticd and because
toggling the AIO v2 LoRa power rail needs privileged GPIO access.  It exposes a
small newline-delimited JSON protocol on a local Unix socket; the web UI runs
as the normal user and connects to it.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import sys
import threading
import time
from typing import Any, Optional

from . import __version__
from .scanner import RadioManager, ScanEngine

LOG = logging.getLogger("lora95.radio")
DEFAULT_SOCKET = "/run/lora95/radio.sock"


class RadioDaemon:
    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path
        self._clients: set[socket.socket] = set()
        self._client_lock = threading.Lock()
        self._shutdown = threading.Event()
        self._server: Optional[socket.socket] = None
        self.manager = RadioManager(self.broadcast)
        self.engine = ScanEngine(self.manager, self.broadcast)

    # -- client plumbing ---------------------------------------------------
    def send(self, client: socket.socket, message: dict[str, Any]) -> bool:
        try:
            data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
            client.sendall(data)
            return True
        except OSError:
            return False

    def broadcast(self, message: dict[str, Any]) -> None:
        with self._client_lock:
            clients = list(self._clients)
        dead = []
        for client in clients:
            if not self.send(client, message):
                dead.append(client)
        if dead:
            with self._client_lock:
                for client in dead:
                    self._clients.discard(client)
                    try:
                        client.close()
                    except OSError:
                        pass

    def _hello(self) -> dict[str, Any]:
        return {
            "type": "hello",
            "version": __version__,
            "pid": os.getpid(),
            "socket": self.socket_path,
            "ts": time.time(),
        }

    def _status(self) -> dict[str, Any]:
        return {
            "type": "status",
            "version": __version__,
            "radio": self.manager.info(),
            "scan": self.engine.status(),
            "ts": time.time(),
        }

    # -- commands ----------------------------------------------------------
    def handle_command(self, command: dict[str, Any]) -> dict[str, Any]:
        cmd = str(command.get("cmd") or "").strip().lower()
        if cmd in ("ping", "hello"):
            return {"ok": True, "data": {"pong": time.time()}}
        if cmd == "status":
            return {"ok": True, "data": self._status()}
        if cmd == "scan":
            settings = command.get("settings") or {}
            if not isinstance(settings, dict):
                return {"ok": False, "error": "settings must be an object"}
            try:
                status = self.engine.start(settings)
                return {"ok": True, "data": {"scan": status}}
            except Exception as exc:
                LOG.exception("scan start failed")
                return {"ok": False, "error": str(exc)}
        if cmd in ("stop", "scan_stop"):
            return {"ok": True, "data": {"scan": self.engine.stop()}}
        if cmd == "monitor":
            profile = command.get("profile") or {}
            try:
                status = self.engine.monitor(profile)
                return {"ok": True, "data": {"scan": status}}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}
        if cmd == "radio_info":
            return {"ok": True, "data": self.manager.info()}
        if cmd == "shutdown":
            self._shutdown.set()
            return {"ok": True, "data": {"shutdown": True}}
        return {"ok": False, "error": f"unknown command: {cmd}"}

    def _client_loop(self, client: socket.socket) -> None:
        with self._client_lock:
            self._clients.add(client)
        LOG.info("web client connected")
        self.send(client, self._hello())
        self.send(client, self._status())
        buffer = b""
        try:
            while not self._shutdown.is_set():
                chunk = client.recv(65536)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        command = json.loads(line.decode("utf-8"))
                    except Exception:
                        self.send(client, {"type": "error", "error": "invalid JSON"})
                        continue
                    response = self.handle_command(command)
                    response.setdefault("type", "result")
                    response.setdefault("cmd", command.get("cmd"))
                    response["id"] = command.get("id")
                    self.send(client, response)
        except OSError:
            pass
        finally:
            with self._client_lock:
                self._clients.discard(client)
            try:
                client.close()
            except OSError:
                pass
            LOG.info("web client disconnected")

    # -- server ------------------------------------------------------------
    def serve(self) -> int:
        os.makedirs(os.path.dirname(self.socket_path), exist_ok=True)
        try:
            if os.path.exists(self.socket_path):
                os.unlink(self.socket_path)
        except OSError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self.socket_path)
        server.listen(16)
        server.settimeout(0.5)
        self._server = server
        try:
            os.chmod(self.socket_path, 0o660)
            try:
                import grp

                gid = grp.getgrnam("alex").gr_gid
                os.chown(self.socket_path, 0, gid)
            except Exception:
                os.chmod(self.socket_path, 0o666)
        except OSError:
            pass
        LOG.info("radio daemon listening on %s (uid=%s)", self.socket_path, os.getuid())
        while not self._shutdown.is_set():
            try:
                client, _addr = server.accept()
            except socket.timeout:
                continue
            except OSError as exc:
                if self._shutdown.is_set():
                    break
                LOG.warning("accept failed: %s", exc)
                continue
            thread = threading.Thread(
                target=self._client_loop, args=(client,), name="lora95-client", daemon=True
            )
            thread.start()
        return 0

    def shutdown(self) -> None:
        LOG.info("shutting down")
        self._shutdown.set()
        try:
            self.engine.stop()
        except Exception:
            LOG.exception("engine stop failed")
        try:
            self.manager.release()
        except Exception:
            pass
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="LoRa95 radio daemon")
    parser.add_argument("--socket", default=os.environ.get("LORA95_SOCKET", DEFAULT_SOCKET))
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    daemon = RadioDaemon(args.socket)

    def _signal(_signum, _frame):
        daemon.shutdown()

    signal.signal(signal.SIGTERM, _signal)
    signal.signal(signal.SIGINT, _signal)
    return daemon.serve()


if __name__ == "__main__":
    sys.exit(main())
