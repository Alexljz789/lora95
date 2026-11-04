"""Low-level SX126x driver for the HackerGadgets AIO v2 LoRa module.

The AIO v2 wires an SX1262 to SPI1 CE0 with:

    BUSY  = GPIO24
    RESET = GPIO25
    DIO1  = GPIO26
    DIO2  = RF switch control
    DIO3  = 1.8 V TCXO supply

This module deliberately has no dependency on RadioLib or meshtasticd.  It
speaks the SX126x SPI command set directly through Linux spidev/libgpiod, so
LoRa95 can receive raw LoRa frames from *any* transmitter instead of only
Meshtastic traffic.

One SX126x quirk is encoded here: every read command needs one dummy byte
after the opcode (and after a register address / buffer offset) before the
chip starts driving data on MISO.  Getting that wrong silently shifts every
status word by one byte.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Optional

try:  # pragma: no cover - imported only on the target
    import lgpio
    import spidev
except Exception:  # pragma: no cover - allows importing on a dev laptop
    lgpio = None
    spidev = None


# ---------------------------------------------------------------------------
# SX126x commands
# ---------------------------------------------------------------------------
CMD_NOP = 0x00
CMD_SET_SLEEP = 0x84
CMD_SET_STANDBY = 0x80
CMD_SET_FS = 0xC1
CMD_SET_TX = 0x83
CMD_SET_RX = 0x82
CMD_STOP_TIMER_ON_PREAMBLE = 0x9F
CMD_SET_RX_DUTY_CYCLE = 0x94
CMD_SET_CAD = 0xC5
CMD_SET_TX_CONTINUOUS_WAVE = 0xD1
CMD_SET_TX_INFINITE_PREAMBLE = 0xD2
CMD_SET_REGULATOR_MODE = 0x96
CMD_CALIBRATE = 0x89
CMD_CALIBRATE_IMAGE = 0x98
CMD_SET_PA_CONFIG = 0x95
CMD_SET_RX_TX_FALLBACK_MODE = 0x93

CMD_WRITE_REGISTER = 0x0D
CMD_READ_REGISTER = 0x1D
CMD_WRITE_BUFFER = 0x0E
CMD_READ_BUFFER = 0x1E

CMD_SET_DIO_IRQ_PARAMS = 0x08
CMD_GET_IRQ_STATUS = 0x12
CMD_CLEAR_IRQ_STATUS = 0x02
CMD_SET_DIO2_AS_RF_SWITCH = 0x9D
CMD_SET_DIO3_AS_TCXO = 0x97

CMD_SET_RF_FREQUENCY = 0x86
CMD_SET_PACKET_TYPE = 0x8A
CMD_GET_PACKET_TYPE = 0x11
CMD_SET_TX_PARAMS = 0x8E
CMD_SET_MODULATION_PARAMS = 0x8B
CMD_SET_PACKET_PARAMS = 0x8C
CMD_SET_CAD_PARAMS = 0x88
CMD_SET_BUFFER_BASE_ADDRESS = 0x8F
CMD_SET_LORA_SYMB_NUM_TIMEOUT = 0xA0

CMD_GET_STATUS = 0xC0
CMD_GET_RSSI_INST = 0x15
CMD_GET_RX_BUFFER_STATUS = 0x13
CMD_GET_PACKET_STATUS = 0x14
CMD_GET_DEVICE_ERRORS = 0x17
CMD_CLEAR_DEVICE_ERRORS = 0x07
CMD_GET_STATS = 0x10
CMD_RESET_STATS = 0x00

# IRQ bits
IRQ_TX_DONE = 0x0001
IRQ_RX_DONE = 0x0002
IRQ_PREAMBLE_DETECTED = 0x0004
IRQ_SYNC_WORD_VALID = 0x0008
IRQ_HEADER_VALID = 0x0010
IRQ_HEADER_ERR = 0x0020
IRQ_CRC_ERR = 0x0040
IRQ_CAD_DONE = 0x0080
IRQ_CAD_DETECTED = 0x0100
IRQ_TIMEOUT = 0x0200

IRQ_RX_ALL = (
    IRQ_RX_DONE
    | IRQ_CRC_ERR
    | IRQ_HEADER_ERR
    | IRQ_TIMEOUT
    | IRQ_CAD_DONE
    | IRQ_CAD_DETECTED
    | IRQ_PREAMBLE_DETECTED
    | IRQ_SYNC_WORD_VALID
)
IRQ_RADIO_ALL = IRQ_RX_ALL | IRQ_TX_DONE

# Device errors
ERR_RC64K = 0x0001
ERR_RC13M = 0x0002
ERR_PLL_CAL = 0x0004
ERR_ADC_CAL = 0x0008
ERR_IMG_CAL = 0x0010
ERR_XOSC_START = 0x0020
ERR_PLL_LOCK = 0x0040
ERR_PA_RAMP = 0x0100

DEVICE_ERROR_NAMES = {
    ERR_RC64K: "RC64K calibration",
    ERR_RC13M: "RC13M calibration",
    ERR_PLL_CAL: "PLL calibration",
    ERR_ADC_CAL: "ADC calibration",
    ERR_IMG_CAL: "image calibration",
    ERR_XOSC_START: "TCXO/XOSC start",
    ERR_PLL_LOCK: "PLL lock",
    ERR_PA_RAMP: "PA ramp",
}

# Bandwidth (kHz) -> SX126x code
BW_CODES = {
    7.8: 0x00,
    10.4: 0x08,
    15.6: 0x01,
    20.8: 0x09,
    31.25: 0x02,
    41.7: 0x0A,
    62.5: 0x03,
    125.0: 0x04,
    250.0: 0x05,
    500.0: 0x06,
    203.125: 0x0E,
    406.25: 0x0F,
    812.5: 0x10,
    1625.0: 0x11,
}

# Coding rate denominator -> SX126x code (4/5..4/8)
CR_CODES = {5: 0x01, 6: 0x02, 7: 0x03, 8: 0x04}

# CalibrateImage ranges: (low MHz, high MHz, low code, high code)
CAL_IMAGE_RANGES = [
    (430.0, 440.0, 0x6B, 0x6F),
    (470.0, 510.0, 0x75, 0x81),
    (779.0, 787.0, 0xC1, 0xC5),
    (863.0, 870.0, 0xD7, 0xDB),
    (902.0, 928.0, 0xE1, 0xE9),
]

REG_LORA_SYNC_WORD = 0x0740
REG_RX_GAIN = 0x08AC
REG_TX_CLAMP = 0x08D8
REG_OCP = 0x08E7
REG_VERSION = 0x0320


class RadioError(RuntimeError):
    """Raised when the radio cannot complete a command."""


@dataclass
class RxPacket:
    """A raw LoRa frame plus PHY metadata."""

    data: bytes
    rssi_dbm: float
    snr_db: float
    signal_rssi_dbm: float
    frequency_hz: int
    sf: int
    bw_khz: float
    cr: int
    sync_word: int
    crc_ok: bool
    timestamp: float

    @property
    def length(self) -> int:
        return len(self.data)


class SX126x:
    """Minimal, robust SX126x LoRa transceiver driver."""

    def __init__(
        self,
        spi_bus: int = 1,
        spi_cs: int = 0,
        pin_busy: int = 24,
        pin_reset: int = 25,
        pin_irq: int = 26,
        gpio_chip: int = 0,
        spi_speed_hz: int = 4_000_000,
        tcxo_voltage: int = 0x02,  # 1.8 V
        tcxo_delay_ms: float = 10.0,
        use_dcdc: bool = True,
        rx_boosted: bool = True,
        tx_power_dbm: int = 14,
    ) -> None:
        self.spi_bus = spi_bus
        self.spi_cs = spi_cs
        self.pin_busy = pin_busy
        self.pin_reset = pin_reset
        self.pin_irq = pin_irq
        self.gpio_chip = gpio_chip
        self.spi_speed_hz = spi_speed_hz
        self.tcxo_voltage = tcxo_voltage
        self.tcxo_delay_ms = tcxo_delay_ms
        self.use_dcdc = use_dcdc
        self.rx_boosted = rx_boosted
        self.tx_power_dbm = tx_power_dbm

        self._spi = None
        self._gpio = None
        self._lock = threading.RLock()
        self._ready = False
        self._mode = "off"
        self._chip = "SX126x"
        self._version = b""
        self._max_power = 22
        self._current_profile = None
        self._last_config = None
        self._last_irq = 0
        self._rx_armed = False
        self._stats = {
            "rx_packets": 0,
            "crc_errors": 0,
            "header_errors": 0,
            "timeouts": 0,
            "cad_scans": 0,
            "cad_hits": 0,
            "tx_packets": 0,
            "resets": 0,
            "spi_errors": 0,
        }

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        if lgpio is None or spidev is None:
            raise RadioError("lgpio/spidev not available (target-only driver)")
        with self._lock:
            if self._gpio is None:
                self._gpio = lgpio.gpiochip_open(self.gpio_chip)
                lgpio.gpio_claim_input(self._gpio, self.pin_busy)
                lgpio.gpio_claim_input(self._gpio, self.pin_irq)
                lgpio.gpio_claim_output(self._gpio, self.pin_reset, 1)
            if self._spi is None:
                spi = spidev.SpiDev()
                spi.open(self.spi_bus, self.spi_cs)
                spi.max_speed_hz = self.spi_speed_hz
                spi.mode = 0
                spi.lsbfirst = False
                spi.cshigh = False
                self._spi = spi
            self._init_chip()

    def close(self) -> None:
        with self._lock:
            try:
                if self._spi is not None and self._ready:
                    self._standby()
            except Exception:
                pass
            if self._spi is not None:
                try:
                    self._spi.close()
                except Exception:
                    pass
                self._spi = None
            if self._gpio is not None:
                try:
                    lgpio.gpiochip_close(self._gpio)
                except Exception:
                    pass
                self._gpio = None
            self._ready = False
            self._mode = "off"

    def _init_chip(self) -> None:
        assert self._spi is not None and self._gpio is not None
        self._reset()
        self._cmd(CMD_SET_STANDBY, b"\x00")
        # Force the packet type early so a stale meshtasticd state cannot leak in.
        self._cmd(CMD_SET_PACKET_TYPE, b"\x01")
        self._set_tcxo(self.tcxo_voltage, self.tcxo_delay_ms)
        # The TCXO needs its full start-up delay before the PLL/XOSC calibration
        # is attempted, otherwise XOSC_START_ERR stays latched.
        time.sleep(max(0.02, self.tcxo_delay_ms / 1000.0 + 0.01))
        self._clear_device_errors()
        self._cmd(CMD_CALIBRATE, b"\xff")
        time.sleep(0.01)
        self._wait_busy()
        self._clear_device_errors()
        self._version = self._read_register(REG_VERSION, 16)
        self._chip = self._version.split(b"\x00")[0].decode("ascii", "replace").strip()
        self._max_power = 15
        self._cmd(CMD_SET_DIO2_AS_RF_SWITCH, b"\x01")
        self._set_rx_boosted(self.rx_boosted)
        self._cmd(CMD_SET_BUFFER_BASE_ADDRESS, b"\x00\x00")
        self._cmd(CMD_SET_LORA_SYMB_NUM_TIMEOUT, b"\x00")
        self._set_irq_params(IRQ_RX_ALL)
        self._clear_irq(0x03FF)
        self._ready = True
        self._mode = "standby"
        self._stats["resets"] += 1

    # -- low level SPI -----------------------------------------------------
    def _reset(self) -> None:
        assert self._gpio is not None
        lgpio.gpio_write(self._gpio, self.pin_reset, 0)
        time.sleep(0.002)
        lgpio.gpio_write(self._gpio, self.pin_reset, 1)
        time.sleep(0.015)
        if not self._wait_busy(0.2):
            raise RadioError("SX126x BUSY stuck high after reset")

    def _busy(self) -> int:
        return lgpio.gpio_read(self._gpio, self.pin_busy)

    def _wait_busy(self, timeout: float = 1.0) -> bool:
        deadline = time.monotonic() + timeout
        while self._busy():
            if time.monotonic() > deadline:
                return False
            time.sleep(0.0002)
        return True

    def _xfer(self, data: bytes) -> bytes:
        assert self._spi is not None
        if not self._wait_busy(0.5):
            self._stats["spi_errors"] += 1
            raise RadioError("BUSY stuck before SPI transaction")
        try:
            out = bytes(self._spi.xfer2(list(data)))
        except Exception as exc:  # pragma: no cover - hardware dependent
            self._stats["spi_errors"] += 1
            raise RadioError(f"SPI transaction failed: {exc}") from exc
        if not self._wait_busy(0.5):
            self._stats["spi_errors"] += 1
            raise RadioError("BUSY stuck after SPI transaction")
        return out

    def _cmd(self, opcode: int, data: bytes = b"") -> bytes:
        return self._xfer(bytes([opcode]) + data)

    def _read(self, opcode: int, count: int, prefix: bytes = b"") -> bytes:
        # opcode + prefix + one dummy byte + payload
        request = bytes([opcode]) + prefix + b"\x00" + bytes(count)
        response = self._xfer(request)
        start = 1 + len(prefix) + 1
        return response[start : start + count]

    def _write_register(self, address: int, data: bytes) -> None:
        self._cmd(CMD_WRITE_REGISTER, bytes([(address >> 8) & 0xFF, address & 0xFF]) + data)

    def _read_register(self, address: int, count: int) -> bytes:
        return self._read(
            CMD_READ_REGISTER, count, bytes([(address >> 8) & 0xFF, address & 0xFF])
        )

    def _write_buffer(self, offset: int, data: bytes) -> None:
        self._cmd(CMD_WRITE_BUFFER, bytes([offset]) + data)

    def _read_buffer(self, offset: int, count: int) -> bytes:
        return self._read(CMD_READ_BUFFER, count, bytes([offset]))

    # -- chip configuration ------------------------------------------------
    def _standby(self, xosc: bool = False) -> None:
        self._cmd(CMD_SET_STANDBY, b"\x01" if xosc else b"\x00")
        self._mode = "standby"

    def _set_tcxo(self, voltage: int, delay_ms: float) -> None:
        steps = int(round(delay_ms / 15.625))
        steps = max(0, min(0xFFFFFF, steps))
        self._cmd(
            CMD_SET_DIO3_AS_TCXO,
            bytes([voltage & 0x07, (steps >> 16) & 0xFF, (steps >> 8) & 0xFF, steps & 0xFF]),
        )

    def _set_rx_boosted(self, enabled: bool) -> None:
        self._write_register(REG_RX_GAIN, b"\x96" if enabled else b"\x94")

    def _set_irq_params(self, mask: int) -> None:
        hi, lo = (mask >> 8) & 0xFF, mask & 0xFF
        self._cmd(CMD_SET_DIO_IRQ_PARAMS, bytes([hi, lo, hi, lo, 0x00, 0x00, 0x00, 0x00]))

    def _clear_irq(self, mask: int = 0x03FF) -> None:
        self._cmd(CMD_CLEAR_IRQ_STATUS, bytes([(mask >> 8) & 0xFF, mask & 0xFF]))

    def _clear_device_errors(self) -> None:
        self._cmd(CMD_CLEAR_DEVICE_ERRORS, b"\x00\x00")

    def get_status(self) -> int:
        with self._lock:
            return self._cmd(CMD_GET_STATUS, b"\x00")[0]

    def get_mode(self) -> str:
        status = self.get_status()
        mode = (status >> 4) & 0x07
        return {
            0x02: "standby_rc",
            0x03: "standby_xosc",
            0x04: "fs",
            0x05: "rx",
            0x06: "tx",
        }.get(mode, f"mode_{mode}")

    def get_device_errors(self) -> int:
        with self._lock:
            raw = self._read(CMD_GET_DEVICE_ERRORS, 2)
            return (raw[0] << 8) | raw[1]

    def device_error_text(self, errors: Optional[int] = None) -> str:
        if errors is None:
            errors = self.get_device_errors()
        if not errors:
            return ""
        names = [name for bit, name in DEVICE_ERROR_NAMES.items() if errors & bit]
        return ", ".join(names) or f"0x{errors:04X}"

    def set_frequency(self, frequency_hz: int) -> None:
        raw = int(round(frequency_hz * (1 << 25) / 32_000_000)) & 0xFFFFFFFF
        self._cmd(CMD_SET_RF_FREQUENCY, raw.to_bytes(4, "big"))
        self._last_config = None

    def calibrate_image(self, frequency_hz: int) -> None:
        mhz = frequency_hz / 1_000_000.0
        for low, high, c1, c2 in CAL_IMAGE_RANGES:
            if low <= mhz <= high:
                self._cmd(CMD_CALIBRATE_IMAGE, bytes([c1, c2]))
                time.sleep(0.001)
                return
        # Pick the nearest supported image calibration window.
        nearest = min(CAL_IMAGE_RANGES, key=lambda r: min(abs(mhz - r[0]), abs(mhz - r[1])))
        self._cmd(CMD_CALIBRATE_IMAGE, bytes([nearest[2], nearest[3]]))
        time.sleep(0.001)

    def set_sync_word(self, sync_word: int) -> None:
        if sync_word <= 0xFF:
            msb = (sync_word & 0xF0) | 0x04
            lsb = ((sync_word << 4) & 0xFF) | 0x04
            self._write_register(REG_LORA_SYNC_WORD, bytes([msb, lsb]))
        else:
            self._write_register(
                REG_LORA_SYNC_WORD, bytes([(sync_word >> 8) & 0xFF, sync_word & 0xFF])
            )

    def _apply_tx_power(self, power_dbm: int | None = None) -> None:
        """(Re)apply PA config, OCP and TX params.

        CalibrateImage / standby can leave the PA table in a state where the
        next SetTx aborts with XOSC_START_ERR.  RadioLib applies the output
        power after frequency calibration for exactly this reason.
        """
        power = min(int(self.tx_power_dbm if power_dbm is None else power_dbm), self._max_power)
        self._cmd(CMD_SET_PA_CONFIG, bytes([0x04, 0x07, 0x00, 0x01]))
        self._write_register(REG_OCP, bytes([0x38]))
        self._cmd(CMD_SET_TX_PARAMS, bytes([power & 0xFF, 0x04]))
        self._cmd(CMD_SET_DIO2_AS_RF_SWITCH, bytes([0x01]))

    def set_modulation(self, sf: int, bw_khz: float, cr: int) -> None:
        bw_code = BW_CODES.get(float(bw_khz))
        if bw_code is None:
            raise RadioError(f"unsupported bandwidth {bw_khz} kHz")
        cr_code = CR_CODES.get(int(cr), 0x01)
        symbol_time = (2 ** int(sf)) / (bw_khz * 1000.0)
        ldro = 0x01 if symbol_time >= 0.016 else 0x00
        self._cmd(CMD_SET_MODULATION_PARAMS, bytes([sf & 0xFF, bw_code, cr_code, ldro, 0, 0, 0, 0]))
        self._last_config = None

    def set_packet_params(
        self, preamble: int = 16, payload_length: int = 255, crc: bool = True, explicit: bool = True
    ) -> None:
        self._cmd(
            CMD_SET_PACKET_PARAMS,
            bytes(
                [
                    (preamble >> 8) & 0xFF,
                    preamble & 0xFF,
                    0x00 if explicit else 0x01,
                    payload_length & 0xFF,
                    0x01 if crc else 0x00,
                    0x00,  # IQ standard
                    0x00,
                    0x00,
                    0x00,
                ]
            ),
        )
        self._last_config = None

    def configure(
        self,
        frequency_hz: int,
        sf: int,
        bw_khz: float,
        cr: int,
        sync_word: int,
        preamble: int = 16,
        tx_power_dbm: Optional[int] = None,
    ) -> None:
        with self._lock:
            self._ensure()
            if tx_power_dbm is not None:
                self.tx_power_dbm = min(int(tx_power_dbm), self._max_power)
                self._cmd(CMD_SET_TX_PARAMS, bytes([self.tx_power_dbm & 0xFF, 0x04]))
            self._standby(xosc=True)
            # CalibrateImage on this AIO v2 unit latches XOSC_START_ERR even
            # though the TCXO is configured exactly as meshtasticd does.  The
            # full Calibrate(0xFF) performed at init already covers the image
            # rejection block, so per-profile image calibration is skipped.
            self.set_frequency(frequency_hz)
            self.set_modulation(sf, bw_khz, cr)
            self.set_packet_params(preamble=preamble, payload_length=255, crc=True, explicit=True)
            self.set_sync_word(sync_word)
            self._current_profile = {
                "frequency_hz": int(frequency_hz),
                "frequency_mhz": round(frequency_hz / 1_000_000.0, 6),
                "sf": int(sf),
                "bw_khz": float(bw_khz),
                "cr": int(cr),
                "sync_word": int(sync_word),
                "preamble": int(preamble),
            }
            self._last_config = dict(self._current_profile)

    # -- receive -----------------------------------------------------------
    def start_rx(self, timeout: int = 0xFFFFFF) -> None:
        with self._lock:
            self._ensure()
            self._clear_irq(0x03FF)
            self._cmd(CMD_SET_RX, timeout.to_bytes(3, "big"))
            self._mode = "rx"
            self._rx_armed = True

    def irq_status(self) -> int:
        with self._lock:
            raw = self._read(CMD_GET_IRQ_STATUS, 2)
            value = (raw[0] << 8) | raw[1]
            self._last_irq = value
            return value

    def clear_irq(self, mask: int = 0x03FF) -> None:
        with self._lock:
            self._clear_irq(mask)

    def irq_pin(self) -> int:
        return lgpio.gpio_read(self._gpio, self.pin_irq)

    def rx_packet(self) -> Optional[RxPacket]:
        """Read a completed packet, if the RX-done IRQ is pending."""
        with self._lock:
            irq = self.irq_status()
            if not irq:
                return None
            cfg = self._current_profile or {}
            if irq & IRQ_CRC_ERR:
                self._stats["crc_errors"] += 1
                self._clear_irq(IRQ_CRC_ERR | IRQ_RX_DONE | IRQ_HEADER_ERR | IRQ_TIMEOUT)
                self._rearm_rx()
                return None
            if irq & IRQ_HEADER_ERR:
                self._stats["header_errors"] += 1
                self._clear_irq(IRQ_HEADER_ERR | IRQ_RX_DONE | IRQ_TIMEOUT)
                self._rearm_rx()
                return None
            if irq & IRQ_TIMEOUT:
                self._stats["timeouts"] += 1
                self._clear_irq(IRQ_TIMEOUT | IRQ_RX_DONE)
                self._rearm_rx()
                return None
            if not (irq & IRQ_RX_DONE):
                return None
            status = self._read(CMD_GET_RX_BUFFER_STATUS, 2)
            length, offset = status[0], status[1]
            if length <= 0 or length > 255:
                self._clear_irq(0x03FF)
                self._rearm_rx()
                return None
            data = self._read_buffer(offset, length)
            packet_status = self._read(CMD_GET_PACKET_STATUS, 3)
            rssi_raw = packet_status[0]
            snr_raw = packet_status[1]
            signal_raw = packet_status[2]
            rssi = -rssi_raw / 2.0
            snr = (snr_raw - 256 if snr_raw > 127 else snr_raw) / 4.0
            signal_rssi = -signal_raw / 2.0
            self._clear_irq(0x03FF)
            self._stats["rx_packets"] += 1
            self._rearm_rx()
            return RxPacket(
                data=bytes(data),
                rssi_dbm=rssi,
                snr_db=snr,
                signal_rssi_dbm=signal_rssi,
                frequency_hz=int(cfg.get("frequency_hz", 0)),
                sf=int(cfg.get("sf", 0)),
                bw_khz=float(cfg.get("bw_khz", 0)),
                cr=int(cfg.get("cr", 0)),
                sync_word=int(cfg.get("sync_word", 0)),
                crc_ok=True,
                timestamp=time.time(),
            )

    def _rearm_rx(self) -> None:
        if not self._rx_armed:
            return
        try:
            self._cmd(CMD_SET_RX, b"\xff\xff\xff")
            self._mode = "rx"
        except Exception:
            self._rx_armed = False

    def stop_rx(self) -> None:
        with self._lock:
            self._rx_armed = False
            if self._ready:
                self._standby()
                self._clear_irq(0x03FF)

    def read_rssi(self) -> float:
        with self._lock:
            raw = self._read(CMD_GET_RSSI_INST, 1)[0]
            return -raw / 2.0

    # -- channel activity detection ---------------------------------------
    def cad(self, timeout: float = 0.5) -> bool:
        """Run one CAD measurement with the current modulation settings."""
        with self._lock:
            self._ensure()
            self._rx_armed = False
            self._standby()
            sf = int((self._current_profile or {}).get("sf", 9))
            self._cmd(
                CMD_SET_CAD_PARAMS,
                bytes([0x02, min(0xFF, sf + 13), 0x0A, 0x00, 0x00, 0x00, 0x00]),
            )
            self._clear_irq(0x03FF)
            self._cmd(CMD_SET_CAD)
            deadline = time.monotonic() + timeout
            detected = False
            while time.monotonic() < deadline:
                irq = self._read(CMD_GET_IRQ_STATUS, 2)
                value = (irq[0] << 8) | irq[1]
                if value & IRQ_CAD_DONE:
                    detected = bool(value & IRQ_CAD_DETECTED)
                    self._clear_irq(IRQ_CAD_DONE | IRQ_CAD_DETECTED)
                    break
                time.sleep(0.001)
            self._stats["cad_scans"] += 1
            if detected:
                self._stats["cad_hits"] += 1
            self._mode = "standby"
            return detected

    # -- transmit ----------------------------------------------------------
    def transmit(self, payload: bytes, timeout_ms: int = 2000) -> bool:
        with self._lock:
            self._ensure()
            if len(payload) > 255:
                raise RadioError("LoRa payload cannot exceed 255 bytes")
            cfg = self._current_profile
            if not cfg:
                raise RadioError("radio is not configured")
            self._rx_armed = False
            self._standby()
            self._cmd(CMD_SET_PACKET_TYPE, bytes([0x01]))
            self.set_frequency(int(cfg["frequency_hz"]))
            self.set_modulation(int(cfg["sf"]), float(cfg["bw_khz"]), int(cfg["cr"]))
            self.set_sync_word(int(cfg["sync_word"]))
            self.set_packet_params(
                preamble=int(cfg.get("preamble", 16)),
                payload_length=len(payload),
                crc=True,
                explicit=True,
            )
            self._write_buffer(0, payload)
            self._apply_tx_power()
            self._set_irq_params(IRQ_RADIO_ALL)
            self._clear_irq(0x03FF)
            ticks = max(1, int(timeout_ms * 1000 / 15.625))
            self._cmd(CMD_SET_TX, ticks.to_bytes(3, "big"))
            self._mode = "tx"
            deadline = time.monotonic() + timeout_ms / 1000.0 + 1.0
            ok = False
            while time.monotonic() < deadline:
                irq = self._read(CMD_GET_IRQ_STATUS, 2)
                value = (irq[0] << 8) | irq[1]
                if value & IRQ_TX_DONE:
                    ok = True
                    self._clear_irq(IRQ_TX_DONE | IRQ_TIMEOUT)
                    break
                if value & IRQ_TIMEOUT:
                    self._clear_irq(IRQ_TX_DONE | IRQ_TIMEOUT)
                    break
                time.sleep(0.001)
            self._standby()
            if ok:
                self._stats["tx_packets"] += 1
            return ok

    # -- status ------------------------------------------------------------
    def _ensure(self) -> None:
        if not self._ready:
            raise RadioError("radio is not open")

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def info(self) -> dict:
        with self._lock:
            return {
                "open": bool(self._ready),
                "chip": self._chip,
                "version": self._version.split(b"\x00")[0].decode("ascii", "replace"),
                "max_power_dbm": self._max_power,
                "mode": self._mode,
                "config": dict(self._current_profile) if self._current_profile else None,
                "stats": dict(self._stats),
            }

    def read_device_errors(self) -> int:
        return self.get_device_errors()


def frequency_to_raw(frequency_hz: int) -> int:
    """SX126x PLL word for a frequency in Hz (32 MHz XTAL, 2^25)."""
    return int(round(frequency_hz * (1 << 25) / 32_000_000)) & 0xFFFFFFFF


def raw_to_frequency(raw: int) -> float:
    return raw * 32_000_000 / (1 << 25)


def bandwidth_code(bw_khz: float) -> int:
    return BW_CODES[float(bw_khz)]


def symbol_time_ms(sf: int, bw_khz: float) -> float:
    return (2 ** sf) / (bw_khz * 1000.0) * 1000.0


if __name__ == "__main__":  # tiny self-test, run on the target
    radio = SX126x()
    radio.open()
    try:
        print(radio.info())
        radio.configure(906_875_000, 11, 250.0, 5, 0x2B)
        print("configured", radio.info())
        radio.start_rx()
        t0 = time.time()
        while time.time() - t0 < 3:
            pkt = radio.rx_packet()
            if pkt:
                print("packet", pkt)
            time.sleep(0.01)
    finally:
        radio.close()
