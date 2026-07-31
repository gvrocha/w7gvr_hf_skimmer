# Implementation plan

Drafted 2026-07-31 from a design conversation covering: decoder options (`jt9`/`wsprd` vs `ft8_lib`), sensitivity/specificity tradeoffs between modes and between decoder implementations, RTL-SDR HF bandwidth/dynamic-range constraints, and target hardware (Pi 5 vs Pi 3 B+).

## Goals

- Headless, RX-only logger of FT8/FT4/WSPR receptions from a fixed backyard HF station.
- Same service architecture as `mobile_aprs_gateway`: a core daemon, Unix-socket IPC, a CLI client — a layer other tools can be built on top of.
- One band, one mode at a time, selected by config.

## Non-goals (explicit, not just unscoped)

- No transmit path.
- No GUI, no web dashboard.
- No concurrent multi-mode/multi-band decoding in the initial build.

## Why RX-only is tractable where mobile_aprs_gateway's TX would not have been

No PTT/CAT coordination, no TX audio timing, no collision avoidance, no "did the rig actually key" failure mode. What remains is squarely solvable: UTC-aligned audio capture (decode windows are strictly clock-bound: FT8 mod 15s, FT4 mod 7.5s, WSPR mod 120s) and subprocess orchestration around existing, proven decoder binaries. The GPS-disciplined clock already built for `mobile_aprs_gateway` is the key enabling asset here.

---

## Phases

### Phase 0 — scaffold (this commit)
Repo structure, README, CLAUDE.md, this plan, `config.json.example`, vendored decoder submodules (`wsjtx`, `ft8_lib`). No functional code.

### Phase 1 — capture daemon only
SDR → UTC-aligned WAV chunks on disk. No decoding yet.
**Exit criterion:** chunk file timestamps verifiably align to the correct UTC second boundary against the GPS-disciplined clock, for a sustained run (hours, not seconds) — this is the one part of the system with zero margin for error, so it gets proven in isolation before anything depends on it.

### Phase 2 — WSPR decode path
Wire the decode daemon to `wsprd` (`vendor/wsjtx`). WSPR's 2-minute cadence gives the most slack for debugging file handoff and output parsing before working under FT8's tighter 15s clock.
**Exit criterion:** decoded spots (call, grid, SNR, frequency) logged to a session-stamped TSV, matching the `mobile_aprs_gateway` TSV convention (`data_collected/*_NNN_*.tsv` naming, GPS/UTC timestamps).

### Phase 3 — FT8 decode path
Add `jt9 -8` as a second mode option. Chosen over `ft8_lib` to start with, since `jt9`'s multi-pass subtraction + AP decoding gives better yield on a busy band — start with the decoder more likely to work well, not the leaner one.
**Exit criterion:** decode yield sanity-checked against a live band session (rough signal count matches expectation for time of day/band conditions).

### Phase 4 — FT4 decode path
Same binary (`jt9 -4`), different cadence (7.5s). Should be a small delta on top of Phase 3's plumbing.

### Phase 5 — IPC + CLI
Unix socket (`hsd.sock`), line-delimited JSON protocol (reuse `magd.sock`'s shape, don't redesign it). `hsctl start/stop/status/monitor`, mirroring `magctl`'s command surface. This is the layer that makes the "services other applications can be built on" goal real.

### Phase 6 — backlog / future work
- A/B `ft8_lib`'s `decode_ft8` against `jt9` on identical captured WAVs — quantify the yield gap rather than trusting README claims.
- Revisit wideband capture (one IQ stream spanning a band's full FT8+FT4+WSPR sub-band spread, channelized in software into per-mode decode windows) if single-mode-at-a-time proves limiting. Bandwidth is not the blocker (RTL-SDR handles the ~20–50 kHz spread trivially at its normal sample rates) — added software complexity (per-mode digital downconversion) is the real cost, which is why this is deferred rather than built first.
- Evaluate whether Pi 3 B+ is the actual deploy target or just a spare-hardware option; if so, plan the cross-build workflow for `jt9`/`wsprd` concretely (build on Pi 5/dev Mac, `scp` binaries over — same deploy pattern `mobile_aprs_gateway` already uses for file pushes).

---

## Config schema (draft)

See `config/config.json.example`. Static vs runtime-mutable key split should follow `mobile_aprs_gateway`'s convention once `hsd` exists (`_save_config()` only persists runtime-mutable keys; static keys like `band`/`mode`/`dial_frequency` require a daemon restart to change).

Common HF dial frequencies (USB, per WSJT-X band-frequency conventions) to reference when building the band→frequency table:

| Band | FT8 | FT4 | WSPR |
|---|---|---|---|
| 40m | 7.074 MHz | 7.047.5 MHz | 7.0386 MHz |
| 20m | 14.074 MHz | 14.080 MHz | 14.0956 MHz |
| 17m | 18.100 MHz | 18.104 MHz | 18.1046 MHz |
| 15m | 21.074 MHz | 21.140 MHz | 21.0946 MHz |

(Verify against current WSJT-X frequency list before hardcoding — band plans and conventional dial frequencies do shift.)

## Hardware notes carried into this plan

- RTL-SDR **V4 preferred over V3 for HF**: built-in upconverter + triplexer filtering vs V3's unfiltered direct-sampling mode (Nyquist folding risk around 14.4 MHz).
- Pi 3 B+ is plausible as a runtime target for this specific design (single mode, headless, no GUI) per real-world reports of WSPR/FT8 decoding at low CPU load on Pi 3-class hardware — the caveat is *building* WSJT-X from source on 1GB RAM, not running it. Cross-build, don't compile on-device.
