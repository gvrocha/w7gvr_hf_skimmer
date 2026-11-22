#!/usr/bin/env python3
"""watch_decodes.py -- live FT8/FT4/WSPR decode monitor for hsd, run ON the Pi.

Connects to hsd.sock (same broadcast stream hsctl monitor uses) and groups
spot output into one block per decode window, with a header showing that
window's cycle_utc (GPS-disciplined, from the spot itself), band/dial
frequency, and the receiver's own grid square and position.

Every spot event already carries the receiver's position (rx_* fields,
from hsd's own GpsClock), so this tool no longer opens a second gpsd
connection of its own.

Run with: python3 tools/watch_decodes.py
"""

import json
import socket
import sys

HSD_SOCKET = "/root/w7gvr_hf_skimmer/hsd.sock"


def _location_str(d: dict) -> str:
    if not d.get("rx_grid"):
        return "location unknown (no GPS fix yet)"
    return f"{d['rx_grid']}  ({float(d['rx_lat']):.4f}, {float(d['rx_lon']):.4f})  fix age {d['rx_fix_age_s']}s"


def main() -> None:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(HSD_SOCKET)
    except (FileNotFoundError, ConnectionRefusedError):
        print(f"error: cannot connect to {HSD_SOCKET} -- is hsd running?", file=sys.stderr)
        sys.exit(1)

    current_block = None
    buf = b""

    print("Watching for decodes... (Ctrl-C to stop)")
    try:
        while True:
            data = sock.recv(4096)
            if not data:
                print("hsd closed the connection", file=sys.stderr)
                break
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                obj = json.loads(line)
                if obj.get("event") != "spot":
                    continue
                d = obj["data"]
                block = d["cycle_utc"]
                if block != current_block:
                    current_block = block
                    print(f"\n\n===== {block} UTC  |  {d['band']} dial {d['dial_hz']} Hz  |  {_location_str(d)} =====")
                print(f"  {d['mode']:<4} {d['snr']:>6} dB  {d['audio_hz']:>5} Hz  ({d['rf_hz']} Hz)  {d['message']}")
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
