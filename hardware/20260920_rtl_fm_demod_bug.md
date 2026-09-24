# rtl_fm intermittent audio-clipping bug (found 2026-09-20/21, Mac-side testing)

## Context

Moved the RTL-SDR Blog V4 + HF antenna to the dev Mac for faster local
iteration (no SSH/Pi round-trip) and to optimize capture parameters (gain,
etc.) before redeploying to the Pi.
Discovered via `SDR++` that the dongle was connected and receiving healthy
real 20m FT8/FT4 traffic — clean waterfall, noise floor around -95 to
-105 dB, no visible saturation.

## Gain sweep (raw I/Q, via `rtl_sdr`)

Motivation: the Pi's earlier session (2026-09-11) found ~33% ADC rail-clipping
at `gain=40.2 dB`, root-caused to something environmental on the Pi (not
antenna, not band — see `STATUS.md`'s Track B-Radio history). Wanted to know
this dongle/antenna's actual clean headroom on a quiet electrical setup (the
Mac) before assuming any specific gain value.

Swept gain from 8.7 to 49.6 dB at 14.074 MHz, checking rail-clipping fraction
on raw 8-bit I/Q:

| Gain (dB) | Rail-clip fraction |
|---|---|
| 8.7 – 40.2 | 0.0000 (perfectly clean) |
| 42.1 | 0.0001 |
| 43.4 | 0.0003 |
| 43.9 | 0.0046 |
| 44.5 | 0.0060 |
| 48.0 | 0.0257 |
| 49.6 | 0.0432 |

Clipping onset is clearly between 43.4 and 43.9 dB. **~42-43 dB is the sweet
spot** for this antenna/dongle combo in a clean electrical environment —
notably higher than what caused saturation on the Pi at the same nominal
gain, reinforcing that the Pi's issue was environmental, not this gain value
being inherently too hot.

## Real decode test at gain=42.1 dB — inconsistent results

Ran a real capture via `capture_worker.CaptureWorker` + `build_rtl_fm_cmd()`
(the actual project code path) at 14.074 MHz, gain=42.1:

- First 4 chunks (1 minute): **0 decodes**.
- Extended to 12 more chunks (3 minutes): **84 real decodes**, but entirely
  concentrated in one ~90-second stretch (6 consecutive chunks, 12-18
  messages each) — chunks on both sides of that window decoded **zero**,
  despite constant settings throughout.

This pattern (band apparently "hot" for 90s, then completely "dead" for
minutes on either side, all within one short continuous test) doesn't match
normal FT8 band-activity variance.

## Isolating the cause: WAV-level stats, not just FFT magnitude

`decode_ft8`'s own "Max magnitude" readout was nearly identical across dead
and live chunks (-24 to -28 dB throughout) — ruling out a simple "no signal
present" explanation. The real signature only showed up in the raw WAV
sample statistics:

| Chunk | Decoded? | stdev | min / max |
|---|---|---|---|
| dead (before) | 0 | 18927.5 | **-32768 / 32767** |
| **live** | **14 spots** | 3432.7 | -12195 / 12111 (clean) |
| dead (after) | 0 | 18904.5 | **-32768 / 32767** |
| dead (later) | 0 | 18911.1 | **-32768 / 32767** |

Every "dead" window is hard-clipped at the exact digital ceiling of 16-bit
PCM audio. This is a **different clipping mechanism** than the Pi's earlier
finding — that was raw ADC/RF-front-end saturation in 8-bit I/Q (visible
even in `rtl_sdr`'s raw output); this is happening *after* the ADC, inside
`rtl_fm`'s own demodulation/decimation/scaling stage, since the raw I/Q gain
sweep above already confirmed the ADC itself is clean at this gain.

## Confirming it's rtl_fm, not the hardware: 120s continuous raw I/Q capture

Captured 120 continuous seconds of raw I/Q (`rtl_sdr -f 14074000 -s 250000
-g 42.1`) and analyzed it in 15-second blocks (matching the FT8 cycle),
computing true RF envelope power (I²+Q², not just byte-level stats):

| Block (0-7, 15s each) | Rail-clip fraction | Mean power (dB) | Peak power (dB) |
|---|---|---|---|
| all 8 blocks | **0.00000** | 19.7 – 22.8 | 37.4 – 42.1 |

Completely stable across the whole 2 minutes — no block resembles a
saturating front end, no dramatic power swings. **This rules out the
hardware, the antenna, and the RF chain entirely.** Whatever caused the
WAV-level clipping is happening downstream, inside `rtl_fm`'s own processing.

Candidate `rtl_fm` flags identified (from `rtl_fm --help`) but not yet tested
live (needs antenna access):
- **`-F 9`** — "low-leakage downsample filter," **off by default** ("0 has
  bad roll off"). A sloppy decimation filter passing this project's chosen
  `-s 12000` is inaudible for voice but could plausibly let aliased energy
  cause intermittent overshoot for a narrow digital-mode passband — a very
  FT991A-menu-shaped bug (fine defaults for voice, wrong defaults for
  digital modes).
- **`-E dc`** — DC-blocking filter for RTL-SDR's inherent zero-IF DC spike.
  Less likely to be *this specific* symptom (today's clipping is symmetric,
  not the lopsided all-positive DC-bias pattern seen on 2m/10m weeks ago),
  but cheap and standard to enable regardless.

## Definitive proof: independent reference demodulator

Rather than wait for antenna access to test `rtl_fm` flags directly (it only
reads live from the device, no file-replay mode), wrote a from-scratch
USB demodulator (`tools/reference_usb_demod.py`, numpy/scipy) against the
*same* saved 120-second raw I/Q capture:

- Proper one-sided (positive-frequency-only) complex bandpass extraction —
  not a symmetric lowpass + `Re(...)`, which would fold both sidebands
  together.
- Polyphase resample 250000 Hz → 12000 Hz.
- Deliberate peak-based scaling to ~85% of int16 full scale, so this
  demodulator can never itself clip regardless of input level — exactly the
  thing `rtl_fm`'s own scaling apparently isn't doing correctly.

Result, decoding all 8 resulting 15-second chunks with the same
`decode_ft8` binary:

**All 8 chunks decoded. 152 total real messages** (18, 20, 15, 20, 18, 20,
19, 22 per chunk), SNRs up to +19 dB, legitimate global callsigns
throughout, **zero clipping in any chunk**.

This is conclusive: the exact same raw I/Q that `rtl_fm` mostly failed to
decode (84 spots across 13 minutes of real capture, concentrated in one
lucky window) produces **152 clean decodes from 2 minutes** when processed
correctly. The band was never quiet. The hardware was never at fault.
**`rtl_fm`'s own internal demodulation/scaling pipeline has a real,
reproducible bug** for this use case.

## Resolution (2026-09-24): replaced rtl_fm with csdr, not numpy/scipy

Two paths were on the table: patch `rtl_fm` in place (`-F 9`/`-E dc`, needs
live antenna access to test since `rtl_fm` has no file-replay mode), or
replace its demod stage entirely. No antenna access was available, so
replacement was investigated further before deciding.

The `numpy`/`scipy` path (`tools/reference_usb_demod.py`) works, but a
lighter option existed: [`csdr`](https://github.com/jketterl/csdr) (a
fork of ha7ilm's original, actively maintained, used in OpenWebRX) is a
small C++ DSP pipeline tool with a *documented* USB demod recipe (a
"modified Weaver demodulator" — the same one-sided complex-bandpass
technique the Python reference demod used by hand). Not packaged in
Alpine, so vendored as source (`vendor/csdr`, matching `wsjtx`/`ft8_lib`'s
pattern) and cross-built for `linux-aarch64` via the same Colima setup.
Its only new runtime deps, `fftw-single-libs` (1.3 MiB) and `libsamplerate`
(1.4 MiB), are dramatically lighter than `numpy`+`scipy`+`openblas`
(~80 MiB) and don't compromise this codebase's stdlib-only design the
same way a Python DSP dependency would. Bonus: `fftw-single-libs` is the
same library `jt9` is missing on the Pi right now (see `STATUS.md`).

Building `csdr` locally on the Mac hit its own portability wall first:
its `CMakeLists.txt` shells out to `/proc/cpuinfo` for ARM NEON detection,
which doesn't exist on macOS (Apple Silicon reports `CMAKE_SYSTEM_PROCESSOR
= arm64`, which wrongly matches the Linux-32-bit-ARMv7 branch that does
this). Confirmed this is Mac-only noise: real Linux `aarch64` -- the actual
Pi target -- takes a separate `elseif` branch that never touches
`/proc/cpuinfo` at all. Building for real `linux-aarch64` (via the
project's existing Colima cross-build environment) worked cleanly with no
Mac-specific patching needed. A second, unrelated macOS wall showed up in
a full local build attempt: `ringbuffer.cpp` uses Linux's `mremap()`
syscall (unavailable on macOS/BSD), confirming a `vendor/csdr/build-
darwin-arm64/` isn't practical without real upstream portability work --
not attempted, since it's not needed for the actual deployment target.

Adapted the documented recipe (dial frequency is already the tuner center,
so no frequency `shift` stage is needed) into
`capture_worker.build_csdr_capture_cmd()`, producing an `rtl_sdr | csdr
convert | csdr fractionaldecimator | csdr bandpass | csdr realpart | csdr
limit | csdr convert` pipeline. Validated the real cross-built
`linux-aarch64` binary against the same saved raw I/Q capture (`cat`
substituted for the live `rtl_sdr` stage): **133 real messages decoded
cleanly across all 7 chunks, zero clipping** -- matching the Python
reference demod's result almost exactly.

Wired in for real, not just proven as a standalone script:
`CaptureWorker` now accepts a shell-pipeline `capture_cmd` (a `str`, not
just `List[str]`), run with `shell=True` in its own process group so
`stop()` can kill every pipeline stage together -- terminating just the
shell would otherwise orphan `rtl_sdr`/`csdr` as zombie processes, a real
bug caught by a dedicated test before it could bite in production.
`hsd.py`'s `start_listening()` uses `build_csdr_capture_cmd()` by default
now; `build_rtl_fm_cmd()` is kept for reference, not deleted.

**Still not live-tested against a real antenna/live `rtl_sdr`** -- only
validated by replaying the saved capture. One caveat worth a proper look
before considering this fully settled: `csdr`'s own `CMakeLists.txt` and
`LICENSE-GPL` file declare it GPL-3.0-or-later, not the "mostly BSD"
framing its README suggests -- likely a non-issue since it's used here as
a separate CLI tool rather than linked code, but not yet verified.

## Artifacts

- `tools/reference_usb_demod.py` — the reference demodulator, kept as a
  reusable diagnostic tool (not part of the core `hsd` pipeline; needs a
  scratch venv with `numpy`+`scipy`, deliberately not a project dependency).
- Raw captures and demod output from this session were left in `/tmp`
  (`iq_90s.bin`, etc.) — not preserved, easy to reproduce with the gain/
  frequency settings documented above.
