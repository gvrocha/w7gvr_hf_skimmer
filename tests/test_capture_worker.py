"""Unit tests for src/capture_worker.py.

align_chunks()/next_boundary()/chunk_filename() are tested against a fake
in-memory byte source with a controlled start_time -- no hardware, no
real-time waiting. CaptureWorker itself gets one integration test against
a real, paced synthetic PCM-generator subprocess standing in for rtl_fm
(runs in real time, so this one test takes up to ~15s).

Run with: PYTHONPATH=src python3 -m unittest tests/test_capture_worker.py -v
"""

import io
import os
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
    build_csdr_capture_cmd,
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

    def test_now_fn_is_used_for_chunk_boundary_instead_of_system_clock(self):
        # A fake "now" nowhere near the real system clock -- if now_fn
        # weren't actually wired in, the resulting chunk filename would
        # reflect real system time instead of this fixed fake instant.
        pacer_script = (
            "import sys, time\n"
            "data = b'\\x00' * 24000\n"
            "while True:\n"
            "    sys.stdout.buffer.write(data)\n"
            "    sys.stdout.buffer.flush()\n"
            "    time.sleep(1.0)\n"
        )
        fake_now = datetime(2030, 5, 17, 12, 0, 3, tzinfo=UTC)
        expected_boundary = next_boundary(fake_now, 7.5)

        chunks_seen = []
        with tempfile.TemporaryDirectory() as tmp:
            chunk_dir = Path(tmp)
            worker = CaptureWorker(
                capture_cmd=[sys.executable, "-c", pacer_script],
                chunk_dir=chunk_dir,
                mode="ft4",
                sample_rate=12000,
                chunk_ready_callback=chunks_seen.append,
                now_fn=lambda: fake_now,
            )
            worker.start()
            try:
                deadline = time.time() + 25
                while time.time() < deadline and not chunks_seen:
                    time.sleep(0.2)
            finally:
                worker.stop()

            self.assertGreater(len(chunks_seen), 0, "no chunk was written within the deadline")
            self.assertEqual(chunks_seen[0].name, chunk_filename(expected_boundary))


class TestBuildCsdrCaptureCmd(unittest.TestCase):
    def test_pipeline_contains_expected_stages_in_order(self):
        cmd = build_csdr_capture_cmd(
            "14074000", "12000", "42.1", iq_sample_rate=250000, bin_dir="/opt/bin"
        )
        self.assertIsInstance(cmd, str)
        stages = [s.strip() for s in cmd.split("|")]
        self.assertEqual(len(stages), 7)
        self.assertEqual(stages[0], "rtl_sdr -f 14074000 -s 250000 -g 42.1 -")
        self.assertEqual(stages[1], "/opt/bin/csdr convert -i char -o float")
        self.assertIn("fractionaldecimator -f complex -p 20.833333333333332", stages[2])
        self.assertIn("bandpass --fft --low 0 --high", stages[3])
        self.assertEqual(stages[4], "/opt/bin/csdr realpart")
        self.assertEqual(stages[5], "/opt/bin/csdr limit 1.0")
        self.assertEqual(stages[6], "/opt/bin/csdr convert -i float -o s16")

    def test_decimation_rate_matches_iq_and_audio_sample_rates(self):
        cmd = build_csdr_capture_cmd("14074000", "10000", "40", iq_sample_rate=200000)
        self.assertIn("-p 20.0", cmd)

    def test_defaults_bin_dir_to_project_bin_directory(self):
        cmd = build_csdr_capture_cmd("14074000", "12000", "42.1")
        expected_bin = str(Path(__file__).resolve().parent.parent / "bin" / "csdr")
        self.assertIn(expected_bin, cmd)


class TestCaptureWorkerShellPipeline(unittest.TestCase):
    """CaptureWorker must also support a shell-pipeline capture_cmd (str,
    not List[str]) -- build_csdr_capture_cmd()'s `rtl_sdr | csdr | ...`
    output. These tests use plain Python subprocesses standing in for the
    real pipeline stages, same spirit as TestCaptureWorkerIntegration's
    rtl_fm stand-in."""

    def test_shell_pipeline_capture_writes_chunks(self):
        pacer_script = (
            "import sys, time\n"
            "data = b'\\x00' * 24000\n"
            "while True:\n"
            "    sys.stdout.buffer.write(data)\n"
            "    sys.stdout.buffer.flush()\n"
            "    time.sleep(1.0)\n"
        )
        passthrough_script = (
            "import sys\n"
            "while True:\n"
            "    data = sys.stdin.buffer.read(4096)\n"
            "    if not data:\n"
            "        break\n"
            "    sys.stdout.buffer.write(data)\n"
            "    sys.stdout.buffer.flush()\n"
        )

        chunks_seen = []
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pacer_path = tmp_path / "pacer.py"
            passthrough_path = tmp_path / "passthrough.py"
            pacer_path.write_text(pacer_script)
            passthrough_path.write_text(passthrough_script)

            chunk_dir = tmp_path / "chunks"
            pipeline = f"{sys.executable} {pacer_path} | {sys.executable} {passthrough_path}"
            worker = CaptureWorker(
                capture_cmd=pipeline,
                chunk_dir=chunk_dir,
                mode="ft4",
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
            with wave.open(str(chunks_seen[0]), "rb") as wav:
                self.assertEqual(wav.getframerate(), 12000)
                self.assertAlmostEqual(wav.getnframes() / wav.getframerate(), 7.5, delta=0.1)

    def test_stop_kills_every_stage_of_the_pipeline_not_just_the_shell(self):
        # Each stage writes its own pid to a file, then blocks forever
        # (sleep / reading stdin that never gets EOF). If stop() only
        # terminated the shell (proc.pid) instead of the whole process
        # group, both of these would be left running as orphans.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pidfile1 = tmp_path / "pid1"
            pidfile2 = tmp_path / "pid2"
            script1 = tmp_path / "stage1.py"
            script2 = tmp_path / "stage2.py"
            script1.write_text(
                f"import os\n"
                f"open(r'{pidfile1}', 'w').write(str(os.getpid()))\n"
                f"import time\n"
                f"time.sleep(30)\n"
            )
            script2.write_text(
                f"import os, sys\n"
                f"open(r'{pidfile2}', 'w').write(str(os.getpid()))\n"
                f"sys.stdin.buffer.read()\n"
            )

            chunk_dir = tmp_path / "chunks"
            pipeline = f"{sys.executable} {script1} | {sys.executable} {script2}"
            worker = CaptureWorker(
                capture_cmd=pipeline, chunk_dir=chunk_dir, mode="ft4", sample_rate=12000
            )
            worker.start()
            try:
                deadline = time.time() + 10
                while time.time() < deadline and not (pidfile1.exists() and pidfile2.exists()):
                    time.sleep(0.1)
                self.assertTrue(pidfile1.exists() and pidfile2.exists(), "pipeline stages never started")
                pid1 = int(pidfile1.read_text())
                pid2 = int(pidfile2.read_text())

                # Both children should be alive right now.
                os.kill(pid1, 0)
                os.kill(pid2, 0)
            finally:
                worker.stop()

            time.sleep(0.3)
            for pid in (pid1, pid2):
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)


if __name__ == "__main__":
    unittest.main()
