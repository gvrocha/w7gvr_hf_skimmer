# w7gvr_hf_skimmer — CLAUDE.md

Headless FT8/FT4/WSPR receive-only logger for a backyard HF station.
Operator: Guilherme Rocha — W7GVR (gvrocha@gmail.com)

Sibling project to [`mobile_aprs_gateway`](../mobile_aprs_gateway/CLAUDE.md) (VHF/APRS) — same architectural pattern, different band and modes. Read that project's CLAUDE.md for the pattern this one follows.

---

## Status

Planning stage. No daemon code written yet. See `planning/implementation_plan.md` for the phased build order — start there before writing code.

---

## Non-negotiable scope boundaries

- **RX only.** No transmit path, ever, in this project. Transmit is deliberately out of scope — it's where the APRS-analogue project got hard (PTT/CAT, TX timing, collision avoidance), and this project exists specifically to avoid that complexity.
- **No GUI, no web dashboard.** This is a service layer only, explicitly requested by the operator. Anything visual is a separate, later project built against `hsd`'s socket — do not add one here even if it seems convenient.
- **One band, one mode at a time**, selected via config. Not concurrent multi-mode/multi-band decoding. A wideband-capture-plus-channelizer design was discussed and explicitly deferred — see implementation plan's "future work" section before considering it as anything other than backlog.

---

## Architecture

```
rtl_sdr/rtl_fm ──► capture daemon ──► WAV chunks ──► decode daemon ──► hsd (core) ←── hsctl (CLI)
```

- Capture daemon writes UTC-aligned WAV chunks per the configured mode's cycle: FT8 = 15s, FT4 = 7.5s, WSPR = 120s.
- Decode daemon is mode-aware: invokes the configured decoder binary (`jt9`, `wsprd`, or `decode_ft8`) as a one-shot subprocess per WAV chunk, parses stdout.
- IPC: Unix socket, line-delimited JSON — same pattern as `mobile_aprs_gateway`'s `magd.sock`. Do not reinvent this protocol; reuse the established shape.
- `hsd`/`hsctl` naming mirrors `magd`/`magctl`.

**Decode window timing is the one part of this system with zero margin for error.** FT8/FT4/WSPR decoders expect audio chunks aligned to specific UTC second boundaries; get that wrong and decode rates collapse even with a perfectly good signal. The GPS-disciplined clock already proven out in `mobile_aprs_gateway` is the asset that makes this tractable — treat GPS time as authoritative over system clock here too, same as that project does.

---

## Decoders

Two vendored submodules under `vendor/`, both providing standalone batch-mode decoding (feed a WAV file, get decoded text on stdout, no live shared-memory IPC needed):

| Decoder | Binary | Modes | Notes |
|---|---|---|---|
| WSJT-X | `jt9`, `wsprd` | FT8, FT4, WSPR | `vendor/wsjtx` → https://github.com/WSJTX/wsjtx.git. Reference implementation — multi-pass subtraction decoding (up to 9 passes: 3 cycles × 3 passes) + 6-mode AP decoding. Best decode yield on a busy band. Heavier build (Qt5 + Fortran). |
| ft8_lib | `decode_ft8` | FT8, FT4 only — **no WSPR** | `vendor/ft8_lib` → https://github.com/kgoba/ft8_lib.git. Lightweight, embedded-oriented (runs on STM32F7, <200KB RAM). No documented subtraction/AP decoding — expect lower yield on crowded bands, verify empirically before trusting README claims alone. |

Decoder is a config setting, not hardcoded — this lets `jt9` and `ft8_lib` be A/B tested against the same captured WAVs.

Sensitivity thresholds (50% decode probability, 2500 Hz reference bandwidth) — grounded facts, not implementation-specific: WSPR ≈ −28 dB SNR, FT8 ≈ −20.8 dB, FT4 ≈ −17.5 dB. Source: WSJT-X/QEX papers, verified via web search during design discussion (2026-07-31).

**Build note:** compiling WSJT-X from source on a 1GB-RAM Pi 3 B+ is expected to be slow/painful (Qt5 + Fortran DSP toolchain). Cross-build on the Pi 5 or dev Mac and copy `jt9`/`wsprd` binaries over rather than building on-device.

---

## Hardware

| Component | Notes |
|---|---|
| Compute | Pi 5 (primary) or Pi 3 B+ — both adequate for **single-mode, headless** decoding per real-world reports (WSPR headless decode reported under 5% CPU via lightweight tools; jt9/wsprd single-channel is fine on Pi 3-class ARM). Multi-channel/deep decoding is where Pi 3 struggles — not this project's design. |
| SDR | RTL-SDR Blog **V4 preferred** for HF work over V3. V4 uses a built-in upconverter + triplexer with front-end filtering (MW/FM/DAB); V3's HF direct-sampling mode has no preselector filtering and suffers Nyquist folding around 14.4 MHz. Both dongles are already owned (see `mobile_aprs_gateway` CLAUDE.md — "interchangeable" there refers to VHF use, this preference is HF-specific). |
| Antenna | Needs an HF-capable antenna — the Diamond MR77SMA used for APRS is VHF/UHF only, not usable here. |
| GPS | Reused from the `mobile_aprs_gateway` Pi setup for UTC discipline. |

---

## Config shape (draft — see `config/config.json.example`)

```json
{
  "band": "20m",
  "mode": "wspr",
  "dial_frequency": "14.095M",
  "sdr_device": "rtl_sdr",
  "gain": "40",
  "sample_rate": "12000",
  "decoder": "wsjtx",
  "decoder_path": "vendor/wsjtx/build/wsprd",
  "listening": false
}
```

Common dial frequencies by band appear in `planning/implementation_plan.md`. Static vs runtime-mutable key split should follow `mobile_aprs_gateway`'s `config.json` convention once `hsd` exists.

---

## Naming rationale

Considered: `hf_skimmer`, `backyard_hf_gateway`, `quietwatch`, `costas_gate`, `weakwatch`, `gridwatch`, `ft_watch`, `spectrogate`, `w7gvr_hf_log`, `hf_spotter`. Settled on `w7gvr_hf_skimmer` — "skimmer" is established ham terminology (CW Skimmer, Reverse Beacon Network) for automated weak-signal listening posts, and the callsign prefix matches how `direwolf.conf`/`aprx.conf` are already scoped to W7GVR in the sibling project.
