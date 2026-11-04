#!/usr/bin/env python3
"""Long ambient RX logger for validating the SX126x receive path."""
import subprocess
import time

from app.radio import SX126x

stop = time.time() + 300
r = SX126x()
r.open()
r.configure(906875000, 11, 250.0, 5, 43, 16)
r.start_rx()
print("RX mode", r.get_mode(), "err", hex(r.get_device_errors()), flush=True)
count = 0
try:
    while time.time() < stop:
        pkt = r.rx_packet()
        if pkt:
            count += 1
            print(
                "PKT",
                int(time.time()),
                len(pkt.data),
                round(pkt.rssi_dbm, 1),
                round(pkt.snr_db, 1),
                pkt.data.hex(),
                flush=True,
            )
        time.sleep(0.004)
finally:
    r.close()
    print("DONE packets", count, flush=True)
    subprocess.run(["sudo", "-n", "systemctl", "start", "meshtasticd.service"], check=False)
