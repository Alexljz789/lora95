#!/usr/bin/env python3
"""Bare-metal SX1262 probe for the uConsole AIO v2 (SPI1, GPIO 24/25/26)."""
import sys, time, glob
import spidev
import lgpio

SPI_BUS, SPI_CS = 1, 0
PIN_BUSY, PIN_RESET, PIN_IRQ = 24, 25, 26

h = lgpio.gpiochip_open(0)
lgpio.gpio_claim_input(h, PIN_BUSY)
lgpio.gpio_claim_input(h, PIN_IRQ)
lgpio.gpio_claim_output(h, PIN_RESET, 1)

spi = spidev.SpiDev()
spi.open(SPI_BUS, SPI_CS)
spi.max_speed_hz = 4_000_000
spi.mode = 0

def busy():
    return lgpio.gpio_read(h, PIN_BUSY)

def wait_busy(timeout=1.0):
    t = time.time()
    while busy():
        if time.time() - t > timeout:
            raise TimeoutError("BUSY stuck high")
        time.sleep(0.0002)

def xfer(data):
    wait_busy()
    r = spi.xfer2(list(data))
    wait_busy()
    return bytes(r)

def cmd(op, data=b""):
    return xfer([op] + list(data))

def rd_reg(addr, n):
    r = xfer([0x1D, (addr >> 8) & 0xFF, addr & 0xFF, 0x00] + [0] * n)
    return r[4:4+n]

def wr_reg(addr, data):
    xfer([0x0D, (addr >> 8) & 0xFF, addr & 0xFF] + list(data))

def status():
    return xfer([0xC0])[0]

def reset():
    lgpio.gpio_write(h, PIN_RESET, 0)
    time.sleep(0.002)
    lgpio.gpio_write(h, PIN_RESET, 1)
    time.sleep(0.02)
    wait_busy()

print("BUSY before reset:", busy())
reset()
print("BUSY after reset:", busy())
print("STATUS:", hex(status()), "mode:", hex(status() & 0x70))
ver = rd_reg(0x0320, 16)
print("VERSION:", ver, ver.split(b"\x00")[0])
err = cmd(0x17, b"\x00\x00")  # GetDeviceErrors needs 2 status bytes? command returns status+2
print("DEVERR raw:", err.hex())
# proper get errors: read 3 bytes after opcode
r = xfer([0x17, 0, 0])
print("GetDeviceErrors:", r.hex(), "=>", (r[1] << 8) | r[2])
# standby RC
print("standby:", cmd(0x80, b"\x00").hex())
print("status:", hex(status()), "mode:", hex(status() & 0x70))
# set packet type LoRa
print("packettype:", cmd(0x8A, b"\x01").hex())
# TCXO 1.8V 10ms
print("tcxo:", cmd(0x97, bytes([0x02, 0x00, 0x05, 0x60])).hex())
time.sleep(0.02)
# calibrate all
print("cal:", cmd(0x89, b"\xff").hex())
time.sleep(0.02)
print("status:", hex(status()))
# calibrate image 902-928
print("calimg:", cmd(0x98, bytes([0xE1, 0xE9])).hex())
time.sleep(0.01)
print("dio2:", cmd(0x9D, b"\x01").hex())
print("regulator dcdc:", cmd(0x96, b"\x01").hex())
# PA config 22 dBm SX1262 + OCP 140mA
print("pa:", cmd(0x95, bytes([0x04, 0x07, 0x00, 0x01])).hex())
print("txparams:", cmd(0x8E, bytes([22, 0x04])).hex())
wr_reg(0x08E7, b"\x38")
# RX boosted gain
wr_reg(0x08AC, b"\x96")
# tx clamp fix
old = rd_reg(0x08D8, 1)[0]
wr_reg(0x08D8, bytes([old | 0x1E]))
print("txclamp:", hex(old), "->", hex(rd_reg(0x08D8, 1)[0]))
# sync word meshtastic 0x2B => 0x24B4
wr_reg(0x0740, bytes([0x24, 0xB4]))
print("sync:", rd_reg(0x0740, 2).hex())
# modulation SF11 BW250 CR5 LDRO on
print("mod:", cmd(0x8B, bytes([11, 0x05, 0x01, 0x01, 0,0,0,0])).hex())
# packet params preamble 16, explicit, 255, crc on, std iq
print("pkt:", cmd(0x8C, bytes([0x00, 0x10, 0x00, 0xFF, 0x01, 0x00, 0,0,0])).hex())
# freq 906.875
frf = int(906_875_000 * (1 << 25) / 32_000_000)
print("freq reg:", hex(frf))
print("setfreq:", cmd(0x86, frf.to_bytes(4, "big")).hex())
# IRQ mask
irq = 0x0002 | 0x0040 | 0x0200 | 0x0080 | 0x0100 | 0x0004 | 0x0008 | 0x0010
print("irq:", cmd(0x08, bytes([(irq>>8)&0xff, irq&0xff, (irq>>8)&0xff, irq&0xff, 0,0,0,0])).hex())
print("clear:", cmd(0x02, b"\xff\xff").hex())
# rx continuous
print("rx:", cmd(0x82, b"\xff\xff\xff").hex())
t0 = time.time()
last = None
seen = 0
while time.time() - t0 < 3.0:
    s = status()
    if s != last:
        print("  status", hex(s), "mode", hex(s & 0x70), "t=%.2f" % (time.time()-t0))
        last = s
    irq = xfer([0x12, 0, 0])
    flags = (irq[1] << 8) | irq[2]
    if flags:
        print("IRQ:", hex(flags), "t=%.2f" % (time.time()-t0))
        if flags & 0x0002:
            r = xfer([0x13, 0, 0, 0])
            print("  rxbuffer:", r.hex())
            payload_len, offset = r[1], r[2]
            data = xfer([0x1E, offset, 0] + [0]*payload_len)[2:2+payload_len]
            ps = xfer([0x14, 0,0,0,0])
            print("  len", payload_len, "data", data.hex(), "pktstatus", ps.hex())
        cmd(0x02, b"\xff\xff")
        cmd(0x82, b"\xff\xff\xff")
        seen += 1
    time.sleep(0.002)
print("done, irq events:", seen)
print("final status", hex(status()), "err", xfer([0x17,0,0]).hex())
cmd(0x80, b"\x00")
spi.close()
lgpio.gpiochip_close(h)
