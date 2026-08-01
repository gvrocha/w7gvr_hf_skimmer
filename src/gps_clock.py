"""gps_clock.py -- GPS-vs-system-clock offset discipline for hsd.

Ported from mobile_aprs_gateway's magd.py _read_gps/_gps_timestamp pattern:
rather than trusting system clock directly, poll GPS periodically and track
the offset between GPS time and system time. Timestamps are then computed
as system-clock-now + offset, so one dropped fix doesn't stall
timestamping -- it just free-runs on the last known offset until the next
good fix corrects it.

No real GPS receiver is available yet (tracked as Track B-GPS in the
implementation plan). This module is built and unit-tested now against a
stub GPS source; GpsdSource (the real gpsd-backed adapter below) is a
faithful port of magd.py's gps.gps(mode=WATCH_ENABLE|WATCH_NEWSTYLE) call,
but is unverified against real hardware until a receiver is on hand.
"""

import threading
from datetime import datetime, timedelta, timezone
from typing import Optional, Protocol

try:
    import gps as _gpslib
except ImportError:
    _gpslib = None


class GpsReport(Protocol):
    """Minimal shape this module needs from a GPS fix report -- a gpsd TPV
    report already satisfies this; a test stub just needs to match it."""

    mode: int  # gpsd fix mode: 0/1 = no fix, 2 = 2D fix, 3 = 3D fix
    time: Optional[str]  # ISO8601 UTC timestamp string from the fix, or None


class GpsSource(Protocol):
    def next(self) -> GpsReport: ...


class GpsdSource:
    """Adapts gpsd's own `gps` session object to GpsSource's .next() shape."""

    def __init__(self):
        if _gpslib is None:
            raise RuntimeError("gpsd python bindings ('gps' module) not installed")
        self._session = _gpslib.gps(mode=_gpslib.WATCH_ENABLE | _gpslib.WATCH_NEWSTYLE)

    def next(self) -> GpsReport:
        return self._session.next()


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
