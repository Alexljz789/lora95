#!/usr/bin/env python3
"""Exercise the radio daemon over its Unix socket (hardware smoke test)."""
import json
import socket
import sys
import time

SOCKET = sys.argv[1] if len(sys.argv) > 1 else "/tmp/lora95-test.sock"
sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.settimeout(0.5)
sock.connect(SOCKET)
buffer = b""
next_id = 1


def send(obj):
    global next_id
    obj.setdefault("id", next_id)
    next_id += 1
    sock.sendall((json.dumps(obj) + "\n").encode())


def read_one():
    global buffer
    deadline = time.time() + 0.5
    while b"\n" not in buffer:
        if time.time() > deadline:
            return None
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            return None
        if not chunk:
            return None
        buffer += chunk
    line, buffer = buffer.split(b"\n", 1)
    try:
        return json.loads(line.decode())
    except Exception:
        return None


for _ in range(2):
    print("HELLO/STATUS", json.dumps(read_one(), ensure_ascii=False)[:600])

send({"cmd": "status"})
end = time.time() + 2
while time.time() < end:
    event = read_one()
    if event and event.get("id") == 1:
        print("STATUS", json.dumps(event, ensure_ascii=False)[:700])

settings = {
    "region": "US",
    "mode": "custom",
    "custom_frequency_mhz": 906.875,
    "custom_sf": 11,
    "custom_bw_khz": 250,
    "custom_cr": 5,
    "custom_sync_word": 43,
    "custom_preamble": 16,
    "dwell": 2.0,
    "min_rssi": -140,
    "cad": True,
    "include_raw": True,
}
send({"cmd": "scan", "settings": settings})
end = time.time() + 12
while time.time() < end:
    event = read_one()
    if not event:
        continue
    kind = event.get("type")
    if kind == "scan":
        scan = event.get("scan") or {}
        print(
            "SCAN running=%s idx=%s/%s round=%s pkt=%s sig=%s profile=%s err=%s"
            % (
                scan.get("running"),
                scan.get("index"),
                scan.get("total"),
                scan.get("round"),
                scan.get("packets"),
                scan.get("signals"),
                (scan.get("profile") or {}).get("label"),
                scan.get("last_error"),
            )
        )
    elif kind == "packet":
        print("PACKET", json.dumps(event, ensure_ascii=False)[:400])
    elif kind == "signal":
        print("SIGNAL", json.dumps(event, ensure_ascii=False)[:300])
    elif kind == "log":
        print("LOG", event.get("level"), event.get("message"))
    elif kind == "error":
        print("ERROR", event)
    elif kind == "result":
        print("RESULT", json.dumps(event, ensure_ascii=False)[:600])
    else:
        print("EVENT", kind)

send({"cmd": "stop"})
end = time.time() + 10
while time.time() < end:
    event = read_one()
    if not event:
        continue
    kind = event.get("type")
    if kind == "scan":
        scan = event.get("scan") or {}
        print("SCAN-STOP running=%s err=%s" % (scan.get("running"), scan.get("last_error")))
    elif kind == "log":
        print("LOG", event.get("level"), event.get("message"))
    elif kind == "result" and event.get("cmd") == "stop":
        print("STOP-RESULT", json.dumps(event, ensure_ascii=False)[:600])
        break
sock.close()
