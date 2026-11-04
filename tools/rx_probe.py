import time
from app.radio import SX126x

def report(label, r):
    time.sleep(0.15)
    print(label, "mode", r.get_mode(), "err", hex(r.get_device_errors()), "status", hex(r.get_status()))

r = SX126x(); r.open()
report("after open", r)
r.start_rx(); report("rx default", r)
r.stop_rx()
r.configure(906875000, 11, 250.0, 5, 43, 16)
r.start_rx(); report("rx configured", r)
r.stop_rx()
r._standby(); r._cmd(0x9D, b"\x00")  # DIO2 not RF switch
r.start_rx(); report("rx dio2off", r)
r.stop_rx()
r._standby(); r._cmd(0x9D, b"\x01")
r._cmd(0x97, bytes([0x02, 0x00, 0x05, 0x60])); time.sleep(0.02)
r.start_rx(); report("rx tcxo", r)
r.stop_rx()
r.close()
