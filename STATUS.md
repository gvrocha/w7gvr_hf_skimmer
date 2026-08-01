# STATUS.md

**Last updated:** 2026-08-01

## Where this stands right now

No radio or GPS hardware on hand yet.
Everything buildable without them is built and tested.
`hsd`/`hsctl` already run end-to-end against WAV files dropped in manually — the only work left before live over-the-air spots is a small capture-worker integration step, plus the physical hardware itself (GPS puck, then RTL-SDR + HF antenna).

## Done

- [x] Phase 0 scaffold — repo layout, planning docs, vendored decoder submodules (`wsjtx`, `ft8_lib`, `minimal_pi`)
- [x] `bin/{jt9,wsprd,decode_ft8}` arch-dispatch wrappers — built and decoding real audio on `darwin-arm64`
- [x] `src/decode_worker.py` — per-decoder output parsers (jt9/decode_ft8/wsprd) + directory-watch pipeline
- [x] `src/hsd.py` / `src/hsctl.py` — core daemon + CLI, Unix socket IPC, session/TSV logging
- [x] `src/gps_clock.py` — GPS-vs-system offset discipline, stub-tested
- [x] `src/capture_worker.py` — UTC-aligned WAV chunking arithmetic, unit-tested standalone (plus a real-time integration test against a synthetic generator standing in for `rtl_fm`)
- [x] 38 passing tests (`tests/`) — `PYTHONPATH=src python3 -m unittest discover tests -v`

## In progress / next up

- [ ] Wire `capture_worker.CaptureWorker` into `hsd.py`'s `start`/`stop` — WAV chunks currently have to be dropped into `chunks/` externally
- [ ] Track B-GPS: source a GPS receiver, swap `gps_clock`'s stub source for the real `GpsdSource`, hardware-validate offset/fix-quality behavior
- [ ] Track B-Radio: set up the RTL-SDR + HF antenna, swap the synthetic PCM generator for the real `rtl_fm` invocation, run the Phase 1 exit criterion (sustained multi-hour real-signal capture verified against UTC boundaries)

## Known gaps / open questions

- `build_rtl_fm_cmd()`'s exact argv (the USB-demod recipe) is unverified against real hardware.
- `wsprd` is invoked without `-f` (no dial frequency), so its frequency field is baseband-relative Hz, consistent with `jt9`/`decode_ft8` — absolute RF frequency (dial + offset) isn't computed anywhere yet.
- No cleanup/retention policy for processed WAV chunks in `chunks/` yet — they accumulate on disk indefinitely.

## Reference

- Full phased plan: `planning/implementation_plan.md`
- Design tradeoffs this session (single-daemon vs. multi-daemon decision, `magd.py`/`magctl.py` pattern mapping, decoder-format quirks): git log, commits `75c7ac9`..`bb6f990`
