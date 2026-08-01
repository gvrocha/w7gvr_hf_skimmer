"""Decoder subprocess invocation and stdout parsing for hsd's decode pipeline.

Chunk WAV files follow WSJT-X's own "Save all" naming convention
(YYMMDD_HHMMSS.wav), which is also what every sample WAV used to develop
this module already uses. Decoder stdout only ever reports a time-of-day
(HHMMSS or HHMM), never a date, so every spot's full UTC timestamp is
assembled here from the chunk filename's date plus the decoder's reported
time-of-day -- never from decoder stdout alone.
"""

import logging
import re
import subprocess
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Optional

_log = logging.getLogger(__name__)

CHUNK_FILENAME_RE = re.compile(r"^(\d{2})(\d{2})(\d{2})_(\d{4,6})\.wav$", re.IGNORECASE)


def chunk_date(path: Path) -> date:
    """Parse the UTC date encoded in a WSJT-X-style chunk filename (YYMMDD_HHMMSS.wav)."""
    m = CHUNK_FILENAME_RE.match(path.name)
    if not m:
        raise ValueError(f"chunk filename doesn't match YYMMDD_HHMMSS.wav: {path.name}")
    yy, mm, dd, _ = m.groups()
    return date(2000 + int(yy), int(mm), int(dd))


def _combine_utc(day: date, hms: str) -> datetime:
    """Combine a chunk's UTC date with a decoder-reported HHMM or HHMMSS time-of-day."""
    if len(hms) == 6:
        hh, mm, ss = int(hms[0:2]), int(hms[2:4]), int(hms[4:6])
    elif len(hms) == 4:
        hh, mm, ss = int(hms[0:2]), int(hms[2:4]), 0
    else:
        raise ValueError(f"unexpected time-of-day field: {hms!r}")
    return datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=timezone.utc)


@dataclass
class Spot:
    utc_timestamp: datetime
    mode: str
    snr: float
    dt: float
    freq_hz: float
    message: str
    call: Optional[str] = None
    grid: Optional[str] = None
    dbm: Optional[int] = None
    drift: Optional[int] = None


_JT9_RE = re.compile(
    r"^(?P<time>\d{6})\s+(?P<snr>-?\d+)\s+(?P<dt>-?\d+\.\d+)\s+"
    r"(?P<freq>\d+)\s+(?P<flag>\S+)\s+(?P<msg>.+?)\s*$"
)

_DECODE_FT8_RE = re.compile(
    r"^(?P<time>\d{6})\s+(?P<snr>[+-]\d+\.\d+)\s+(?P<dt>[+-]\d+\.\d+)\s+"
    r"(?P<freq>\d+)\s+(?P<flag>\S+)\s+(?P<msg>.+?)\s*$"
)


def parse_jt9_line(line: str, day: date, mode: str) -> Optional[Spot]:
    """Parse one line of `jt9 -8`/`jt9 -5` stdout, e.g.:
    '024845   2  0.5 1488 ~  N8PFK WF3H RR73'
    """
    m = _JT9_RE.match(line)
    if not m:
        return None
    return Spot(
        utc_timestamp=_combine_utc(day, m["time"]),
        mode=mode,
        snr=float(m["snr"]),
        dt=float(m["dt"]),
        freq_hz=float(m["freq"]),
        message=m["msg"].strip(),
    )


def parse_decode_ft8_line(line: str, day: date, mode: str) -> Optional[Spot]:
    """Parse one line of `decode_ft8`/`decode_ft8 -ft4` stdout, e.g.:
    '000000 +17.0 +1.28 1300 ~  K0C NI6CH 73'
    """
    m = _DECODE_FT8_RE.match(line)
    if not m:
        return None
    return Spot(
        utc_timestamp=_combine_utc(day, m["time"]),
        mode=mode,
        snr=float(m["snr"]),
        dt=float(m["dt"]),
        freq_hz=float(m["freq"]),
        message=m["msg"].strip(),
    )


def parse_wsprd_line(line: str, day: date) -> Optional[Spot]:
    """Parse one line of `wsprd` stdout, e.g.:
    '0918  -9  1.1   0.001446  0  ND6P DM04 30'

    wsprd is invoked without -f (no dial frequency), so its "frequency"
    column is baseband-relative Hz -- same semantics as jt9/decode_ft8's
    freq column -- just printed as if it were an absolute frequency in MHz.
    Convert back to Hz here so freq_hz means the same thing across all
    three decoders.
    """
    parts = line.split()
    if len(parts) != 8:
        return None
    hhmm, snr, dt, freq_mhz, drift, call, grid, dbm = parts
    try:
        return Spot(
            utc_timestamp=_combine_utc(day, hhmm),
            mode="wspr",
            snr=float(snr),
            dt=float(dt),
            freq_hz=float(freq_mhz) * 1e6,
            message=f"{call} {grid} {dbm}",
            call=call,
            grid=grid,
            dbm=int(dbm),
            drift=int(drift),
        )
    except ValueError:
        return None


# (decoder, mode) -> (binary name, extra args, line parser)
DecoderKey = tuple
_LineParser = Callable[[str, date], Optional[Spot]]

DECODERS: dict = {
    ("wsjtx", "ft8"): ("jt9", ["-8"], lambda line, day: parse_jt9_line(line, day, "ft8")),
    ("wsjtx", "ft4"): ("jt9", ["-5"], lambda line, day: parse_jt9_line(line, day, "ft4")),
    ("wsjtx", "wspr"): ("wsprd", [], parse_wsprd_line),
    ("ft8_lib", "ft8"): ("decode_ft8", [], lambda line, day: parse_decode_ft8_line(line, day, "ft8")),
    ("ft8_lib", "ft4"): ("decode_ft8", ["-ft4"], lambda line, day: parse_decode_ft8_line(line, day, "ft4")),
}


DEFAULT_WORK_DIR = Path(__file__).resolve().parent.parent / "var" / "decoder-cache"


def decode_chunk(
    bin_dir: Path, decoder: str, mode: str, wav_path: Path, work_dir: Optional[Path] = None
) -> list[Spot]:
    """Invoke the configured decoder on one WAV chunk and return parsed spots.

    jt9/wsprd/decode_ft8 all drop working files into their current directory
    (FFTW wisdom cache, hashtable, ALL_WSPR.TXT, etc.) -- run them from a
    dedicated, gitignored work_dir rather than inheriting the caller's cwd,
    so these side files don't scatter across the repo root.
    """
    key = (decoder, mode)
    if key not in DECODERS:
        raise ValueError(f"no decoder mapping for decoder={decoder!r} mode={mode!r}")
    binary, extra_args, parser = DECODERS[key]
    day = chunk_date(wav_path)

    work_dir = Path(work_dir) if work_dir else DEFAULT_WORK_DIR
    work_dir.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        [str(bin_dir / binary), *extra_args, str(wav_path)],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(work_dir),
    )

    spots = []
    for line in result.stdout.splitlines():
        spot = parser(line, day)
        if spot is not None:
            spots.append(spot)
    return spots


class DecodeWorker:
    """Watches a directory for new WAV chunks and decodes each one.

    Deliberately narrow interface: the constructor only takes a directory to
    watch, which decoder/mode to run, and a callback to push parsed Spots
    to. Nothing about hsd's socket, config file, or session state leaks in
    here -- so this could be lifted into a separate process later (swap
    chunk_dir for a shared/networked directory, swap results_callback for a
    socket push) without touching the decode/parse logic at all.
    """

    def __init__(
        self,
        chunk_dir: Path,
        decoder: str,
        mode: str,
        results_callback: Callable[[Spot], None],
        bin_dir: Optional[Path] = None,
        work_dir: Optional[Path] = None,
        poll_interval: float = 1.0,
    ):
        self.chunk_dir = Path(chunk_dir)
        self.decoder = decoder
        self.mode = mode
        self.results_callback = results_callback
        self.bin_dir = Path(bin_dir) if bin_dir else Path(__file__).resolve().parent.parent / "bin"
        self.work_dir = Path(work_dir) if work_dir else DEFAULT_WORK_DIR
        self.poll_interval = poll_interval
        self._last_processed: Optional[str] = None
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        self.chunk_dir.mkdir(parents=True, exist_ok=True)
        while not self._stop_event.is_set():
            candidates = sorted(
                p
                for p in self.chunk_dir.glob("*.wav")
                if self._last_processed is None or p.name > self._last_processed
            )
            for wav_path in candidates:
                try:
                    spots = decode_chunk(
                        self.bin_dir, self.decoder, self.mode, wav_path, work_dir=self.work_dir
                    )
                except Exception:
                    _log.exception("decode failed for %s", wav_path)
                    spots = []
                for spot in spots:
                    self.results_callback(spot)
                self._last_processed = wav_path.name
            self._stop_event.wait(self.poll_interval)
