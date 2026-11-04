#!/usr/bin/env python3
"""Tiny SSH/scp helper for developing LoRa95 on the uConsole.

Usage:
    python tools/r.py run "uname -a"
    python tools/r.py put app/radio.py /tmp/radio.py
    python tools/r.py get /tmp/out.png shots/out.png
"""
from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

import paramiko

HOST = os.environ.get("LORA95_HOST", "")
USER = os.environ.get("LORA95_USER", "alex")
PASS = os.environ.get("LORA95_PASS", "")


def connect() -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        HOST,
        username=USER,
        password=PASS,
        timeout=15,
        banner_timeout=15,
        auth_timeout=15,
        look_for_keys=False,
        allow_agent=False,
    )
    return client


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    if not HOST or not PASS:
        print("Set LORA95_HOST and LORA95_PASS in the environment (never commit them).", file=sys.stderr)
        return 2
    action = sys.argv[1]
    client = connect()
    try:
        if action == "run":
            cmd = sys.argv[2]
            timeout = float(os.environ.get("LORA95_TIMEOUT", "180"))
            _in, out, err = client.exec_command(cmd, timeout=timeout)
            sys.stdout.write(out.read().decode("utf-8", "replace"))
            sys.stderr.write(err.read().decode("utf-8", "replace"))
            return out.channel.recv_exit_status()
        if action in ("put", "get"):
            sftp = client.open_sftp()
            if action == "put":
                data = Path(sys.argv[2]).read_bytes()
                with sftp.open(sys.argv[3], "wb") as fh:
                    fh.write(data)
                print(f"put {sys.argv[2]} -> {sys.argv[3]} ({len(data)} B)", file=sys.stderr)
            else:
                with sftp.open(sys.argv[2], "rb") as fh:
                    data = fh.read()
                Path(sys.argv[3]).write_bytes(data)
                print(f"get {sys.argv[2]} -> {sys.argv[3]} ({len(data)} B)", file=sys.stderr)
            sftp.close()
            return 0
        print(f"unknown action: {action}", file=sys.stderr)
        return 2
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
