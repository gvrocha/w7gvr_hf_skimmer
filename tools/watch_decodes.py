#!/usr/bin/env python3
"""watch_decodes.py -- live FT8/FT4/WSPR decode monitor for hsd, run ON the Pi.

Connects to hsd.sock (same broadcast stream hsctl monitor uses) and groups
spot output into one block per decode window, with a header showing that
window's real UTC time (from the spot's own GPS-disciplined utc_timestamp,
not wall-clock-at-print-time) and the station's current grid square
(polled fresh from gpsd for each new block).

Run with: python3 tools/watch_decodes.py
"""

import json
import socket
import sys

HSD_SOCKET = "/root/w7gvr_hf_skimmer/hsd.sock"
GPSD_HOST, GPSD_PORT = "127.0.0.1", 2947


def _grid_square(lat: float, lon: float) -> str:
    """6-character Maidenhead locator for (lat, lon)."""
    lon, lat = lon + 180, lat + 90
    a = ord("A")
    field_lon, field_lat = int(lon // 20), int(lat // 10)
    square_lon, square_lat = int((lon % 20) // 2), int(lat % 10)
    subsq_lon = int(((lon % 20) % 2) * 12)
    subsq_lat = int((lat % 1) * 24)
    return "%c%c%d%d%c%c" % (
        a + field_lon, a + field_lat, square_lon, square_lat,
        chr(ord("a") + subsq_lon), chr(ord("a") + subsq_lat),
    )


class GpsdPoller:
    """One persistent gpsd connection, polled on demand via gpsd's own
    ?POLL; command -- returns the last cached fix instantly, no waiting
    for a new report cycle."""

    def __init__(self):
        self._sock = None

    def _ensure_connected(self):
        if self._sock is not None:
            return
        try:
            self._sock = socket.create_connection((GPSD_HOST, GPSD_PORT), timeout=2)
            self._sock.settimeout(2)
            self._sock.recv(4096)  # discard VERSION banner
        except OSError:
            self._sock = None

    def location_str(self) -> str:
        self._ensure_connected()
        if self._sock is None:
            return "location unknown (gpsd unreachable)"
        try:
            self._sock.sendall(b"?POLL;\n")
            data = self._sock.recv(65536)
            obj = json.loads(data.splitlines()[0])
            tpv = obj.get("tpv") or []
            fix = tpv[-1] if tpv else {}
            if fix.get("mode", 0) < 2 or "lat" not in fix:
                return "location unknown (no GPS fix)"
            grid = _grid_square(fix["lat"], fix["lon"])
            return f"{grid}  ({fix['lat']:.4f}, {fix['lon']:.4f})"
        except (OSError, ValueError, IndexError, KeyError):
            self._sock = None
            return "location unknown (gpsd read error)"


def main() -> None:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(HSD_SOCKET)
    except (FileNotFoundError, ConnectionRefusedError):
        print(f"error: cannot connect to {HSD_SOCKET} -- is hsd running?", file=sys.stderr)
        sys.exit(1)

    gpsd = GpsdPoller()
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
                block = d["utc_timestamp"]
                if block != current_block:
                    current_block = block
                    print(f"\n\n===== {block}  UTC  |  {gpsd.location_str()} =====")
                print(f"  {d['mode']:<4} {d['snr']:>6} dB  {d['freq_hz']:>9.1f} Hz  {d['message']}")
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
