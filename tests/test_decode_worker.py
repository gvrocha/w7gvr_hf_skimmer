"""Unit tests for src/decode_worker.py.

Run with: PYTHONPATH=src python3 -m unittest tests/test_decode_worker.py -v
"""

import shutil
import sys
import tempfile
import time
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from decode_worker import (  # noqa: E402
    DECODERS,
    DecodeWorker,
    Spot,
    chunk_date,
    decode_chunk,
    parse_decode_ft8_line,
    parse_jt9_line,
    parse_wsprd_line,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
FT8_FT4_CORPUS = Path(
    "/Users/gvrocha/dev/hamradio/wsjtx_hacks/data_gitignore/WSJT-X/save"
)
WSPR_SAMPLE = REPO_ROOT / "vendor/wsjtx/samples/WSPR/150426_0918.wav"


class TestChunkDate(unittest.TestCase):
    def test_parses_wsjtx_style_filename(self):
        self.assertEqual(chunk_date(Path("260624_024845.wav")), date(2026, 6, 24))

    def test_rejects_bad_filename(self):
        with self.assertRaises(ValueError):
            chunk_date(Path("not_a_chunk.wav"))


class TestParseJt9Line(unittest.TestCase):
    def test_real_ft8_line(self):
        line = "024845   2  0.5 1488 ~  N8PFK WF3H RR73                         "
        spot = parse_jt9_line(line, date(2026, 6, 24), "ft8")
        self.assertIsInstance(spot, Spot)
        self.assertEqual(spot.utc_timestamp, datetime(2026, 6, 24, 2, 48, 45, tzinfo=timezone.utc))
        self.assertEqual(spot.mode, "ft8")
        self.assertEqual(spot.snr, 2.0)
        self.assertEqual(spot.dt, 0.5)
        self.assertEqual(spot.freq_hz, 1488.0)
        self.assertEqual(spot.message, "N8PFK WF3H RR73")

    def test_decode_finished_line_ignored(self):
        self.assertIsNone(parse_jt9_line("<DecodeFinished>   0  28        0", date(2026, 6, 24), "ft8"))

    def test_negative_snr(self):
        line = "024845 -17  0.5 2412 ~  WL4ES KK7RPI CN87                       "
        spot = parse_jt9_line(line, date(2026, 6, 24), "ft8")
        self.assertEqual(spot.snr, -17.0)


class TestParseDecodeFt8Line(unittest.TestCase):
    def test_real_ft8_line(self):
        line = "000000 +17.0 +1.28 1300 ~  K0C NI6CH 73"
        spot = parse_decode_ft8_line(line, date(2026, 6, 24), "ft8")
        self.assertIsInstance(spot, Spot)
        self.assertEqual(spot.utc_timestamp, datetime(2026, 6, 24, 0, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(spot.snr, 17.0)
        self.assertEqual(spot.dt, 1.28)
        self.assertEqual(spot.freq_hz, 1300.0)
        self.assertEqual(spot.message, "K0C NI6CH 73")

    def test_status_lines_ignored(self):
        self.assertIsNone(
            parse_decode_ft8_line(
                "Decoded 18 messages, callsign hashtable size 32", date(2026, 6, 24), "ft8"
            )
        )
        self.assertIsNone(
            parse_decode_ft8_line("Max magnitude: -13.7 dB", date(2026, 6, 24), "ft8")
        )


class TestParseWsprdLine(unittest.TestCase):
    def test_real_wspr_line(self):
        line = "0918  -9  1.1   0.001446  0  ND6P DM04 30 "
        spot = parse_wsprd_line(line, date(2015, 4, 26))
        self.assertIsInstance(spot, Spot)
        self.assertEqual(spot.utc_timestamp, datetime(2015, 4, 26, 9, 18, 0, tzinfo=timezone.utc))
        self.assertEqual(spot.mode, "wspr")
        self.assertEqual(spot.snr, -9.0)
        self.assertEqual(spot.dt, 1.1)
        self.assertAlmostEqual(spot.freq_hz, 1446.0)
        self.assertEqual(spot.call, "ND6P")
        self.assertEqual(spot.grid, "DM04")
        self.assertEqual(spot.dbm, 30)
        self.assertEqual(spot.drift, 0)

    def test_blank_line_ignored(self):
        self.assertIsNone(parse_wsprd_line("", date(2015, 4, 26)))


@unittest.skipUnless(FT8_FT4_CORPUS.is_dir(), "wsjtx_hacks sample corpus not present on this machine")
class TestDecodeChunkAgainstRealCorpus(unittest.TestCase):
    def test_ft8_sample_decodes_with_jt9(self):
        wav = FT8_FT4_CORPUS / "260624_024845.wav"
        spots = decode_chunk(BIN_DIR, "wsjtx", "ft8", wav)
        self.assertGreater(len(spots), 0)
        self.assertTrue(all(s.mode == "ft8" for s in spots))
        self.assertTrue(all(s.utc_timestamp.date() == date(2026, 6, 24) for s in spots))

    def test_ft8_sample_decodes_with_decode_ft8(self):
        wav = FT8_FT4_CORPUS / "260624_024845.wav"
        spots = decode_chunk(BIN_DIR, "ft8_lib", "ft8", wav)
        self.assertGreater(len(spots), 0)

    def test_ft4_sample_with_signal_decodes(self):
        wav = FT8_FT4_CORPUS / "260708_014230.wav"
        spots = decode_chunk(BIN_DIR, "ft8_lib", "ft4", wav)
        self.assertGreater(len(spots), 0)
        self.assertTrue(all(s.mode == "ft4" for s in spots))

    def test_ft4_quiet_sample_decodes_to_zero(self):
        wav = FT8_FT4_CORPUS / "260708_013545.wav"
        spots = decode_chunk(BIN_DIR, "ft8_lib", "ft4", wav)
        self.assertEqual(spots, [])


@unittest.skipUnless(WSPR_SAMPLE.is_file(), "vendored WSPR sample not present")
class TestDecodeChunkWspr(unittest.TestCase):
    def test_wspr_sample_decodes_with_wsprd(self):
        spots = decode_chunk(BIN_DIR, "wsjtx", "wspr", WSPR_SAMPLE)
        self.assertGreater(len(spots), 0)
        self.assertTrue(all(s.mode == "wspr" for s in spots))
        self.assertTrue(all(s.call for s in spots))


@unittest.skipUnless(FT8_FT4_CORPUS.is_dir(), "wsjtx_hacks sample corpus not present on this machine")
class TestDecodeWorker(unittest.TestCase):
    def test_watches_directory_and_decodes_dropped_chunks(self):
        results = []
        with tempfile.TemporaryDirectory() as tmp:
            chunk_dir = Path(tmp)
            worker = DecodeWorker(
                chunk_dir=chunk_dir,
                decoder="ft8_lib",
                mode="ft4",
                results_callback=results.append,
                bin_dir=BIN_DIR,
                poll_interval=0.2,
            )
            worker.start()
            try:
                # a file that decodes to real spots, and one that legitimately
                # decodes to zero -- both should be processed without errors.
                shutil.copy(FT8_FT4_CORPUS / "260708_014230.wav", chunk_dir / "260708_014230.wav")
                shutil.copy(FT8_FT4_CORPUS / "260708_013545.wav", chunk_dir / "260708_013545.wav")
                deadline = time.time() + 10
                while time.time() < deadline and len(results) == 0:
                    time.sleep(0.2)
            finally:
                worker.stop()

        self.assertGreater(len(results), 0)
        self.assertTrue(all(isinstance(s, Spot) and s.mode == "ft4" for s in results))


if __name__ == "__main__":
    unittest.main()
