#!/usr/bin/env python3
"""reference_usb_demod.py -- independent USB demodulator for raw RTL-SDR I/Q,
built to cross-check rtl_fm's own demod output during diagnosis of an
intermittent-clipping bug (see hardware/20260920_rtl_fm_demod_bug.md).

Takes a raw 8-bit unsigned interleaved I/Q capture (rtl_sdr's native output
format) and produces the same kind of 15-second, 12000 Hz mono WAV chunks
hsd's own pipeline expects -- but via a from-scratch, correctly-scaled USB
demod (positive-sideband complex bandpass extraction + polyphase decimation)
instead of rtl_fm's.

Requires numpy + scipy, which are NOT project dependencies (the rest of this
codebase is deliberately stdlib-only for portability to the Pi -- see
gps_clock.py's docstring). This tool is diagnostic-only, meant to run on a
dev machine with a scratch venv, not deployed to the Pi:

    python3 -m venv /tmp/dsp_venv && source /tmp/dsp_venv/bin/activate
    pip install numpy scipy

Usage:
    python3 tools/reference_usb_demod.py <raw_iq.bin> <out_dir> \
        [--iq-rate 250000] [--audio-rate 12000] [--cutoff 2000] [--chunk-sec 15]

raw_iq.bin is produced with e.g.:
    rtl_sdr -f 14074000 -s 250000 -g 42.1 -n <iq_rate * duration_sec> raw_iq.bin
"""

import argparse
import os
from datetime import datetime, timedelta, timezone
from math import gcd

import numpy as np
import wave
from scipy import signal


def demodulate_usb(raw_path: str, iq_rate: int, audio_rate: int, cutoff: float) -> np.ndarray:
    """Extract USB audio from a raw 8-bit interleaved I/Q file.

    Method: build a one-sided (positive-frequency-only) complex bandpass
    filter by taking a real lowpass prototype and shifting it up by its own
    cutoff -- this turns a symmetric [-cutoff, +cutoff] passband into
    [0, 2*cutoff], which is exactly USB's sideband. Convolving with this
    filter and taking Re(...) afterward gives proper single-sideband
    demodulation, unlike a plain symmetric lowpass + Re(...) (which folds
    both sidebands together). Then polyphase-resample down to audio_rate.
    """
    raw = np.fromfile(raw_path, dtype=np.uint8).astype(np.float64)
    i_samples = raw[0::2] - 127.5
    q_samples = raw[1::2] - 127.5
    x = i_samples + 1j * q_samples

    numtaps = 1001
    h_lp = signal.firwin(numtaps, cutoff, fs=iq_rate)
    n = np.arange(numtaps)
    h_bp = h_lp * np.exp(1j * 2 * np.pi * cutoff * n / iq_rate)

    y = signal.fftconvolve(x, h_bp, mode="same")

    g = gcd(audio_rate, iq_rate)
    y_ds = signal.resample_poly(y, audio_rate // g, iq_rate // g)

    audio = 2 * np.real(y_ds)

    # Deliberate headroom: scale to ~85% of int16 full scale so this
    # demodulator can never itself clip, regardless of input level --
    # this is the exact thing rtl_fm's own internal scaling was NOT doing.
    peak = np.max(np.abs(audio))
    if peak > 0:
        audio = audio * (32767 * 0.85 / peak)
    return np.clip(np.round(audio), -32768, 32767).astype(np.int16)


def write_chunks(audio: np.ndarray, audio_rate: int, chunk_sec: int, out_dir: str, label_start: datetime) -> None:
    chunk_samples = audio_rate * chunk_sec
    n_chunks = len(audio) // chunk_samples
    os.makedirs(out_dir, exist_ok=True)
    for i in range(n_chunks):
        t = label_start + timedelta(seconds=i * chunk_sec)
        path = os.path.join(out_dir, t.strftime("%y%m%d_%H%M%S.wav"))
        seg = audio[i * chunk_samples:(i + 1) * chunk_samples]
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(audio_rate)
            w.writeframes(seg.tobytes())
        print(f"{os.path.basename(path)}  peak={np.max(np.abs(seg))}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("raw_iq_path")
    p.add_argument("out_dir")
    p.add_argument("--iq-rate", type=int, default=250000)
    p.add_argument("--audio-rate", type=int, default=12000)
    p.add_argument("--cutoff", type=float, default=2000.0, help="Hz; USB passband becomes [0, 2*cutoff]")
    p.add_argument("--chunk-sec", type=int, default=15)
    args = p.parse_args()

    audio = demodulate_usb(args.raw_iq_path, args.iq_rate, args.audio_rate, args.cutoff)
    print(f"demodulated {len(audio)/args.audio_rate:.1f}s of audio, peak={np.max(np.abs(audio))}")
    write_chunks(audio, args.audio_rate, args.chunk_sec, args.out_dir, datetime.now(timezone.utc))


if __name__ == "__main__":
    main()
