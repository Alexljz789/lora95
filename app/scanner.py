"""Radio lifecycle and the multi-profile scan engine."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Any, Callable, Optional

from . import profiles as profile_mod
from .radio import RadioError, SX126x

RESTORE_FLAG = "/run/lora95/meshtasticd_was_active"


def _run(cmd: list[str], timeout: float = 15.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:  # pragma: no cover - target dependent
        return 255, str(exc)


class RadioManager:
    """Owns the SX1262 and arbitrates it against meshtasticd.

    meshtasticd uses the same SPI bus and GPIO lines, so it has to be stopped
    while LoRa95 is scanning.  The previous state is remembered (and restored
    by the systemd unit's ExecStopPost helper even if the daemon crashes).
    """

    def __init__(self, emit: Callable[[dict[str, Any]], None]) -> None:
        self.emit = emit
        self.radio: Optional[SX126x] = None
        self._lock = threading.RLock()
        self._stop_meshtastic = False
        self._meshtastic_was_active = False

    def _log(self, message: str, level: str = "info") -> None:
        self.emit({"type": "log", "level": level, "message": message, "ts": time.time()})

    def _systemctl(self, action: str, unit: str) -> tuple[int, str]:
        return _run(["systemctl", action, unit], timeout=20)

    def acquire(self) -> SX126x:
        with self._lock:
            if self.radio is not None:
                return self.radio
            # Recover a stale flag from an earlier crash.
            if os.path.exists(RESTORE_FLAG):
                try:
                    self._systemctl("start", "meshtasticd.service")
                except Exception:
                    pass
                try:
                    os.unlink(RESTORE_FLAG)
                except OSError:
                    pass
            active = self._systemctl("is-active", "meshtasticd.service")[1].strip() == "active"
            if active:
                self._meshtastic_was_active = True
                os.makedirs(os.path.dirname(RESTORE_FLAG), exist_ok=True)
                with open(RESTORE_FLAG, "w", encoding="utf-8") as fh:
                    fh.write("1\n")
                self._log("Stopping meshtasticd to free SPI1/GPIO for LoRa95…")
                code, out = self._systemctl("stop", "meshtasticd.service")
                if code != 0:
                    self._log(f"meshtasticd stop failed: {out.strip()}", "error")
                for _ in range(40):
                    if (
                        self._systemctl("is-active", "meshtasticd.service")[1].strip()
                        != "active"
                    ):
                        break
                    time.sleep(0.25)
                time.sleep(0.4)
            # The AIO v2 LoRa rail is GPIO16.  pinctrl sets mux + level and is
            # persistent; this is safer than aiov2_ctl, which would also toggle
            # meshtasticd underneath us.
            rail = _run(["pinctrl", "get", "16"])[1]
            if "hi" not in rail:
                _run(["pinctrl", "set", "16", "op", "dh"])
                self._log("Powered the AIO v2 LoRa rail (GPIO16).")
            radio = SX126x()
            radio.open()
            self.radio = radio
            self._log(
                f"SX126x ready: {radio.info().get('chip')} · max {radio.info().get('max_power_dbm')} dBm",
                "ok",
            )
            return radio

    def reopen(self) -> SX126x:
        with self._lock:
            if self.radio is not None:
                try:
                    self.radio.close()
                except Exception:
                    pass
                self.radio = None
            time.sleep(0.5)
            return self.acquire()

    def release(self) -> None:
        with self._lock:
            if self.radio is not None:
                try:
                    self.radio.close()
                except Exception:
                    pass
                self.radio = None
            if self._meshtastic_was_active or os.path.exists(RESTORE_FLAG):
                self._log("Restoring meshtasticd…")
                code, out = self._systemctl("start", "meshtasticd.service")
                if code != 0:
                    self._log(f"meshtasticd restart failed: {out.strip()}", "error")
                try:
                    os.unlink(RESTORE_FLAG)
                except OSError:
                    pass
                self._meshtastic_was_active = False

    def info(self) -> dict[str, Any]:
        with self._lock:
            out: dict[str, Any] = {
                "open": self.radio is not None,
                "meshtasticd": self._systemctl("is-active", "meshtasticd.service")[1].strip(),
                "radio": self.radio.info() if self.radio else None,
            }
            return out


class ScanEngine:
    def __init__(self, manager: RadioManager, emit: Callable[[dict[str, Any]], None]) -> None:
        self.manager = manager
        self.emit = emit
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._settings: dict[str, Any] = {}
        self._profiles: list[profile_mod.Profile] = []
        self._state: dict[str, Any] = {
            "running": False,
            "monitor": False,
            "mode": "quick",
            "region": "US",
            "index": 0,
            "total": 0,
            "round": 0,
            "profile": None,
            "packets": 0,
            "signals": 0,
            "hits": {},
            "started_at": None,
            "dwell": 1.2,
            "last_error": None,
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            out = dict(self._state)
            out["profiles"] = len(self._profiles)
            out["hits"] = dict(self._state.get("hits") or {})
            return out

    def _set_status(self, **kwargs: Any) -> None:
        with self._lock:
            self._state.update(kwargs)
            snapshot = dict(self._state)
        self.emit({"type": "scan", "scan": snapshot, "ts": time.time()})

    def _log(self, message: str, level: str = "info") -> None:
        self.emit({"type": "log", "level": level, "message": message, "ts": time.time()})

    def start(self, settings: dict[str, Any]) -> dict[str, Any]:
        self.stop()
        profiles = profile_mod.build_profiles(
            region=settings.get("region", "US"),
            mode=settings.get("mode", "quick"),
            custom_frequency_mhz=settings.get("custom_frequency_mhz"),
            custom_sf=settings.get("custom_sf"),
            custom_bw_khz=settings.get("custom_bw_khz"),
            custom_cr=settings.get("custom_cr"),
            custom_sync_word=settings.get("custom_sync_word"),
            custom_preamble=int(settings.get("custom_preamble", 16) or 16),
            include_raw=bool(settings.get("include_raw", True)),
        )
        if not profiles:
            raise ValueError("no scan profiles were generated for this region")
        with self._lock:
            self._settings = dict(settings)
            self._profiles = profiles
            self._stop.clear()
            self._state.update(
                {
                    "running": True,
                    "monitor": False,
                    "mode": settings.get("mode", "quick"),
                    "region": settings.get("region", "US"),
                    "index": 0,
                    "total": len(profiles),
                    "round": 0,
                    "profile": profiles[0].to_dict(),
                    "packets": 0,
                    "signals": 0,
                    "hits": {},
                    "started_at": time.time(),
                    "dwell": float(settings.get("dwell", 1.2)),
                    "last_error": None,
                }
            )
            self._thread = threading.Thread(target=self._run, name="lora95-scan", daemon=True)
            self._thread.start()
        self._log(f"Scan started: {len(profiles)} profiles, region {settings.get('region', 'US')}")
        self._set_status()
        return self.status()

    def monitor(self, profile: dict[str, Any]) -> dict[str, Any]:
        self.stop()
        try:
            prof = profile_mod.Profile(
                id=profile.get("id") or "monitor",
                label=profile.get("label") or "Monitor",
                protocol=profile.get("protocol") or "custom",
                region=profile.get("region") or "US",
                frequency_mhz=float(profile["frequency_mhz"]),
                sf=int(profile["sf"]),
                bw_khz=float(profile["bw_khz"]),
                cr=int(profile.get("cr") or 5),
                sync_word=int(profile.get("sync_word") or 0x2B),
                preamble=int(profile.get("preamble") or 16),
                preset=profile.get("preset") or "monitor",
            )
        except Exception as exc:
            raise ValueError(f"invalid profile: {exc}") from exc
        with self._lock:
            self._settings = {"region": prof.region, "mode": "monitor", "dwell": 999999}
            self._profiles = [prof]
            self._stop.clear()
            self._state.update(
                {
                    "running": True,
                    "monitor": True,
                    "mode": "monitor",
                    "region": prof.region,
                    "index": 0,
                    "total": 1,
                    "round": 0,
                    "profile": prof.to_dict(),
                    "packets": 0,
                    "signals": 0,
                    "hits": {},
                    "started_at": time.time(),
                    "dwell": 999999,
                    "last_error": None,
                }
            )
            self._thread = threading.Thread(target=self._run_monitor, name="lora95-monitor", daemon=True)
            self._thread.start()
        self._log(f"Monitoring {prof.frequency_mhz:.4f} MHz · SF{prof.sf}/BW{int(prof.bw_khz)}")
        self._set_status()
        return self.status()

    def stop(self) -> dict[str, Any]:
        thread = None
        with self._lock:
            thread = self._thread
            self._stop.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=8.0)
        with self._lock:
            self._thread = None
            was_running = self._state.get("running")
            self._state.update({"running": False, "monitor": False})
        self.manager.release()
        if was_running:
            self._log("Scan stopped.")
        self._set_status()
        return self.status()

    # -- workers -----------------------------------------------------------
    def _run(self) -> None:
        settings = dict(self._settings)
        dwell = max(0.25, min(8.0, float(settings.get("dwell", 1.2))))
        min_rssi = float(settings.get("min_rssi", -140.0))
        cad_enabled = bool(settings.get("cad", False))
        try:
            radio = self.manager.acquire()
        except Exception as exc:
            self._set_status(running=False, last_error=str(exc))
            self._log(f"Cannot open the radio: {exc}", "error")
            self.manager.release()
            return
        try:
            index = 0
            total = len(self._profiles)
            while not self._stop.is_set():
                for i, profile in enumerate(self._profiles):
                    if self._stop.is_set():
                        break
                    index = i
                    self._set_status(index=i, round=self._state.get("round", 0))
                    radio.configure(
                        profile.frequency_hz,
                        profile.sf,
                        profile.bw_khz,
                        profile.cr,
                        profile.sync_word,
                        profile.preamble,
                    )
                    if cad_enabled:
                        try:
                            if radio.cad():
                                rssi = radio.read_rssi()
                                if rssi is None:
                                    rssi = -120.0
                                if -120.0 <= rssi <= -20.0 and rssi >= min_rssi:
                                    self._emit_signal(profile, rssi)
                                    self._bump_signal()
                        except Exception as exc:
                            self._log(f"CAD failed: {exc}", "warn")
                    radio.start_rx()
                    deadline = time.monotonic() + dwell
                    while not self._stop.is_set() and time.monotonic() < deadline:
                        try:
                            packet = radio.rx_packet()
                        except RadioError as exc:
                            self._log(f"Radio read error: {exc}", "warn")
                            raise
                        if packet:
                            if packet.rssi_dbm >= min_rssi:
                                self.emit(
                                    {
                                        "type": "packet",
                                        "packet": {
                                            "ts": packet.timestamp,
                                            "data_hex": packet.data.hex(),
                                            "rssi_dbm": round(packet.rssi_dbm, 1),
                                            "snr_db": round(packet.snr_db, 1),
                                            "signal_rssi_dbm": round(packet.signal_rssi_dbm, 1),
                                            "frequency_mhz": profile.frequency_mhz,
                                            "sf": profile.sf,
                                            "bw_khz": profile.bw_khz,
                                            "cr": profile.cr,
                                            "sync_word": profile.sync_word,
                                            "crc_ok": packet.crc_ok,
                                            "profile": profile.to_dict(),
                                        },
                                        "ts": packet.timestamp,
                                    }
                                )
                                self._bump_packet(profile)
                                # A pack may be followed by a reply; linger a bit.
                                deadline = min(
                                    deadline + 0.45,
                                    time.monotonic() + dwell * 3.0,
                                )
                        else:
                            time.sleep(0.004)
                    radio.stop_rx()
                if self._settings.get("once"):
                    break
                with self._lock:
                    self._state["round"] = int(self._state.get("round", 0)) + 1
                self._set_status()
        except Exception as exc:
            self._set_status(running=False, last_error=str(exc))
            self._log(f"Scan aborted: {exc}", "error")
        finally:
            try:
                if self.manager.radio is not None:
                    self.manager.radio.stop_rx()
            except Exception:
                pass
            self.manager.release()
            self._set_status(running=False)
            with self._lock:
                self._state["running"] = False

    def _run_monitor(self) -> None:
        profile = self._profiles[0]
        try:
            radio = self.manager.acquire()
        except Exception as exc:
            self._set_status(running=False, last_error=str(exc))
            self._log(f"Cannot open the radio: {exc}", "error")
            self.manager.release()
            return
        try:
            radio.configure(
                profile.frequency_hz,
                profile.sf,
                profile.bw_khz,
                profile.cr,
                profile.sync_word,
                profile.preamble,
            )
            radio.start_rx()
            while not self._stop.is_set():
                try:
                    packet = radio.rx_packet()
                except RadioError as exc:
                    self._log(f"Radio read error: {exc}", "warn")
                    radio = self.manager.reopen()
                    radio.configure(
                        profile.frequency_hz,
                        profile.sf,
                        profile.bw_khz,
                        profile.cr,
                        profile.sync_word,
                        profile.preamble,
                    )
                    radio.start_rx()
                    continue
                if packet:
                    self.emit(
                        {
                            "type": "packet",
                            "packet": {
                                "ts": packet.timestamp,
                                "data_hex": packet.data.hex(),
                                "rssi_dbm": round(packet.rssi_dbm, 1),
                                "snr_db": round(packet.snr_db, 1),
                                "signal_rssi_dbm": round(packet.signal_rssi_dbm, 1),
                                "frequency_mhz": profile.frequency_mhz,
                                "sf": profile.sf,
                                "bw_khz": profile.bw_khz,
                                "cr": profile.cr,
                                "sync_word": profile.sync_word,
                                "crc_ok": packet.crc_ok,
                                "profile": profile.to_dict(),
                            },
                            "ts": packet.timestamp,
                        }
                    )
                    self._bump_packet(profile)
                else:
                    time.sleep(0.004)
        except Exception as exc:
            self._set_status(running=False, last_error=str(exc))
            self._log(f"Monitor aborted: {exc}", "error")
        finally:
            try:
                if self.manager.radio is not None:
                    self.manager.radio.stop_rx()
            except Exception:
                pass
            self.manager.release()
            with self._lock:
                self._state["running"] = False
            self._set_status()

    def _emit_signal(self, profile: profile_mod.Profile, rssi: float) -> None:
        self.emit(
            {
                "type": "signal",
                "signal": {
                    "ts": time.time(),
                    "rssi_dbm": round(float(rssi), 1),
                    "detected": True,
                    "profile": profile.to_dict(),
                },
                "ts": time.time(),
            }
        )

    def _bump_packet(self, profile: profile_mod.Profile) -> None:
        with self._lock:
            self._state["packets"] = int(self._state.get("packets", 0)) + 1
            hits = dict(self._state.get("hits") or {})
            hits[profile.id] = int(hits.get(profile.id, 0)) + 1
            self._state["hits"] = hits
        self._set_status()

    def _bump_signal(self) -> None:
        with self._lock:
            self._state["signals"] = int(self._state.get("signals", 0)) + 1
        self._set_status()
