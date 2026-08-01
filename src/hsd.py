#!/usr/bin/env python3
"""hsd -- core daemon for w7gvr_hf_skimmer.

IPC protocol (line-delimited JSON on ./hsd.sock), mirroring mobile_aprs_gateway's
magd.sock shape rather than inventing a new one:

  Commands (client -> daemon): {"cmd": <str>, ...}
    {"cmd": "start"}   -- begin a decode session
    {"cmd": "stop"}    -- end the current session
    {"cmd": "status"}  -- request a status snapshot

  Events (daemon -> client): {"event": <str>, ...}
    {"event": "ack", "message": <str>}
    {"event": "status", "listening": bool, "mode": str, "decoder": str,
               "session_seq": str|null, "spot_count": int}
    {"event": "spot", "data": {utc_timestamp, mode, snr, dt, freq_hz,
               message, call, grid, dbm, drift}}
    {"event": "error", "message": <str>}

A "status" event is sent automatically to every client on connect. "spot"
events are broadcast to all connected clients as they're decoded.

NOTE: this version only manages decode_worker.DecodeWorker -- WAV chunks
must be supplied into CHUNK_DIR externally (e.g. dropped in manually for
testing). capture_worker.py integration (real rtl_fm/rtl_sdr capture) is
a follow-up, tracked separately since it needs no radio to be written but
does need one to be hardware-validated.
"""

import csv
import json
import logging
import os
import socket
import sys
import threading
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from decode_worker import DecodeWorker, Spot  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "config.json"
SOCKET_PATH = REPO_ROOT / "hsd.sock"
CHUNK_DIR = REPO_ROOT / "chunks"
SESSIONS_DIR = REPO_ROOT / "sessions"
LOGS_DIR = REPO_ROOT / "logs"
LOG_COUNTER_FILE = REPO_ROOT / ".hsd_log_counter"

HSD_VERSION = "0.1"

DEFAULT_CONFIG = {
    "band": "20m",
    "mode": "wspr",
    "dial_frequency": "14.095M",
    "sdr_device": "rtl_sdr",
    "gain": "40",
    "sample_rate": "12000",
    "decoder": "wsjtx",
    "listening": False,
}
# Everything else (band/mode/dial_frequency/sdr_device/gain/sample_rate/decoder)
# is static -- requires a daemon restart to change, matching magd.py's split.
_RUNTIME_KEYS = {"listening"}

SPOT_TSV_FIELDS = [
    "utc_timestamp", "mode", "snr", "dt", "freq_hz", "message",
    "call", "grid", "dbm", "drift",
]

config: dict = dict(DEFAULT_CONFIG)
_session_seq: Optional[str] = None
_tsv_path: Optional[Path] = None
_spot_count = 0
_decode_worker: Optional[DecodeWorker] = None
_state_lock = threading.Lock()

_log = logging.getLogger("hsd")


def _load_config() -> None:
    global config
    on_disk = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            on_disk.update(json.load(f))
    config = on_disk


def _save_config() -> None:
    on_disk = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            on_disk.update(json.load(f))
    for key in _RUNTIME_KEYS:
        on_disk[key] = config[key]
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(on_disk, f, indent=2)
        f.write("\n")


def _next_log_seq() -> str:
    try:
        n = int(LOG_COUNTER_FILE.read_text().strip()) + 1
    except (FileNotFoundError, ValueError):
        n = 1
    LOG_COUNTER_FILE.write_text(str(n))
    return f"{n:04d}"


def _spot_to_row(spot: Spot) -> dict:
    return {
        "utc_timestamp": spot.utc_timestamp.isoformat(),
        "mode": spot.mode,
        "snr": spot.snr,
        "dt": spot.dt,
        "freq_hz": spot.freq_hz,
        "message": spot.message,
        "call": spot.call or "",
        "grid": spot.grid or "",
        "dbm": spot.dbm if spot.dbm is not None else "",
        "drift": spot.drift if spot.drift is not None else "",
    }


def _init_session() -> None:
    global _session_seq, _tsv_path, _spot_count
    _session_seq = _next_log_seq()
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    _tsv_path = SESSIONS_DIR / f"decodes_{_session_seq}_hsd_v{HSD_VERSION}.tsv"
    _spot_count = 0
    with open(_tsv_path, "w", newline="") as f:
        csv.DictWriter(f, fieldnames=SPOT_TSV_FIELDS, delimiter="\t").writeheader()


def _write_spot_tsv(spot: Spot) -> None:
    with open(_tsv_path, "a", newline="") as f:
        csv.DictWriter(f, fieldnames=SPOT_TSV_FIELDS, delimiter="\t").writerow(_spot_to_row(spot))


class _Client:
    def __init__(self, conn: socket.socket):
        self.conn = conn
        self.lock = threading.Lock()

    def send(self, obj: dict) -> None:
        try:
            with self.lock:
                self.conn.sendall((json.dumps(obj) + "\n").encode())
        except OSError:
            pass


class _ClientManager:
    def __init__(self):
        self._clients: list = []
        self._lock = threading.Lock()

    def add(self, client: _Client) -> None:
        with self._lock:
            self._clients.append(client)

    def remove(self, client: _Client) -> None:
        with self._lock:
            if client in self._clients:
                self._clients.remove(client)

    def broadcast(self, obj: dict) -> None:
        with self._lock:
            clients = list(self._clients)
        for c in clients:
            c.send(obj)


_clients = _ClientManager()


def _on_spot(spot: Spot) -> None:
    global _spot_count
    with _state_lock:
        _spot_count += 1
        _write_spot_tsv(spot)
    _clients.broadcast({"event": "spot", "data": _spot_to_row(spot)})


def start_listening() -> None:
    global _decode_worker
    with _state_lock:
        if config["listening"]:
            return
        _init_session()
        CHUNK_DIR.mkdir(parents=True, exist_ok=True)
        _decode_worker = DecodeWorker(
            chunk_dir=CHUNK_DIR,
            decoder=config["decoder"],
            mode=config["mode"],
            results_callback=_on_spot,
        )
        _decode_worker.start()
        config["listening"] = True
        _save_config()
        _log.info("started session %s (mode=%s decoder=%s)", _session_seq, config["mode"], config["decoder"])


def stop_listening() -> None:
    global _decode_worker
    with _state_lock:
        if not config["listening"]:
            return
        if _decode_worker:
            _decode_worker.stop()
            _decode_worker = None
        config["listening"] = False
        _save_config()
        _log.info("stopped session %s (%d spots)", _session_seq, _spot_count)


def _status_snapshot() -> dict:
    return {
        "event": "status",
        "listening": config["listening"],
        "mode": config["mode"],
        "decoder": config["decoder"],
        "session_seq": _session_seq,
        "spot_count": _spot_count,
    }


def _handle_command(client: _Client, obj: dict) -> None:
    cmd = obj.get("cmd")
    if cmd == "start":
        start_listening()
        client.send({"event": "ack", "message": "started"})
    elif cmd == "stop":
        stop_listening()
        client.send({"event": "ack", "message": "stopped"})
    elif cmd == "status":
        client.send(_status_snapshot())
    else:
        client.send({"event": "error", "message": f"unknown command: {cmd!r}"})


def _client_handler(conn: socket.socket) -> None:
    client = _Client(conn)
    _clients.add(client)
    client.send(_status_snapshot())
    buf = b""
    try:
        while True:
            data = conn.recv(4096)
            if not data:
                break
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    client.send({"event": "error", "message": "invalid JSON"})
                    continue
                _handle_command(client, obj)
    except OSError:
        pass
    finally:
        _clients.remove(client)
        conn.close()


def _run_socket_server() -> None:
    if SOCKET_PATH.exists():
        SOCKET_PATH.unlink()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(SOCKET_PATH))
    os.chmod(SOCKET_PATH, 0o660)
    srv.listen(5)
    _log.info("listening on %s", SOCKET_PATH)
    while True:
        conn, _addr = srv.accept()
        threading.Thread(target=_client_handler, args=(conn,), daemon=True).start()


def main() -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        filename=str(LOGS_DIR / "hsd.log"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    _load_config()
    if config.get("listening"):
        start_listening()
    _run_socket_server()


if __name__ == "__main__":
    main()
