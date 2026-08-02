# STATUS.md

**Last updated:** 2026-08-02

## Where this stands right now

No radio hardware on hand yet, but a real GPS unit (u-blox 7) got a genuine outdoor 3D fix this session (raw NMEA, not yet through `gpsd`).
Everything else buildable without hardware is built and tested — including a full offline Pi-provisioning pipeline (cross-built decoders + an offline `apk` bundle), not yet run against an actual Pi.
`hsd`'s `start`/`stop` now drive both `CaptureWorker` and `DecodeWorker` internally, wired only through the shared `chunks/` directory — WAV chunks no longer need to be dropped in manually.
`vendor/minimal_pi` is now pinned to upstream's Milestone 1 (headless-SSH-via-Ethernet, confirmed on real Pi 3B+ hardware, hostname `minpi`) plus in-progress Milestone 2 (WiFi AP) work — this repo's `provisioning/deploy.sh` already defaults to that exact Pi's address (`root@169.254.100.1`), so a dry run is unblocked as soon as that Pi is reconnected.
The remaining work before live over-the-air spots is validating the GPS fix through `gpsd` (not just raw NMEA), the `deploy.sh`/`install.sh` dry run against that Pi, and the RTL-SDR + HF antenna itself.

## Done

- [x] Phase 0 scaffold — repo layout, planning docs, vendored decoder submodules (`wsjtx`, `ft8_lib`, `minimal_pi`)
- [x] `bin/{jt9,wsprd,decode_ft8}` arch-dispatch wrappers — built and decoding real audio on `darwin-arm64`
- [x] `src/decode_worker.py` — per-decoder output parsers (jt9/decode_ft8/wsprd) + directory-watch pipeline
- [x] `src/hsd.py` / `src/hsctl.py` — core daemon + CLI, Unix socket IPC, session/TSV logging
- [x] `src/gps_clock.py` — GPS-vs-system offset discipline, stub-tested
- [x] `src/capture_worker.py` — UTC-aligned WAV chunking arithmetic, unit-tested standalone (plus a real-time integration test against a synthetic generator standing in for `rtl_fm`)
- [x] 38 passing tests (`tests/`) — `PYTHONPATH=src python3 -m unittest discover tests -v`
- [x] Track B-GPS, partial (2026-08-01): real u-blox 7 GPS unit confirmed alive (raw NMEA read directly off `/dev/cu.usbmodem1301`), `gpsd` brought up against it, and `GpsdSource` rewritten to talk to `gpsd`'s own JSON socket protocol directly (stdlib `socket`/`json` — no Python `gps` bindings exist for this platform). No-fix path (`mode=1`) validated end-to-end through `GpsClock` against the real device, both indoors and after ~3 minutes outside with only 0-2 satellites visible.
- [x] `provisioning/` — offline Pi provisioning, entirely no-internet-required (2026-08-01): Colima (Alpine 3.21 aarch64, native speed) set up sudo-free via plain user-owned binaries (no Homebrew write access on this machine). `jt9`/`wsprd`/`decode_ft8` cross-built for `linux-aarch64` and validated against real audio (decode results match the `darwin-arm64` builds exactly). A self-contained `apk` bundle (`python3`, `gpsd`, `gpsd-openrc`, `rtl-sdr`, 26 packages with a real `APKINDEX.tar.gz`) verified installable in a `--network none` container. `hsd.openrc` mirrors `mobile_aprs_gateway`'s own init script template. Not yet run against a real Pi.
- [x] Wire `capture_worker.CaptureWorker` into `hsd.py`'s `start`/`stop` (2026-08-02): `start_listening()` now builds the `rtl_fm` argv via `build_rtl_fm_cmd()` and starts a `CaptureWorker` alongside `DecodeWorker`; `stop_listening()` tears both down. The two workers are wired only through the shared `chunks/` directory, same as `DecodeWorker`'s own design. Still unverified against real hardware (no RTL-SDR/antenna on hand) — covered by stubbed unit tests only, same pattern as the existing `DecodeWorker` tests.
- [x] Track B-GPS, real outdoor 3D fix acquired (2026-08-02): u-blox 7 got a genuine fix outdoors — `$GPGGA` fix quality 1, 4-5 satellites used, HDOP 1.4-1.9, real lat/lon/altitude. Read via raw `cat` of `/dev/cu.usbmodem1301`, not through `gpsd` — so this proves the hardware/sky-view combination works, but doesn't yet exercise `GpsdSource`/`GpsClock`'s actual offset-computation code path.
- [x] `vendor/minimal_pi` submodule bumped `ddf4500` → `bf4fbd1` (2026-08-02): pulls in upstream's Milestone 1 (headless SSH via Ethernet, single-script `flash-and-install.sh`, confirmed end-to-end on real Pi 3B+ hardware, hostname `minpi`) plus Milestone 2 WiFi AP work-in-progress (`hostapd`/`dnsmasq` wired into `build-apkovl.sh`, live AP hardware test still open upstream). This repo's `provisioning/deploy.sh` already defaults to that confirmed Pi's address (`root@169.254.100.1`), so the dry run below is unblocked as soon as that Pi is reconnected.

## In progress / next up

- [ ] Track B-GPS, remaining: run `gpsd` against the u-blox 7 while it still has a fix, and validate `GpsClock`'s actual offset-computation math against a real fix through `GpsdSource` — only the no-fix path and now a raw-NMEA fix have real-hardware coverage, `gpsd` itself hasn't been exercised against a real fix yet
- [ ] Track B-Radio: set up the RTL-SDR + HF antenna, swap the synthetic PCM generator for the real `rtl_fm` invocation, run the Phase 1 exit criterion (sustained multi-hour real-signal capture verified against UTC boundaries)
- [ ] `provisioning/deploy.sh` + `install.sh` dry run against the real Pi 3B+ (`minpi`, `root@169.254.100.1`) once it's reconnected via the direct-cable link

## Known gaps / open questions

- `build_rtl_fm_cmd()`'s exact argv (the USB-demod recipe) is unverified against real hardware.
- `wsprd` is invoked without `-f` (no dial frequency), so its frequency field is baseband-relative Hz, consistent with `jt9`/`decode_ft8` — absolute RF frequency (dial + offset) isn't computed anywhere yet.
- No cleanup/retention policy for processed WAV chunks in `chunks/` yet — they accumulate on disk indefinitely.
- GPS offset math (the actual point of `gps_clock.py`) is still only verified against the stub, not a real fix through `gpsd` — the raw-NMEA fix acquired this session (2026-08-02) proves the hardware works outdoors, but `GpsdSource` itself hasn't been run against a live fix yet.

## Reference

- Full phased plan: `planning/implementation_plan.md`
- Design tradeoffs this session (single-daemon vs. multi-daemon decision, `magd.py`/`magctl.py` pattern mapping, decoder-format quirks): git log, commits `75c7ac9`..`bb6f990`
