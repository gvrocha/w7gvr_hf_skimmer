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
               "band": str, "dial_hz": int|null,
               "session_seq": str|null, "session_start": str|null,
               "spot_count": int}
    {"event": "spot", "data": {<one TSV row, keyed by SPOT_TSV_FIELDS>}}
    {"event": "error", "message": <str>}

A "status" event is sent automatically to every client on connect. "spot"
events are broadcast to all connected clients as they're decoded.

start/stop drive both capture_worker.CaptureWorker (writes UTC-aligned WAV
chunks into CHUNK_DIR from a live capture) and decode_worker.DecodeWorker
(watches CHUNK_DIR and decodes each chunk as it lands) -- the two are wired
only through that shared directory, same as DecodeWorker's own doc comment
describes. Capture uses build_csdr_capture_cmd()'s `rtl_sdr | csdr` pipeline,
not rtl_fm directly -- rtl_fm has a real, reproducible demod-stage clipping
bug (see capture_worker.py's module docstring and
hardware/20260920_rtl_fm_demod_bug.md).

A gps_clock.GpsClock runs for the whole daemon lifetime (not just while a
session is active), started once in main(). CaptureWorker gets its chunk
boundaries from _now_utc(), which prefers the GPS-disciplined clock over
the Pi's own RTC-less system clock -- if gpsd isn't reachable at startup,
_start_gps_clock() logs a warning and _now_utc() falls back to plain
system time, same free-running philosophy as GpsClock.timestamp() itself.

Session TSVs (sessions/decodes_YYYYMMDD_HHMMSS_NNNN_hsd_v<ver>.tsv, the
timestamp being the session's start time) carry one row per decoded
message, tagged with the decode cycle's start (cycle_utc), the band/dial
frequency it was heard on, its offset inside the audio passband
(audio_hz) and absolute on-air frequency (rf_hz), and the receiver's own
position from the latest GPS fix (rx_*). See
planning/20261122_spot_tagging_plan.md for the column rationale.
"""

import csv
import json
import logging
import os
import socket
import sys
import threading
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from capture_worker import CaptureWorker, build_csdr_capture_cmd  # noqa: E402
from decode_worker import DecodeWorker, Spot  # noqa: E402
from gps_clock import GpsClock, GpsdSource  # noqa: E402
from maidenhead import grid_square  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "config.json"
SOCKET_PATH = REPO_ROOT / "hsd.sock"
CHUNK_DIR = REPO_ROOT / "chunks" / "rtlsdr"
SESSIONS_DIR = REPO_ROOT / "sessions"
LOGS_DIR = REPO_ROOT / "logs"
LOG_COUNTER_FILE = REPO_ROOT / ".hsd_log_counter"

HSD_VERSION = "0.2"

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
    "cycle_utc", "band", "dial_hz", "audio_hz", "rf_hz",
    "mode", "snr", "dt", "message",
    "tx_call", "tx_grid", "dbm", "drift",
    "rx_lat", "rx_lon", "rx_alt_m", "rx_grid", "rx_fix_mode", "rx_fix_age_s",
]

CYCLE_UTC_FORMAT = "%Y/%m/%d %H:%M:%S"
SESSION_FILE_TIME_FORMAT = "%Y%m%d_%H%M%S"

# How often GpsClock re-reads gpsd once it holds a fix. Also bounds how old
# a spot's rx_* position can be (rx_fix_age_s), so shorter than
# GpsClock's own 30s default.
GPS_POLL_INTERVAL_S = 10.0

config: dict = dict(DEFAULT_CONFIG)
_session_seq: Optional[str] = None
_tsv_path: Optional[Path] = None
_session_start: Optional[datetime] = None
_session_band: str = ""
_session_dial_hz: Optional[int] = None
_spot_count = 0
_decode_worker: Optional[DecodeWorker] = None
_capture_worker: Optional[CaptureWorker] = None
_gps_clock: Optional[GpsClock] = None
_state_lock = threading.Lock()

_log = logging.getLogger("hsd")


def _start_gps_clock() -> None:
    """Start a GpsClock for the daemon's whole lifetime. If gpsd isn't
    reachable (e.g. not installed/running yet), log a warning and leave
    _gps_clock as None -- _now_utc() falls back to system time in that
    case, same as GpsClock.timestamp() falls back before its first fix."""
    global _gps_clock
    try:
        source = GpsdSource()
    except OSError:
        _log.warning("gpsd unreachable at startup -- falling back to system clock for chunk timing")
        return
    _gps_clock = GpsClock(source, fixed_interval=GPS_POLL_INTERVAL_S)
    _gps_clock.start()


def _now_utc() -> datetime:
    if _gps_clock is not None:
        return _gps_clock.timestamp()
    return datetime.now(timezone.utc)


_FREQ_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([kKmMgG]?)\s*(?:[hH][zZ])?\s*$")
_FREQ_SCALE = {"": 1, "k": 1e3, "m": 1e6, "g": 1e9}


def parse_frequency_hz(value) -> int:
    """Parse a dial frequency in any of the forms config.json uses --
    '14074000', '14.074M', '14074k', 14074000 -- into integer Hz. The same
    suffix style rtl_sdr -f itself accepts."""
    if isinstance(value, (int, float)):
        return int(round(value))
    m = _FREQ_RE.match(str(value))
    if not m:
        raise ValueError(f"unparseable frequency: {value!r}")
    return int(round(float(m[1]) * _FREQ_SCALE[m[2].lower()]))


def _gps_position():
    if _gps_clock is None:
        return None
    last_position = getattr(_gps_clock, "last_position", None)
    return last_position() if last_position else None


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


def _blank(value):
    return "" if value is None else value


def _spot_to_row(spot: Spot, position=None, written_at: Optional[datetime] = None) -> dict:
    """One TSV row / socket spot event for a decoded message.

    position is a gps_clock.GpsPosition (or None if no fix has been seen
    yet -- all rx_* columns are then blank). rx_fix_age_s is how old that
    fix was at written_at (when the row was produced), not relative to
    cycle_utc: the decode is written ~one cycle after cycle_utc, so this
    is the honest "how stale is this position" number.
    """
    audio_hz = int(round(spot.audio_hz))
    rf_hz = _session_dial_hz + audio_hz if _session_dial_hz is not None else None
    row = {
        "cycle_utc": spot.utc_timestamp.astimezone(timezone.utc).strftime(CYCLE_UTC_FORMAT),
        "band": _session_band,
        "dial_hz": _blank(_session_dial_hz),
        "audio_hz": audio_hz,
        "rf_hz": _blank(rf_hz),
        "mode": spot.mode,
        "snr": spot.snr,
        "dt": spot.dt,
        "message": spot.message,
        "tx_call": spot.call or "",
        "tx_grid": spot.grid or "",
        "dbm": _blank(spot.dbm),
        "drift": _blank(spot.drift),
        "rx_lat": "", "rx_lon": "", "rx_alt_m": "", "rx_grid": "",
        "rx_fix_mode": "", "rx_fix_age_s": "",
    }
    if position is not None:
        written_at = written_at or _now_utc()
        row.update({
            "rx_lat": f"{position.lat:.6f}",
            "rx_lon": f"{position.lon:.6f}",
            "rx_alt_m": f"{position.alt:.1f}" if position.alt is not None else "",
            "rx_grid": grid_square(position.lat, position.lon),
            "rx_fix_mode": position.mode,
            "rx_fix_age_s": max(0, int(round((written_at - position.fix_time).total_seconds()))),
        })
    return row


def _init_session() -> None:
    global _session_seq, _tsv_path, _spot_count, _session_start, _session_band, _session_dial_hz
    _session_seq = _next_log_seq()
    _session_start = _now_utc()
    if _gps_clock is None or not _gps_clock.is_ready():
        _log.warning(
            "session %s starting without a GPS time fix -- filename timestamp %s "
            "comes from the system clock and may be wrong",
            _session_seq, _session_start.strftime(SESSION_FILE_TIME_FORMAT),
        )
    _session_band = config.get("band", "")
    try:
        _session_dial_hz = parse_frequency_hz(config["dial_frequency"])
    except (KeyError, ValueError):
        _log.warning("could not parse dial_frequency %r -- dial_hz/rf_hz will be blank",
                     config.get("dial_frequency"))
        _session_dial_hz = None
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _session_start.strftime(SESSION_FILE_TIME_FORMAT)
    _tsv_path = SESSIONS_DIR / f"decodes_{stamp}_{_session_seq}_hsd_v{HSD_VERSION}.tsv"
    _spot_count = 0
    with open(_tsv_path, "w", newline="") as f:
        csv.DictWriter(f, fieldnames=SPOT_TSV_FIELDS, delimiter="\t").writeheader()


def _write_spot_tsv(row: dict) -> None:
    with open(_tsv_path, "a", newline="") as f:
        csv.DictWriter(f, fieldnames=SPOT_TSV_FIELDS, delimiter="\t").writerow(row)


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
    row = _spot_to_row(spot, position=_gps_position())
    with _state_lock:
        _spot_count += 1
        _write_spot_tsv(row)
    _clients.broadcast({"event": "spot", "data": row})


def start_listening() -> None:
    global _decode_worker, _capture_worker
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
        capture_cmd = build_csdr_capture_cmd(config["dial_frequency"], config["sample_rate"], config["gain"])
        _capture_worker = CaptureWorker(
            capture_cmd=capture_cmd,
            chunk_dir=CHUNK_DIR,
            mode=config["mode"],
            sample_rate=int(config["sample_rate"]),
            now_fn=_now_utc,
        )
        _capture_worker.start()
        config["listening"] = True
        _save_config()
        _log.info(
            "started session %s (band=%s dial_hz=%s mode=%s decoder=%s) -> %s",
            _session_seq, _session_band, _session_dial_hz, config["mode"], config["decoder"], _tsv_path.name,
        )


def stop_listening() -> None:
    global _decode_worker, _capture_worker
    with _state_lock:
        if not config["listening"]:
            return
        if _capture_worker:
            _capture_worker.stop()
            _capture_worker = None
        if _decode_worker:
            _decode_worker.stop()
            _decode_worker = None
        config["listening"] = False
        _save_config()
        _log.info("stopped session %s (%d spots)", _session_seq, _spot_count)


def _safe_dial_hz() -> Optional[int]:
    try:
        return parse_frequency_hz(config.get("dial_frequency"))
    except ValueError:
        return None


def _status_snapshot() -> dict:
    return {
        "event": "status",
        "listening": config["listening"],
        "mode": config["mode"],
        "decoder": config["decoder"],
        "band": config.get("band", ""),
        "dial_hz": _safe_dial_hz(),
        "session_seq": _session_seq,
        "session_start": _session_start.strftime(SESSION_FILE_TIME_FORMAT) if _session_start else None,
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
    _start_gps_clock()
    if config.get("listening"):
        start_listening()
    _run_socket_server()


if __name__ == "__main__":
    main()
