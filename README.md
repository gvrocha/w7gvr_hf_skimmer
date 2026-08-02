# w7gvr_hf_skimmer

**Status:** planning — no working code yet. See `planning/implementation_plan.md`.

A barebones, headless Linux logger of FT8/FT4/WSPR receptions from a backyard HF station.
Same service-oriented architecture as [`mobile_aprs_gateway`](../mobile_aprs_gateway) — a daemon, a Unix-socket IPC protocol, and a CLI client — adapted from VHF/APRS to HF digital modes.

No graphical interface is planned.
This is a receive-only skimmer: it logs what it hears, it does not transmit.

> **License requirement:** Operating an amateur radio station requires a valid amateur radio license. This project is RX-only and never keys a transmitter, but the receiving station itself should still be operated by a licensed amateur.

---

## Why not just run WSJT-X?

WSJT-X's GUI already does all of this for a human operator watching a screen.
This project exists because `mobile_aprs_gateway` proved out a pattern worth repeating: a headless daemon + socket IPC + CLI, so other tools (a web dashboard, a database sync job, a Discord bot, whatever) can be built **on top of** a receive service, instead of needing to scrape a GUI or parse WSJT-X's UDP `NetworkMessage` protocol from scratch.

`jt9` and `wsprd` — the actual decode engines WSJT-X ships — are already separate OS processes under the hood (confirmed via prior reverse-engineering in [`wsjtx_hacks`](../wsjtx_hacks)). WSJT-X's GUI talks to them over shared memory for live decoding; this project instead uses their documented standalone batch mode (decode a WAV file, print results, exit) and wraps that in its own daemon.

---

## Architecture (planned)

```
rtl_sdr/rtl_fm ──► capture daemon ──► WAV chunks ──► decode daemon ──► hsd (core) ←── hsctl (CLI)
                   (UTC-aligned,                      (mode-aware:
                    GPS-disciplined)                    jt9 / wsprd / ft8_lib)
```

- **Capture daemon** — tunes the SDR to the configured band/mode's dial frequency, writes WAV chunks aligned to that mode's UTC decode-cycle boundary (15s FT8, 7.5s FT4, 120s WSPR).
- **Decode daemon** — mode-aware; invokes the configured decoder binary as a one-shot subprocess per chunk, parses decoded lines from stdout.
- **`hsd`** — core daemon, IPC server (Unix socket, line-delimited JSON — same pattern as `magd.sock`), owns config and session state.
- **`hsctl`** — CLI client: start/stop/status/monitor, mirroring `magctl`.

One band, one mode at a time — selected via config, not run concurrently. See `planning/implementation_plan.md` for the phased build order.

---

## Decoders

Two decode engines are vendored as git submodules under `vendor/`:

| Decoder | Modes | Source | Notes |
|---|---|---|---|
| `jt9` / `wsprd` | FT8, FT4, WSPR | `vendor/wsjtx` ([WSJTX/wsjtx](https://github.com/WSJTX/wsjtx.git)) | Reference implementation. `jt9` runs multi-pass subtraction decoding (up to 3 cycles × 3 passes) plus 6 modes of a priori (AP) decoding — best decode yield on a busy band. |
| `decode_ft8` | FT8, FT4 | `vendor/ft8_lib` ([kgoba/ft8_lib](https://github.com/kgoba/ft8_lib.git)) | Lightweight reimplementation designed for embedded targets (runs on an STM32F7 in <200KB RAM). No documented subtraction/AP decoding — lower expected yield on crowded bands, but a much smaller footprint. Does not support WSPR. |

Decoder choice is a config setting (`decoder`, combined with `mode`), so `jt9` and `ft8_lib` can be A/B compared on the same captured WAV files before committing to one.

Rough sensitivity thresholds (50% decode probability, 2500 Hz reference bandwidth), for context: WSPR ≈ −28 dB SNR, FT8 ≈ −20.8 dB, FT4 ≈ −17.5 dB. WSPR's narrowband/long-integration design makes it dramatically more sensitive than FT8/FT4, at the cost of a 2-minute cycle instead of 15s/7.5s.

---

## Hardware

| Component | Notes |
|---|---|
| Compute | Raspberry Pi 5 (primary target) or Pi 3 B+ — single-mode headless decoding is light enough for either; see gotchas below |
| SDR | RTL-SDR Blog V4 preferred for HF — built-in upconverter + triplexer filtering (MW/FM/DAB) beats V3's direct-sampling mode, which has no front-end filtering and suffers Nyquist folding around 14.4 MHz |
| Antenna | HF-capable (not the dual-band VHF/UHF antenna used for APRS) |
| GPS | Reused from `mobile_aprs_gateway` setup for UTC time discipline — decode windows are strictly UTC-aligned, so clock accuracy is load-bearing here, not a nice-to-have |

---

## Layout

| Path | What's there |
|---|---|
| `src/` | Daemon and CLI source: `decode_worker.py`, `hsd.py` (core daemon), `hsctl.py` (CLI), `gps_clock.py`, `capture_worker.py` all implemented. `capture_worker.py` isn't yet wired into `hsd.py`'s `start`/`stop` — WAV chunks must be dropped into `chunks/` externally until that integration and real RTL-SDR hardware validation land (Track B-Radio) |
| `tests/` | `unittest`-based tests, run via `PYTHONPATH=src python3 -m unittest discover tests -v`; some are skipped unless the external `wsjtx_hacks` sample corpus is present on the machine |
| `config/` | `config.json.example` — band/mode/decoder selection |
| `bin/` | Arch-dispatch wrapper scripts (`jt9`, `wsprd`, `decode_ft8`) — resolve `$(uname -s)-$(uname -m)` and exec the matching build under `vendor/*/build-<platform>/`; see `bin/_platform.sh` |
| `tools/` | Standalone analysis/query scripts (future) |
| `planning/` | Implementation plan and design notes |
| `hardware/` | Dated hardware bring-up session logs (GPS, SDR, etc.) — hard-won operational notes, not idealized procedures |
| `provisioning/` | Gets a Pi already running `minimal_pi`'s baseline the rest of the way to running `hsd` — entirely offline, no internet assumed. `build-bundle.sh` (dev Mac, via Colima) cross-builds `jt9`/`wsprd`/`decode_ft8` for `linux-aarch64` and fetches+indexes a self-contained `apk` package bundle (`python3`, `gpsd`, `rtl-sdr`); `deploy.sh` rsyncs it all to the Pi; `install.sh` (run on the Pi) installs offline and registers the `hsd`/`gpsd` OpenRC services. `bundle/` is gitignored, rebuilt on demand |
| `vendor/` | Pinned git submodules: `wsjtx` (real build dependency, provides `jt9`/`wsprd`), `ft8_lib` (alternative FT8/FT4 decoder), `minimal_pi` (reusable "blank SD card → headless SSH" base image, scoped to OS/first-boot only — app-specific hardware setup like GPS lives in this project's own `hardware/`, not there) |
| `logs/` | Runtime logs, gitignored, created on first run |
| `sessions/` | Session-stamped decode logs, gitignored, created on first run |

---

## Setup

```sh
git submodule update --init --recursive
```

Build `jt9`/`wsprd` from the vendored WSJT-X source, and/or `decode_ft8` from `vendor/ft8_lib`, then invoke them via the `bin/` wrapper scripts rather than the raw build paths — this keeps decoder invocation valid across hosts with different OS/arch (see `bin/_platform.sh`). `src/decode_worker.py`'s `DECODERS` table maps `(decoder, mode)` straight to the right `bin/` wrapper and CLI flags, so `config.json` only needs `decoder`/`mode`, not a separate path.

```sh
# jt9 / wsprd (macOS example — qt@5 is keg-only, needs CMAKE_PREFIX_PATH)
cmake -S vendor/wsjtx -B vendor/wsjtx/build-darwin-arm64 \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_PREFIX_PATH="$(brew --prefix qt@5)" \
  -DWSJT_SKIP_MAP65=ON -DWSJT_BUILD_UTILS=OFF -DWSJT_BUILD_TESTS=OFF \
  -DWSJT_SKIP_MANPAGES=ON -DWSJT_GENERATE_DOCS=OFF
cmake --build vendor/wsjtx/build-darwin-arm64 --target jt9 --target wsprd -j

# decode_ft8 (macOS: Apple's libc hides snprintf's declaration under
# _POSIX_C_SOURCE without _DARWIN_C_SOURCE — pass it explicitly)
make -C vendor/ft8_lib CFLAGS="-O3 -DHAVE_STPCPY -D_DARWIN_C_SOURCE -I." decode_ft8
mkdir -p vendor/ft8_lib/build-darwin-arm64
mv vendor/ft8_lib/decode_ft8 vendor/ft8_lib/build-darwin-arm64/
```

Build output lands in `vendor/*/build-<platform>/` (gitignored, per-host) — the `bin/jt9`, `bin/wsprd`, `bin/decode_ft8` wrappers dispatch to whichever platform directory matches the current host.

**Note on Pi 3 B+:** building WSJT-X from source (Qt5 + Fortran DSP code) is a much heavier job than running it — plan to cross-build on the Pi 5 or a dev machine and copy the resulting `jt9`/`wsprd` binaries over, rather than compiling on a 1GB-RAM Pi 3 B+.

---

## Known constraints

- RX only — no transmit path is planned. Transmit is where APRS-analogue projects get complicated (PTT/CAT coordination, TX timing precision, collision avoidance); skipping it is a deliberate scope decision, not an oversight.
- No GUI, no web dashboard — this is a service layer. Anything visual is a separate project built on top of `hsd`'s socket.
- One band, one mode at a time by config — not concurrent multi-mode decoding (see implementation plan for the wideband-capture idea kept as future work, not near-term scope).
- Decode windows are strictly UTC-aligned; GPS-disciplined timing is a first-class daemon concern, mirroring how `mobile_aprs_gateway` already treats GPS time as authoritative over system clock.
