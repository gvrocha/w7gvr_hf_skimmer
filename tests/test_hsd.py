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

    def __init__(self, capture_cmd, chunk_dir, mode, sample_rate, **kwargs):
        self.capture_cmd = capture_cmd
        self.chunk_dir = chunk_dir
        self.mode = mode
        self.sample_rate = sample_rate
        self.started = False
        self.stopped = False
        _StubCaptureWorker.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


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
            "config": dict(hsd.config),
        }

        hsd.CONFIG_PATH = tmp_path / "config.json"
        hsd.SESSIONS_DIR = tmp_path / "sessions"
        hsd.CHUNK_DIR = tmp_path / "chunks"
        hsd.LOG_COUNTER_FILE = tmp_path / ".log_counter"
        hsd.DecodeWorker = _StubDecodeWorker
        hsd.CaptureWorker = _StubCaptureWorker
        hsd.config = dict(hsd.DEFAULT_CONFIG)
        hsd._session_seq = None
        hsd._spot_count = 0
        hsd._decode_worker = None
        hsd._capture_worker = None
        _StubDecodeWorker.instances = []
        _StubCaptureWorker.instances = []

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

    def test_spot_to_row_serializes_utc_timestamp(self):
        spot = Spot(
            utc_timestamp=datetime(2026, 6, 24, 2, 48, 45, tzinfo=timezone.utc),
            mode="ft8",
            snr=2.0,
            dt=0.5,
            freq_hz=1488.0,
            message="N8PFK WF3H RR73",
        )
        row = hsd._spot_to_row(spot)
        self.assertEqual(row["utc_timestamp"], "2026-06-24T02:48:45+00:00")
        self.assertEqual(row["call"], "")

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
            mode="ft8", snr=2.0, dt=0.5, freq_hz=1488.0, message="N8PFK WF3H RR73",
        )
        hsd._on_spot(spot)
        self.assertEqual(hsd._spot_count, 1)
        rows = hsd._tsv_path.read_text().splitlines()
        self.assertEqual(len(rows), 2)  # header + one row
        self.assertIn("N8PFK WF3H RR73", rows[1])


if __name__ == "__main__":
    unittest.main()
