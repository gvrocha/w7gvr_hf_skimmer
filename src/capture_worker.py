"""capture_worker.py -- SDR audio capture + UTC-aligned WAV chunking for hsd.

Splits a continuous raw PCM stream into WAV files aligned to strict UTC
second boundaries, per the configured mode's cycle (FT8=15s, FT4=7.5s,
WSPR=120s) -- the one part of this system with zero margin for error, per
the project's own CLAUDE.md.

align_chunks() -- the actual boundary/byte-counting arithmetic -- is
source-agnostic and unit-tested against a fake in-memory byte source with
a controlled start time, so it needs neither hardware nor real-time
waiting to verify. CaptureWorker (the subprocess/threading glue) is
additionally tested against a real, paced synthetic PCM-generator
subprocess, for both a plain argv command and a shell pipeline.

build_rtl_fm_cmd() was hardware-validated (real RTL-SDR Blog V4, real 20m
FT8 traffic decoded), but rtl_fm itself has a real, reproducible bug: it
intermittently hard-clips its own demodulated audio output even when the
underlying raw I/Q is completely clean and stable -- see
hardware/20260920_rtl_fm_demod_bug.md for the full investigation.
build_csdr_capture_cmd() replaces it with an `rtl_sdr | csdr` pipeline that
does the USB extraction itself (csdr's documented "modified Weaver
demodulator": a one-sided complex bandpass filter + realpart), validated
against a real 120-second raw I/Q capture -- 133-152 real FT8 messages
decoded cleanly across every chunk, vs. rtl_fm's mostly-empty output on
the same signal. This is now the default capture command hsd.py uses;
build_rtl_fm_cmd() is kept for reference/fallback, not deleted.
"""

import logging
import os
import signal
import subprocess
import threading
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator, List, Optional, Tuple, Union

_log = logging.getLogger(__name__)

# seconds per WAV chunk cycle, by mode
CYCLE_SECONDS = {
    "ft8": 15.0,
    "ft4": 7.5,
    "wspr": 120.0,
}

SAMPLE_WIDTH_BYTES = 2  # 16-bit PCM, matches jt9/wsprd/decode_ft8's expected input


def next_boundary(after: datetime, cycle_seconds: float) -> datetime:
    """The next UTC instant that's an exact multiple of cycle_seconds past
    the top of the hour, strictly after `after`. Correct for 15/7.5/120 --
    all divide 3600 evenly, so a boundary never has to cross an hour in a
    way that needs extra handling."""
    epoch_hour = after.replace(minute=0, second=0, microsecond=0)
    elapsed = (after - epoch_hour).total_seconds()
    n = int(elapsed // cycle_seconds) + 1
    return epoch_hour + timedelta(seconds=n * cycle_seconds)


def chunk_filename(start: datetime) -> str:
    """WSJT-X-style YYMMDD_HHMMSS.wav filename for a chunk starting at `start`."""
    return start.strftime("%y%m%d_%H%M%S.wav")


def _read_exact(read_fn: Callable[[int], bytes], n: int) -> bytes:
    """Read exactly n bytes from read_fn(n), or fewer on EOF.

    Treats the stream being closed out from under us (ValueError/OSError)
    the same as EOF -- CaptureWorker.stop() closes stdout from the main
    thread while this may be mid-read on _run()'s own thread, and Python
    can raise "I/O operation on closed file" instead of just returning
    b"" in that race, especially with a multi-stage shell pipeline where
    reads are more likely to be blocked at the exact moment stop() runs.
    """
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = read_fn(n - len(buf))
        except (ValueError, OSError):
            break
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


def align_chunks(
    read_fn: Callable[[int], bytes],
    start_time: datetime,
    cycle_seconds: float,
    bytes_per_second: int,
) -> Iterator[Tuple[datetime, bytes]]:
    """Yield (chunk_start_utc, pcm_bytes) pairs aligned to UTC boundaries of
    cycle_seconds, reading from read_fn(n) -> up to n bytes (b"" on EOF).

    The first partial period between start_time and the next boundary is
    discarded, so every yielded chunk is a full, boundary-aligned cycle --
    this is the only alignment step needed; everything after it is exact
    byte-counting against the nominal sample rate.
    """
    chunk_bytes = int(round(cycle_seconds * bytes_per_second))
    boundary = next_boundary(start_time, cycle_seconds)
    warmup_bytes = max(0, int(round((boundary - start_time).total_seconds() * bytes_per_second)))
    if warmup_bytes:
        _read_exact(read_fn, warmup_bytes)

    chunk_start = boundary
    while True:
        data = _read_exact(read_fn, chunk_bytes)
        if len(data) < chunk_bytes:
            return
        yield chunk_start, data
        chunk_start = chunk_start + timedelta(seconds=cycle_seconds)


def build_rtl_fm_cmd(dial_frequency: str, sample_rate: str, gain: str) -> List[str]:
    """Standard rtl_fm USB-demod recipe for HF FT8/FT4/WSPR reception.
    Hardware-validated (real RTL-SDR Blog V4, real 20m FT8 traffic decoded)
    -- but rtl_fm itself has a real, reproducible demod-stage clipping bug
    (see module docstring and hardware/20260920_rtl_fm_demod_bug.md).
    Kept for reference/fallback; build_csdr_capture_cmd() is what hsd.py
    actually uses now.
    """
    return ["rtl_fm", "-f", dial_frequency, "-M", "usb", "-s", sample_rate, "-g", gain, "-"]


DEFAULT_BIN_DIR = Path(__file__).resolve().parent.parent / "bin"

# USB passband as a fraction of the final audio sample rate, and the
# bandpass filter's transition width (also a fraction) -- matches the
# reference demod validated in hardware/20260920_rtl_fm_demod_bug.md: a
# 0-4000 Hz passband at 12000 Hz audio is 4000/12000 = 1/3.
_USB_PASSBAND_FRACTION = 1 / 3
_USB_TRANSITION_FRACTION = 0.02


def build_csdr_capture_cmd(
    dial_frequency: str,
    sample_rate: str,
    gain: str,
    iq_sample_rate: int = 250000,
    bin_dir: Optional[Path] = None,
) -> str:
    """rtl_sdr | csdr pipeline that replaces rtl_fm's own (buggy) USB demod
    with csdr's documented "modified Weaver demodulator" recipe: a one-
    sided complex bandpass filter (extracts the upper sideband directly,
    unlike a plain symmetric lowpass + realpart, which would fold both
    sidebands together) followed by realpart. Validated against a real
    120-second raw I/Q capture -- 133 real FT8 messages decoded cleanly
    across all 7 chunks, zero clipping, vs. rtl_fm's mostly-empty output
    on the same signal. Full investigation: hardware/20260920_rtl_fm_demod_bug.md.

    Returns a shell pipeline string, not an argv list -- CaptureWorker
    detects this (via isinstance) and runs it with shell=True in its own
    process group, so stop() can tear down every stage together instead
    of leaving rtl_sdr/csdr orphaned.

    The tuner is centered exactly at dial_frequency (rtl_sdr -f), so no
    frequency shift is needed before the bandpass step -- USB content
    already lives at positive baseband frequencies.
    """
    bin_dir = Path(bin_dir) if bin_dir else DEFAULT_BIN_DIR
    csdr = str(bin_dir / "csdr")
    audio_rate = int(sample_rate)
    decimation_rate = iq_sample_rate / audio_rate

    return (
        f"rtl_sdr -f {dial_frequency} -s {iq_sample_rate} -g {gain} - | "
        f"{csdr} convert -i char -o float | "
        f"{csdr} fractionaldecimator -f complex -p {decimation_rate} | "
        f"{csdr} bandpass --fft --low 0 --high {_USB_PASSBAND_FRACTION} {_USB_TRANSITION_FRACTION} | "
        f"{csdr} realpart | "
        f"{csdr} limit 1.0 | "
        f"{csdr} convert -i float -o s16"
    )


class CaptureWorker:
    """Reads continuous raw PCM audio from a subprocess and writes WAV
    chunks aligned to UTC boundaries for the configured mode.

    capture_cmd is either:
      - a List[str] argv, run directly (no shell) -- build_rtl_fm_cmd()'s
        output, or a synthetic generator for testing; or
      - a str shell pipeline, run with shell=True in its own process group
        -- build_csdr_capture_cmd()'s output (an `rtl_sdr | csdr | ...`
        pipeline). stop() detects this and kills the whole process group,
        not just the shell, so no pipeline stage is left orphaned.

    now_fn supplies the current UTC time used to compute the first chunk
    boundary -- defaults to the system clock, but hsd.py passes in
    GpsClock.timestamp so chunk filenames/boundaries are GPS-disciplined
    rather than trusting a Pi's RTC-less, possibly-wrong system clock.
    """

    def __init__(
        self,
        capture_cmd: Union[List[str], str],
        chunk_dir: Path,
        mode: str,
        sample_rate: int,
        chunk_ready_callback: Optional[Callable[[Path], None]] = None,
        now_fn: Optional[Callable[[], datetime]] = None,
    ):
        if mode not in CYCLE_SECONDS:
            raise ValueError(f"unknown mode: {mode!r}")
        self.capture_cmd = capture_cmd
        self.chunk_dir = Path(chunk_dir)
        self.mode = mode
        self.sample_rate = sample_rate
        self.chunk_ready_callback = chunk_ready_callback
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.cycle_seconds = CYCLE_SECONDS[mode]
        self._is_shell_pipeline = isinstance(capture_cmd, str)

        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        self.chunk_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._proc = subprocess.Popen(
                self.capture_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                shell=self._is_shell_pipeline,
                start_new_session=self._is_shell_pipeline,
            )
        except FileNotFoundError:
            cmd_desc = self.capture_cmd if self._is_shell_pipeline else self.capture_cmd[0]
            _log.error("capture command not found: %s", cmd_desc)
            raise
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._proc:
            self._terminate(self._proc, signal.SIGTERM)
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._terminate(self._proc, signal.SIGKILL)
                self._proc.wait()
            if self._proc.stdout:
                self._proc.stdout.close()
            self._proc = None
        if self._thread:
            self._thread.join(timeout=5)

    def _terminate(self, proc: subprocess.Popen, sig: int) -> None:
        """Signal the capture process. For a shell pipeline, sig'ing just
        the shell (proc.pid) leaves its rtl_sdr/csdr children running --
        the whole pipeline was put in its own process group at start()
        (start_new_session=True), so kill that group instead."""
        if self._is_shell_pipeline:
            try:
                os.killpg(os.getpgid(proc.pid), sig)
            except ProcessLookupError:
                pass
        elif sig == signal.SIGKILL:
            proc.kill()
        else:
            proc.terminate()

    def _write_chunk(self, start: datetime, pcm_data: bytes) -> None:
        path = self.chunk_dir / chunk_filename(start)
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(SAMPLE_WIDTH_BYTES)
            wav.setframerate(self.sample_rate)
            wav.writeframes(pcm_data)
        _log.info("wrote chunk %s (%d bytes)", path.name, len(pcm_data))
        if self.chunk_ready_callback:
            self.chunk_ready_callback(path)

    def _run(self) -> None:
        bytes_per_second = self.sample_rate * SAMPLE_WIDTH_BYTES
        for chunk_start, data in align_chunks(
            self._proc.stdout.read, self.now_fn(), self.cycle_seconds, bytes_per_second
        ):
            if self._stop_event.is_set():
                return
            self._write_chunk(chunk_start, data)
