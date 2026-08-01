#!/usr/bin/env python3
"""hsctl -- CLI client for hsd, mirroring mobile_aprs_gateway's magctl.py."""

import argparse
import json
import socket
import sys
from pathlib import Path

# os.path.realpath (Path.resolve() does the same) so this still finds hsd.sock
# next to the real script even when invoked through a /usr/local/bin symlink.
BASE_DIR = Path(__file__).resolve().parent.parent
SOCKET_PATH = BASE_DIR / "hsd.sock"


def _connect() -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(str(SOCKET_PATH))
    except (FileNotFoundError, ConnectionRefusedError):
        print(f"error: cannot connect to {SOCKET_PATH}", file=sys.stderr)
        print("       Is hsd running?", file=sys.stderr)
        sys.exit(1)
    return sock


def _send(sock: socket.socket, obj: dict) -> None:
    sock.sendall((json.dumps(obj) + "\n").encode())


def _read_events(sock: socket.socket):
    buf = b""
    while True:
        try:
            data = sock.recv(4096)
        except TimeoutError:
            return
        if not data:
            return
        buf += data
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if not line.strip():
                continue
            yield json.loads(line)


def _wait_for(sock: socket.socket, event_name: str, timeout: float = 5):
    sock.settimeout(timeout)
    for obj in _read_events(sock):
        if obj.get("event") == event_name:
            return obj
    return None


def cmd_start(args) -> None:
    sock = _connect()
    _send(sock, {"cmd": "start"})
    obj = _wait_for(sock, "ack")
    print(obj.get("message", "started") if obj else "no response from hsd")


def cmd_stop(args) -> None:
    sock = _connect()
    _send(sock, {"cmd": "stop"})
    obj = _wait_for(sock, "ack")
    print(obj.get("message", "stopped") if obj else "no response from hsd")


def cmd_status(args) -> None:
    sock = _connect()
    _send(sock, {"cmd": "status"})
    obj = _wait_for(sock, "status")
    if not obj:
        print("no response from hsd", file=sys.stderr)
        sys.exit(1)
    print(f"listening: {obj['listening']}")
    print(f"mode:      {obj['mode']}")
    print(f"decoder:   {obj['decoder']}")
    print(f"session:   {obj['session_seq']}")
    print(f"spots:     {obj['spot_count']}")


def cmd_monitor(args) -> None:
    sock = _connect()
    sock.settimeout(None)
    for obj in _read_events(sock):
        event = obj.get("event")
        if event == "spot":
            d = obj["data"]
            print(f"{d['utc_timestamp']}  {d['mode']:<4} {d['snr']:>5}  {d['freq_hz']:>9.1f} Hz  {d['message']}")
        elif event == "error":
            print(f"error: {obj.get('message')}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(prog="hsctl")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("start")
    sub.add_parser("stop")
    sub.add_parser("status")
    sub.add_parser("monitor")

    args = parser.parse_args()
    dispatch = {
        "start": cmd_start,
        "stop": cmd_stop,
        "status": cmd_status,
        "monitor": cmd_monitor,
    }
    if args.command not in dispatch:
        parser.print_help()
        sys.exit(1)
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
