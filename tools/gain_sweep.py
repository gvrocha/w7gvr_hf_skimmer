#!/usr/bin/env python3
"""gain_sweep.py -- find a usable RTL-SDR gain for one band, run ON the Pi.

For each gain in --gains (visited round-robin, --passes times, so band
conditions drifting over the run affect every gain equally) this:

  1. grabs ~2 s of raw 8-bit I/Q straight from rtl_sdr and reports how hard
     it drives the ADC: RMS (in ADC counts, full scale = 127.5) and the
     percentage of samples pinned at 0 or 255 (clipping -- anything well
     above ~0.01% means overload, and decodes will suffer).
  2. runs the real hsd capture path (build_csdr_capture_cmd + CaptureWorker,
     GPS-disciplined chunk boundaries) for --cycles FT8 cycles and decodes
     each chunk with the real decoder (decode_worker.decode_chunk).

Prints a per-visit line as it goes and a per-gain summary at the end:
decodes, unique callsign-bearing messages, median SNR, raw ADC RMS/clip%,
and audio level in dBFS.

hsd must NOT be listening (the SDR can only be opened once). Stop it
first with `python3 src/hsctl.py stop`; this tool checks and refuses
otherwise. Chunks go to var/gain_sweep/<timestamp>/ for later inspection.

Usage:
  python3 tools/gain_sweep.py --dial 21074000 \
      --gains 7.7,14.4,20.7,25.4,29.7,33.8,38.6,43.4,49.6 --cycles 2 --passes 2
"""

import argparse
import array
import json
import socket
import statistics
import subprocess
import sys
import threading
import wave
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from capture_worker import CaptureWorker, build_csdr_capture_cmd  # noqa: E402
from decode_worker import decode_chunk  # noqa: E402
from gps_clock import GpsClock, GpsdSource  # noqa: E402

HSD_SOCKET = REPO / "hsd.sock"
BIN_DIR = REPO / "bin"
RAW_SAMPLE_RATE = 250000
RAW_SKIP_BYTES = 2 * 50000  # tuner/AGC settling at stream start
RAW_KEEP_BYTES = 2 * 500000  # ~2 s of I/Q


def hsd_is_listening() -> bool:
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(str(HSD_SOCKET))
        line = s.makefile().readline()
        s.close()
        return bool(json.loads(line).get("listening"))
    except (OSError, ValueError):
        return False


def raw_adc_stats(dial_hz: int, gain: str) -> tuple:
    """(rms_counts, clip_pct) of raw rtl_sdr I/Q bytes at this gain."""
    n = RAW_SKIP_BYTES + RAW_KEEP_BYTES
    out = subprocess.run(
        ["rtl_sdr", "-f", str(dial_hz), "-s", str(RAW_SAMPLE_RATE), "-g", gain, "-n", str(n // 2), "-"],
        capture_output=True, timeout=30, check=False,
    ).stdout[RAW_SKIP_BYTES:]
    if not out:
        return float("nan"), float("nan")
    clipped = out.count(0) + out.count(255)
    acc = 0.0
    for b in out:
        d = b - 127.5
        acc += d * d
    return (acc / len(out)) ** 0.5, 100.0 * clipped / len(out)


def wav_dbfs(path: Path) -> float:
    with wave.open(str(path)) as w:
        a = array.array("h", w.readframes(w.getnframes()))
    if not a:
        return float("-inf")
    rms = (sum(x * x for x in a) / len(a)) ** 0.5
    return 20 * __import__("math").log10(max(rms, 1e-9) / 32768.0)


def capture_cycles(dial_hz: int, gain: str, cycles: int, out_dir: Path, now_fn) -> list:
    out_dir.mkdir(parents=True, exist_ok=True)
    got = []
    done = threading.Event()

    def on_chunk(path: Path) -> None:
        got.append(path)
        if len(got) >= cycles:
            done.set()

    worker = CaptureWorker(
        capture_cmd=build_csdr_capture_cmd(str(dial_hz), "12000", gain),
        chunk_dir=out_dir,
        mode="ft8",
        sample_rate=12000,
        chunk_ready_callback=on_chunk,
        now_fn=now_fn,
    )
    worker.start()
    done.wait(timeout=15 * (cycles + 2) + 10)
    worker.stop()
    return got[:cycles]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dial", type=int, default=21074000, help="dial frequency, Hz")
    ap.add_argument("--gains", default="7.7,14.4,20.7,25.4,29.7,33.8,38.6,43.4,49.6")
    ap.add_argument("--cycles", type=int, default=2, help="FT8 cycles per gain visit")
    ap.add_argument("--passes", type=int, default=2, help="round-robin passes over all gains")
    ap.add_argument("--decoder", default="ft8_lib")
    args = ap.parse_args()

    if hsd_is_listening():
        sys.exit("hsd is listening and holds the SDR -- run `python3 src/hsctl.py stop` first")

    gains = [g.strip() for g in args.gains.split(",") if g.strip()]
    try:
        clock = GpsClock(GpsdSource(), fixed_interval=10)
        clock.start()
        now_fn = clock.timestamp
    except OSError:
        print("warning: gpsd unreachable, using system clock (chunk alignment may be off)", file=sys.stderr)
        now_fn = lambda: datetime.now(timezone.utc)  # noqa: E731

    run_dir = REPO / "var" / "gain_sweep" / now_fn().strftime("%Y%m%d_%H%M%S")
    print(f"dial {args.dial} Hz, gains {gains}, {args.cycles} cycles x {args.passes} passes -> {run_dir}", flush=True)

    results = {g: {"decodes": 0, "msgs": set(), "snrs": [], "rms": [], "clip": [], "dbfs": [], "chunks": 0} for g in gains}
    for p in range(args.passes):
        for g in gains:
            rms, clip = raw_adc_stats(args.dial, g)
            chunks = capture_cycles(args.dial, g, args.cycles, run_dir / f"gain_{g}" / f"pass{p}", now_fn)
            n = 0
            r = results[g]
            for c in chunks:
                spots = decode_chunk(BIN_DIR, args.decoder, "ft8", c)
                n += len(spots)
                r["msgs"].update(s.message for s in spots)
                r["snrs"].extend(s.snr for s in spots)
                r["dbfs"].append(wav_dbfs(c))
            r["decodes"] += n
            r["chunks"] += len(chunks)
            r["rms"].append(rms)
            r["clip"].append(clip)
            print(f"pass {p} gain {g:>5}: {len(chunks)} chunks, {n:3d} decodes, "
                  f"ADC rms {rms:5.1f} clip {clip:6.3f}%", flush=True)

    print("\n gain | chunks | decodes | dec/cycle | uniq msgs | med SNR | ADC rms | clip %  | audio dBFS")
    print("------+--------+---------+-----------+-----------+---------+---------+---------+-----------")
    for g in gains:
        r = results[g]
        per = r["decodes"] / r["chunks"] if r["chunks"] else 0
        snr = statistics.median(r["snrs"]) if r["snrs"] else float("nan")
        dbfs = statistics.mean(r["dbfs"]) if r["dbfs"] else float("nan")
        print(f"{g:>5} | {r['chunks']:>6} | {r['decodes']:>7} | {per:>9.1f} | {len(r['msgs']):>9} | "
              f"{snr:>7.1f} | {statistics.mean(r['rms']):>7.1f} | {max(r['clip']):>7.3f} | {dbfs:>9.1f}")


if __name__ == "__main__":
    main()
