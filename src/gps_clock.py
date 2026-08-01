"""gps_clock.py -- GPS-vs-system-clock offset discipline for hsd.

Ported from mobile_aprs_gateway's magd.py _read_gps/_gps_timestamp pattern:
rather than trusting system clock directly, poll GPS periodically and track
the offset between GPS time and system time. Timestamps are then computed
as system-clock-now + offset, so one dropped fix doesn't stall
timestamping -- it just free-runs on the last known offset until the next
good fix corrects it.

Hardware-validated 2026-08-01 against a real u-blox 7 GPS/GNSS receiver
(the same model mobile_aprs_gateway uses) on macOS: gpsd has no bundled
Python bindings on this platform (Homebrew's gpsd formula ships none, and
there's no "gps" package reliably available via pip either), so GpsdSource
talks to gpsd directly over its own JSON socket protocol (127.0.0.1:2947)
using only stdlib socket/json -- confirmed working end-to-end, including
the no-fix case (mode=1, no "time" field) tested indoors before a fix was
acquired. This is also more portable to the Pi than depending on
platform-specific bindings there too, since gpsd's wire protocol is
identical everywhere gpsd itself runs.
"""

import json
import socket
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional, Protocol


class GpsReport(Protocol):
    """Minimal shape this module needs from a GPS fix report -- a gpsd TPV
    report already satisfies this; a test stub just needs to match it."""

    mode: int  # gpsd fix mode: 0/1 = no fix, 2 = 2D fix, 3 = 3D fix
    time: Optional[str]  # ISO8601 UTC timestamp string from the fix, or None


class GpsSource(Protocol):
    def next(self) -> GpsReport: ...


class _TpvReport:
    def __init__(self, mode: int, time: Optional[str]):
        self.mode = mode
        self.time = time


class GpsdSource:
    """Talks to gpsd over its own JSON socket protocol (default
    127.0.0.1:2947), filtering for TPV (time-position-velocity) reports --
    stdlib-only, no gpsd Python bindings required.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 2947, timeout: float = 5.0):
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.settimeout(timeout)
        self._buf = b""
        self._readline()  # discard gpsd's VERSION banner
        self._sock.sendall(b'?WATCH={"enable":true,"json":true}\n')

    def _readline(self) -> dict:
        while b"\n" not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("gpsd closed the connection")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return json.loads(line)

    def next(self) -> GpsReport:
        while True:
            obj = self._readline()
            if obj.get("class") == "TPV":
                return _TpvReport(mode=obj.get("mode", 0), time=obj.get("time"))


class GpsClock:
    """Polls a GpsSource on a background thread and tracks the GPS-vs-system
    offset. Cadence mirrors magd.py: fixed_interval between polls once a fix
    is held, retry_interval while hunting for one.
    """

    def __init__(self, source: GpsSource, fixed_interval: float = 30.0, retry_interval: float = 1.0):
        self._source = source
        self.fixed_interval = fixed_interval
        self.retry_interval = retry_interval
        self._lock = threading.Lock()
        self._offset: Optional[float] = None  # seconds, GPS - system, at last good fix
        self._last_fix_mode = 0
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def poll_once(self) -> bool:
        """Read one report from the GPS source; update the offset on a good
        fix. Returns True if a usable (mode >= 2, has a time) fix was read.
        """
        try:
            report = self._source.next()
        except StopIteration:
            return False

        mode = getattr(report, "mode", 0)
        with self._lock:
            self._last_fix_mode = mode

        report_time = getattr(report, "time", None)
        if mode < 2 or not report_time:
            return False

        gps_dt = datetime.fromisoformat(report_time.replace("Z", "+00:00"))
        if gps_dt.tzinfo is None:
            gps_dt = gps_dt.replace(tzinfo=timezone.utc)
        system_dt = datetime.now(timezone.utc)
        with self._lock:
            self._offset = (gps_dt - system_dt).total_seconds()
        return True

    def is_ready(self) -> bool:
        with self._lock:
            return self._offset is not None

    def timestamp(self) -> datetime:
        """Current UTC time, corrected by the last known GPS offset. Falls
        back to plain system time if no fix has ever been seen."""
        with self._lock:
            offset = self._offset or 0.0
        return datetime.now(timezone.utc) + timedelta(seconds=offset)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            got_fix = self.poll_once()
            interval = self.fixed_interval if got_fix else self.retry_interval
            self._stop_event.wait(interval)
