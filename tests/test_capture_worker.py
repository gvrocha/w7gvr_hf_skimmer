"""Unit tests for src/capture_worker.py.

align_chunks()/next_boundary()/chunk_filename() are tested against a fake
in-memory byte source with a controlled start_time -- no hardware, no
real-time waiting. CaptureWorker itself gets one integration test against
a real, paced synthetic PCM-generator subprocess standing in for rtl_fm
(runs in real time, so this one test takes up to ~15s).

Run with: PYTHONPATH=src python3 -m unittest tests/test_capture_worker.py -v
"""

import io
import sys
import tempfile
import time
import unittest
import wave
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from capture_worker import (  # noqa: E402
    CaptureWorker,
    align_chunks,
    chunk_filename,
    next_boundary,
)

UTC = timezone.utc


class TestNextBoundary(unittest.TestCase):
    def test_mid_cycle_rounds_up_to_next_boundary(self):
        after = datetime(2026, 1, 1, 0, 0, 7, tzinfo=UTC)
        self.assertEqual(next_boundary(after, 15.0), datetime(2026, 1, 1, 0, 0, 15, tzinfo=UTC))

    def test_exactly_on_boundary_advances_to_next_one(self):
        after = datetime(2026, 1, 1, 0, 0, 15, tzinfo=UTC)
        self.assertEqual(next_boundary(after, 15.0), datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC))

    def test_ft4_cycle_boundaries(self):
        after = datetime(2026, 1, 1, 0, 0, 10, tzinfo=UTC)
        self.assertEqual(next_boundary(after, 7.5), datetime(2026, 1, 1, 0, 0, 15, tzinfo=UTC))

    def test_wspr_cycle_boundaries(self):
        after = datetime(2026, 1, 1, 0, 1, 30, tzinfo=UTC)
        self.assertEqual(next_boundary(after, 120.0), datetime(2026, 1, 1, 0, 2, 0, tzinfo=UTC))


class TestChunkFilename(unittest.TestCase):
    def test_matches_wsjtx_convention(self):
        self.assertEqual(
            chunk_filename(datetime(2026, 6, 24, 2, 48, 45, tzinfo=UTC)), "260624_024845.wav"
        )


class TestAlignChunks(unittest.TestCase):
    def test_discards_warmup_and_yields_exact_boundary_aligned_chunks(self):
        cycle_seconds = 15.0
        bytes_per_second = 12000 * 2
        chunk_bytes = int(cycle_seconds * bytes_per_second)
        start_time = datetime(2026, 1, 1, 0, 0, 7, tzinfo=UTC)  # -> boundary at :15, 8s warmup
        warmup_bytes = 8 * bytes_per_second

        warmup_data = b"\xaa" * warmup_bytes
        chunk1_data = b"\x01" * chunk_bytes
        chunk2_data = b"\x02" * chunk_bytes
        stream = io.BytesIO(warmup_data + chunk1_data + chunk2_data)

        chunks = list(align_chunks(stream.read, start_time, cycle_seconds, bytes_per_second))

        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0][0], datetime(2026, 1, 1, 0, 0, 15, tzinfo=UTC))
        self.assertEqual(chunks[0][1], chunk1_data)
        self.assertEqual(chunks[1][0], datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC))
        self.assertEqual(chunks[1][1], chunk2_data)

    def test_stops_on_incomplete_final_chunk(self):
        cycle_seconds = 15.0
        bytes_per_second = 12000 * 2
        chunk_bytes = int(cycle_seconds * bytes_per_second)
        start_time = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)  # boundary at :15, 15s warmup

        stream = io.BytesIO(b"\x00" * (15 * bytes_per_second) + b"\x01" * (chunk_bytes // 2))
        chunks = list(align_chunks(stream.read, start_time, cycle_seconds, bytes_per_second))
        self.assertEqual(chunks, [])


class TestCaptureWorkerIntegration(unittest.TestCase):
    def test_writes_utc_aligned_wav_chunks_from_real_subprocess(self):
        # A paced synthetic generator standing in for rtl_fm: emits one
        # second of 16-bit mono silence per second, in real time.
        pacer_script = (
            "import sys, time\n"
            "data = b'\\x00' * 24000\n"
            "while True:\n"
            "    sys.stdout.buffer.write(data)\n"
            "    sys.stdout.buffer.flush()\n"
            "    time.sleep(1.0)\n"
        )
        chunks_seen = []
        with tempfile.TemporaryDirectory() as tmp:
            chunk_dir = Path(tmp)
            worker = CaptureWorker(
                capture_cmd=[sys.executable, "-c", pacer_script],
                chunk_dir=chunk_dir,
                mode="ft4",  # fastest real cycle (7.5s), keeps the test quick
                sample_rate=12000,
                chunk_ready_callback=chunks_seen.append,
            )
            worker.start()
            try:
                deadline = time.time() + 25
                while time.time() < deadline and not chunks_seen:
                    time.sleep(0.2)
            finally:
                worker.stop()

            self.assertGreater(len(chunks_seen), 0, "no chunk was written within the deadline")
            chunk_path = chunks_seen[0]
            self.assertTrue(chunk_path.exists())
            with wave.open(str(chunk_path), "rb") as wav:
                self.assertEqual(wav.getnchannels(), 1)
                self.assertEqual(wav.getsampwidth(), 2)
                self.assertEqual(wav.getframerate(), 12000)
                duration = wav.getnframes() / wav.getframerate()
                self.assertAlmostEqual(duration, 7.5, delta=0.1)


if __name__ == "__main__":
    unittest.main()
