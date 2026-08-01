"""capture_worker.py -- SDR audio capture + UTC-aligned WAV chunking for hsd.

Splits a continuous raw PCM stream into WAV files aligned to strict UTC
second boundaries, per the configured mode's cycle (FT8=15s, FT4=7.5s,
WSPR=120s) -- the one part of this system with zero margin for error, per
the project's own CLAUDE.md.

No RTL-SDR/antenna is available yet (tracked as Track B-Radio in the
implementation plan). align_chunks() -- the actual boundary/byte-counting
arithmetic -- is source-agnostic and unit-tested against a fake in-memory
byte source with a controlled start time, so it needs neither hardware nor
real-time waiting to verify. CaptureWorker (the subprocess/threading glue)
is additionally tested once against a real, paced synthetic PCM-generator
subprocess standing in for rtl_fm. build_rtl_fm_cmd()'s actual argv is
written to match the standard rtl_fm USB-demod recipe used for HF
FT8/FT4/WSPR reception, but is unverified against real hardware.
"""

import logging
import subprocess
import threading
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator, List, Optional, Tuple

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
    """Read exactly n bytes from read_fn(n), or fewer on EOF."""
    buf = bytearray()
    while len(buf) < n:
        chunk = read_fn(n - len(buf))
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
    Unverified against real hardware -- no RTL-SDR/antenna on hand yet.
    """
    return ["rtl_fm", "-f", dial_frequency, "-M", "usb", "-s", sample_rate, "-g", gain, "-"]


class CaptureWorker:
    """Reads continuous raw PCM audio from a subprocess and writes WAV
    chunks aligned to UTC boundaries for the configured mode.

    capture_cmd is the full argv for the audio-producing subprocess --
    build_rtl_fm_cmd(...)'s output in production, or any command emitting
    the same raw 16-bit mono PCM format for testing (e.g. a synthetic
    generator standing in for rtl_fm).
    """

    def __init__(
        self,
        capture_cmd: List[str],
        chunk_dir: Path,
        mode: str,
        sample_rate: int,
        chunk_ready_callback: Optional[Callable[[Path], None]] = None,
    ):
        if mode not in CYCLE_SECONDS:
            raise ValueError(f"unknown mode: {mode!r}")
        self.capture_cmd = capture_cmd
        self.chunk_dir = Path(chunk_dir)
        self.mode = mode
        self.sample_rate = sample_rate
        self.chunk_ready_callback = chunk_ready_callback
        self.cycle_seconds = CYCLE_SECONDS[mode]

        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        self.chunk_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._proc = subprocess.Popen(
                self.capture_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
        except FileNotFoundError:
            _log.error("capture command not found: %s", self.capture_cmd[0])
            raise
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
            if self._proc.stdout:
                self._proc.stdout.close()
            self._proc = None
        if self._thread:
            self._thread.join(timeout=5)

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
            self._proc.stdout.read, datetime.now(timezone.utc), self.cycle_seconds, bytes_per_second
        ):
            if self._stop_event.is_set():
                return
            self._write_chunk(chunk_start, data)
