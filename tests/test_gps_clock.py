"""Unit tests for src/gps_clock.py, using a stub GPS source (no gpsd/hardware
needed). Run with:
  PYTHONPATH=src python3 -m unittest tests/test_gps_clock.py -v
"""

import sys
import time
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from gps_clock import GpsClock  # noqa: E402

TOLERANCE_SECONDS = 1.0


@dataclass
class FakeReport:
    mode: int
    time: Optional[str]


class StubGpsSource:
    """Yields a fixed sequence of reports, then raises StopIteration."""

    def __init__(self, reports):
        self._reports = iter(reports)

    def next(self):
        return next(self._reports)


class FlakyGpsSource:
    """Yields a fixed sequence of items, where an item that's an exception
    instance is raised instead of returned -- stands in for GpsdSource
    hitting a transient socket error (e.g. TimeoutError) partway through
    an otherwise-working session."""

    def __init__(self, items):
        self._items = iter(items)

    def next(self):
        item = next(self._items)
        if isinstance(item, Exception):
            raise item
        return item


class TestPollOnce(unittest.TestCase):
    def test_good_3d_fix_sets_offset(self):
        gps_time = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
        clock = GpsClock(StubGpsSource([FakeReport(mode=3, time=gps_time)]))

        self.assertFalse(clock.is_ready())
        self.assertTrue(clock.poll_once())
        self.assertTrue(clock.is_ready())
        offset = clock.timestamp() - datetime.now(timezone.utc)
        self.assertAlmostEqual(offset.total_seconds(), 5.0, delta=TOLERANCE_SECONDS)

    def test_2d_fix_is_usable(self):
        gps_time = datetime.now(timezone.utc).isoformat()
        clock = GpsClock(StubGpsSource([FakeReport(mode=2, time=gps_time)]))
        self.assertTrue(clock.poll_once())
        self.assertTrue(clock.is_ready())

    def test_no_fix_does_not_set_offset(self):
        clock = GpsClock(StubGpsSource([FakeReport(mode=0, time=None)]))
        self.assertFalse(clock.poll_once())
        self.assertFalse(clock.is_ready())

    def test_fix_mode_without_time_is_not_usable(self):
        clock = GpsClock(StubGpsSource([FakeReport(mode=3, time=None)]))
        self.assertFalse(clock.poll_once())
        self.assertFalse(clock.is_ready())

    def test_zulu_suffix_time_format_is_parsed(self):
        gps_time = (datetime.now(timezone.utc) + timedelta(seconds=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        clock = GpsClock(StubGpsSource([FakeReport(mode=3, time=gps_time)]))
        self.assertTrue(clock.poll_once())
        offset = clock.timestamp() - datetime.now(timezone.utc)
        self.assertAlmostEqual(offset.total_seconds(), 2.0, delta=TOLERANCE_SECONDS)

    def test_exhausted_source_returns_false_without_raising(self):
        clock = GpsClock(StubGpsSource([]))
        self.assertFalse(clock.poll_once())

    def test_timeout_error_returns_false_without_raising(self):
        clock = GpsClock(FlakyGpsSource([TimeoutError("timed out")]))
        self.assertFalse(clock.poll_once())
        self.assertFalse(clock.is_ready())


class TestTimestamp(unittest.TestCase):
    def test_falls_back_to_system_time_before_any_fix(self):
        clock = GpsClock(StubGpsSource([]))
        offset = clock.timestamp() - datetime.now(timezone.utc)
        self.assertAlmostEqual(offset.total_seconds(), 0.0, delta=TOLERANCE_SECONDS)


class TestRunLoop(unittest.TestCase):
    def test_start_stop_acquires_fix_and_shuts_down_cleanly(self):
        gps_time = datetime.now(timezone.utc).isoformat()
        reports = [FakeReport(mode=0, time=None), FakeReport(mode=3, time=gps_time)]
        clock = GpsClock(StubGpsSource(reports), fixed_interval=10.0, retry_interval=0.1)
        clock.start()
        try:
            deadline = time.time() + 5
            while time.time() < deadline and not clock.is_ready():
                time.sleep(0.05)
            self.assertTrue(clock.is_ready())
        finally:
            clock.stop()

    def test_background_thread_survives_transient_error_and_recovers(self):
        """Regression test for the real production bug: a raw TimeoutError
        from the GPS source used to propagate out of poll_once() and kill
        the polling thread silently -- is_ready() would then stay False
        forever, even though a later poll would have succeeded. This
        proves the thread keeps polling past the error and reaches a fix
        on a subsequent cycle."""
        gps_time = datetime.now(timezone.utc).isoformat()
        items = [
            TimeoutError("timed out"),
            TimeoutError("timed out"),
            FakeReport(mode=3, time=gps_time),
        ]
        clock = GpsClock(FlakyGpsSource(items), fixed_interval=10.0, retry_interval=0.05)
        clock.start()
        try:
            deadline = time.time() + 5
            while time.time() < deadline and not clock.is_ready():
                time.sleep(0.05)
            self.assertTrue(clock.is_ready())
            self.assertTrue(clock._thread.is_alive())
        finally:
            clock.stop()


if __name__ == "__main__":
    unittest.main()
