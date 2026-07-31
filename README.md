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

Decoder choice is a config setting (`decoder` / `decoder_path`), so `jt9` and `ft8_lib` can be A/B compared on the same captured WAV files before committing to one.

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
| `src/` | Daemon and CLI source (capture daemon, decode daemon, `hsd`, `hsctl`) — not yet implemented |
| `config/` | `config.json.example` — band/mode/decoder selection |
| `tools/` | Standalone analysis/query scripts (future) |
| `planning/` | Implementation plan and design notes |
| `vendor/` | Pinned git submodules: `wsjtx` (real build dependency, provides `jt9`/`wsprd`) and `ft8_lib` (alternative FT8/FT4 decoder) |
| `logs/` | Runtime logs, gitignored, created on first run |
| `sessions/` | Session-stamped decode logs, gitignored, created on first run |

---

## Setup

```sh
git submodule update --init --recursive
```

Build `jt9`/`wsprd` from the vendored WSJT-X source, and/or `decode_ft8` from `vendor/ft8_lib`, per their respective build instructions.

**Note on Pi 3 B+:** building WSJT-X from source (Qt5 + Fortran DSP code) is a much heavier job than running it — plan to cross-build on the Pi 5 or a dev machine and copy the resulting `jt9`/`wsprd` binaries over, rather than compiling on a 1GB-RAM Pi 3 B+.

---

## Known constraints

- RX only — no transmit path is planned. Transmit is where APRS-analogue projects get complicated (PTT/CAT coordination, TX timing precision, collision avoidance); skipping it is a deliberate scope decision, not an oversight.
- No GUI, no web dashboard — this is a service layer. Anything visual is a separate project built on top of `hsd`'s socket.
- One band, one mode at a time by config — not concurrent multi-mode decoding (see implementation plan for the wideband-capture idea kept as future work, not near-term scope).
- Decode windows are strictly UTC-aligned; GPS-disciplined timing is a first-class daemon concern, mirroring how `mobile_aprs_gateway` already treats GPS time as authoritative over system clock.
