"""Unit tests for src/hsd.py's config/session logic.

Uses a stub DecodeWorker so these tests don't depend on real decoder
binaries being built -- decode_worker.py's own behavior is covered by
test_decode_worker.py. Run with:
  PYTHONPATH=src python3 -m unittest tests/test_hsd.py -v
"""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import hsd  # noqa: E402
from decode_worker import Spot  # noqa: E402
from gps_clock import GpsPosition  # noqa: E402


class _StubDecodeWorker:
    """Stands in for decode_worker.DecodeWorker so these tests never touch
    real decoder binaries or spawn a real polling thread."""

    instances = []

    def __init__(self, chunk_dir, decoder, mode, results_callback, **kwargs):
        self.chunk_dir = chunk_dir
        self.decoder = decoder
        self.mode = mode
        self.results_callback = results_callback
        self.started = False
        self.stopped = False
        _StubDecodeWorker.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class _StubCaptureWorker:
    """Stands in for capture_worker.CaptureWorker so these tests never spawn
    a real rtl_fm subprocess."""

    instances = []

    def __init__(self, capture_cmd, chunk_dir, mode, sample_rate, now_fn=None, **kwargs):
        self.capture_cmd = capture_cmd
        self.chunk_dir = chunk_dir
        self.mode = mode
        self.sample_rate = sample_rate
        self.now_fn = now_fn
        self.started = False
        self.stopped = False
        _StubCaptureWorker.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class _StubGpsdSource:
    """Stands in for gps_clock.GpsdSource -- raising_on_init lets a test
    simulate gpsd being unreachable at startup."""

    raising_on_init = False

    def __init__(self):
        if _StubGpsdSource.raising_on_init:
            raise ConnectionRefusedError("gpsd not running")


class _StubGpsClock:
    """Stands in for gps_clock.GpsClock -- fixed_timestamp lets a test
    control what timestamp() returns without a real fix."""

    instances = []

    def __init__(self, source, **kwargs):
        self.source = source
        self.kwargs = kwargs
        self.started = False
        self.ready = True
        self.position = None
        self.fixed_timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
        _StubGpsClock.instances.append(self)

    def start(self):
        self.started = True

    def is_ready(self):
        return self.ready

    def last_position(self):
        return self.position

    def timestamp(self):
        return self.fixed_timestamp


class HsdTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self._tmp.name)

        self._orig = {
            "CONFIG_PATH": hsd.CONFIG_PATH,
            "SESSIONS_DIR": hsd.SESSIONS_DIR,
            "CHUNK_DIR": hsd.CHUNK_DIR,
            "LOG_COUNTER_FILE": hsd.LOG_COUNTER_FILE,
            "DecodeWorker": hsd.DecodeWorker,
            "CaptureWorker": hsd.CaptureWorker,
            "GpsdSource": hsd.GpsdSource,
            "GpsClock": hsd.GpsClock,
            "config": dict(hsd.config),
        }

        hsd.CONFIG_PATH = tmp_path / "config.json"
        hsd.SESSIONS_DIR = tmp_path / "sessions"
        hsd.CHUNK_DIR = tmp_path / "chunks"
        hsd.LOG_COUNTER_FILE = tmp_path / ".log_counter"
        hsd.DecodeWorker = _StubDecodeWorker
        hsd.CaptureWorker = _StubCaptureWorker
        hsd.GpsdSource = _StubGpsdSource
        hsd.GpsClock = _StubGpsClock
        hsd.config = dict(hsd.DEFAULT_CONFIG)
        hsd._session_seq = None
        hsd._session_start = None
        hsd._session_band = ""
        hsd._session_dial_hz = None
        hsd._spot_count = 0
        hsd._decode_worker = None
        hsd._capture_worker = None
        hsd._gps_clock = None
        _StubDecodeWorker.instances = []
        _StubCaptureWorker.instances = []
        _StubGpsdSource.raising_on_init = False
        _StubGpsClock.instances = []

    def tearDown(self):
        for key, value in self._orig.items():
            setattr(hsd, key, value)
        self._tmp.cleanup()

    def test_next_log_seq_increments_and_persists(self):
        self.assertEqual(hsd._next_log_seq(), "0001")
        self.assertEqual(hsd._next_log_seq(), "0002")
        self.assertEqual(hsd.LOG_COUNTER_FILE.read_text(), "2")

    def test_save_config_only_persists_runtime_keys(self):
        hsd.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        hsd.CONFIG_PATH.write_text(json.dumps({**hsd.DEFAULT_CONFIG, "mode": "ft8"}))
        hsd._load_config()
        self.assertEqual(hsd.config["mode"], "ft8")

        # Flip a static key only in memory (simulating drift), then a
        # runtime-key save should NOT clobber the on-disk static value.
        hsd.config["mode"] = "wspr"
        hsd.config["listening"] = True
        hsd._save_config()

        on_disk = json.loads(hsd.CONFIG_PATH.read_text())
        self.assertEqual(on_disk["mode"], "ft8")
        self.assertEqual(on_disk["listening"], True)

    def _ft8_spot(self):
        return Spot(
            utc_timestamp=datetime(2026, 6, 24, 2, 48, 45, tzinfo=timezone.utc),
            mode="ft8",
            snr=2.0,
            dt=0.5,
            audio_hz=1488.0,
            message="N8PFK WF3H RR73",
        )

    def test_spot_to_row_formats_cycle_utc(self):
        row = hsd._spot_to_row(self._ft8_spot())
        self.assertEqual(row["cycle_utc"], "2026/06/24 02:48:45")
        self.assertEqual(row["tx_call"], "")
        self.assertEqual(list(row.keys()), hsd.SPOT_TSV_FIELDS)

    def test_spot_to_row_tags_band_dial_and_rf_frequency(self):
        hsd._session_band = "15m"
        hsd._session_dial_hz = 21074000
        row = hsd._spot_to_row(self._ft8_spot())
        self.assertEqual(row["band"], "15m")
        self.assertEqual(row["dial_hz"], 21074000)
        self.assertEqual(row["audio_hz"], 1488)
        self.assertEqual(row["rf_hz"], 21075488)

    def test_spot_to_row_blank_rx_fields_without_fix(self):
        row = hsd._spot_to_row(self._ft8_spot(), position=None)
        for key in ("rx_lat", "rx_lon", "rx_alt_m", "rx_grid", "rx_fix_mode", "rx_fix_age_s"):
            self.assertEqual(row[key], "", key)

    def test_spot_to_row_fills_rx_fields_from_position(self):
        fix_time = datetime(2026, 6, 24, 2, 48, 50, tzinfo=timezone.utc)
        position = GpsPosition(lat=47.612345, lon=-122.334567, alt=52.14, mode=3, fix_time=fix_time)
        written_at = datetime(2026, 6, 24, 2, 49, 4, tzinfo=timezone.utc)
        row = hsd._spot_to_row(self._ft8_spot(), position=position, written_at=written_at)
        self.assertEqual(row["rx_lat"], "47.612345")
        self.assertEqual(row["rx_lon"], "-122.334567")
        self.assertEqual(row["rx_alt_m"], "52.1")
        self.assertEqual(row["rx_grid"], "CN87to")
        self.assertEqual(row["rx_fix_mode"], 3)
        self.assertEqual(row["rx_fix_age_s"], 14)

    def test_spot_to_row_2d_fix_has_blank_altitude(self):
        fix_time = datetime(2026, 6, 24, 2, 48, 50, tzinfo=timezone.utc)
        position = GpsPosition(lat=47.6, lon=-122.3, alt=None, mode=2, fix_time=fix_time)
        row = hsd._spot_to_row(self._ft8_spot(), position=position, written_at=fix_time)
        self.assertEqual(row["rx_alt_m"], "")
        self.assertEqual(row["rx_fix_age_s"], 0)

    def test_parse_frequency_hz_accepts_config_styles(self):
        self.assertEqual(hsd.parse_frequency_hz("21074000"), 21074000)
        self.assertEqual(hsd.parse_frequency_hz("14.074M"), 14074000)
        self.assertEqual(hsd.parse_frequency_hz("14.095M"), 14095000)
        self.assertEqual(hsd.parse_frequency_hz("14074k"), 14074000)
        self.assertEqual(hsd.parse_frequency_hz(7074000), 7074000)
        with self.assertRaises(ValueError):
            hsd.parse_frequency_hz("twenty meters")

    def test_session_filename_carries_start_timestamp(self):
        clock = _StubGpsClock(source=None)
        clock.fixed_timestamp = datetime(2026, 11, 22, 16, 19, 30, tzinfo=timezone.utc)
        hsd._gps_clock = clock
        hsd.config["band"] = "15m"
        hsd.config["dial_frequency"] = "21074000"
        hsd._init_session()
        self.assertEqual(hsd._tsv_path.name, f"decodes_20261122_161930_0001_hsd_v{hsd.HSD_VERSION}.tsv")
        self.assertEqual(hsd._session_band, "15m")
        self.assertEqual(hsd._session_dial_hz, 21074000)
        header = hsd._tsv_path.read_text().splitlines()[0].split("\t")
        self.assertEqual(header, hsd.SPOT_TSV_FIELDS)

    def test_start_listening_creates_session_and_starts_worker(self):
        hsd.start_listening()
        self.assertTrue(hsd.config["listening"])
        self.assertEqual(hsd._session_seq, "0001")
        self.assertTrue(hsd._tsv_path.exists())
        self.assertEqual(len(_StubDecodeWorker.instances), 1)
        self.assertTrue(_StubDecodeWorker.instances[0].started)
        self.assertEqual(len(_StubCaptureWorker.instances), 1)
        self.assertTrue(_StubCaptureWorker.instances[0].started)
        self.assertEqual(_StubCaptureWorker.instances[0].chunk_dir, hsd.CHUNK_DIR)
        self.assertEqual(_StubCaptureWorker.instances[0].sample_rate, int(hsd.config["sample_rate"]))
        self.assertIs(_StubCaptureWorker.instances[0].now_fn, hsd._now_utc)

    def test_start_listening_is_idempotent(self):
        hsd.start_listening()
        hsd.start_listening()
        self.assertEqual(len(_StubDecodeWorker.instances), 1)
        self.assertEqual(len(_StubCaptureWorker.instances), 1)

    def test_stop_listening_stops_worker(self):
        hsd.start_listening()
        decode_worker = _StubDecodeWorker.instances[0]
        capture_worker = _StubCaptureWorker.instances[0]
        hsd.stop_listening()
        self.assertFalse(hsd.config["listening"])
        self.assertTrue(decode_worker.stopped)
        self.assertTrue(capture_worker.stopped)

    def test_on_spot_writes_tsv_row_and_increments_count(self):
        hsd.start_listening()
        spot = Spot(
            utc_timestamp=datetime(2026, 6, 24, 2, 48, 45, tzinfo=timezone.utc),
            mode="ft8", snr=2.0, dt=0.5, audio_hz=1488.0, message="N8PFK WF3H RR73",
        )
        clock = _StubGpsClock(source=None)
        clock.position = GpsPosition(
            lat=47.6, lon=-122.3, alt=None, mode=2,
            fix_time=clock.fixed_timestamp,
        )
        hsd._gps_clock = clock
        hsd._on_spot(spot)
        self.assertEqual(hsd._spot_count, 1)
        rows = hsd._tsv_path.read_text().splitlines()
        self.assertEqual(len(rows), 2)  # header + one row
        self.assertIn("N8PFK WF3H RR73", rows[1])
        self.assertIn("CN87", rows[1])  # rx_grid from the GPS clock's position

    def test_now_utc_falls_back_to_system_time_by_default(self):
        before = datetime.now(timezone.utc)
        result = hsd._now_utc()
        after = datetime.now(timezone.utc)
        self.assertLessEqual(before, result)
        self.assertLessEqual(result, after)

    def test_now_utc_delegates_to_gps_clock_when_set(self):
        stub_clock = _StubGpsClock(source=None)
        hsd._gps_clock = stub_clock
        self.assertEqual(hsd._now_utc(), stub_clock.fixed_timestamp)

    def test_start_gps_clock_success_sets_and_starts_clock(self):
        hsd._start_gps_clock()
        self.assertIsNotNone(hsd._gps_clock)
        self.assertTrue(hsd._gps_clock.started)
        self.assertEqual(len(_StubGpsClock.instances), 1)

    def test_start_gps_clock_handles_unreachable_gpsd_gracefully(self):
        _StubGpsdSource.raising_on_init = True
        hsd._start_gps_clock()  # must not raise
        self.assertIsNone(hsd._gps_clock)


if __name__ == "__main__":
    unittest.main()
