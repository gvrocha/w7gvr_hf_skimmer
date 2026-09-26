# First live csdr-pipeline test, and two real bugs it surfaced (2026-09-25/26)

## Context

First live test of the `csdr`-based capture pipeline (see
`hardware/20260920_rtl_fm_demod_bug.md`) against a real antenna on the Pi,
via `hsd`/`hsctl` (not a replayed capture). Also the session that finally
diagnosed the GPS "no fix" issue open since 2026-09-11.

## Deploying csdr to the Pi surfaced a packaging gap

`bin/csdr` failed immediately with `Error loading shared library
libcsdr.so.0.19` — the `csdr` executable dynamically links against
`libcsdr.so`, a shared library built *alongside* it in the same source
tree, not a system package. Only the executable had been copied to the
Pi, not the library. Fixed properly (not just patched live): `bin/
_platform.sh`'s `decoder_bin()` now exports `LD_LIBRARY_PATH`/
`DYLD_LIBRARY_PATH` for the resolved binary's own directory before
exec'ing (harmless for `jt9`/`wsprd`/`decode_ft8`, which don't need it),
and `provisioning/build-bundle.sh`'s csdr cross-build step now copies
`libcsdr.so*` (the full `.so` -> `.so.N` -> `.so.N.M` symlink chain) flat
into `build-linux-aarch64/` alongside the executable, matching where
`_platform.sh` now looks.

## The live test itself: real success

With that fixed, a real 20m FT8 session against the actual antenna
decoded genuine live traffic continuously — every single cycle produced
decodes, a qualitative improvement over `rtl_fm`'s old all-or-nothing
pattern. This is the first live (not replayed) confirmation the `csdr`
fix actually works.

## A confusing "impossible speedup" -- and what it actually was

The session log initially looked deeply wrong: 76 distinct 15-second-cycle
labels (563 total spots) spanning what looked like ~19 minutes, while
other evidence (the `hsd.py` process's own `ps` uptime) suggested the
whole thing should have taken under 5 minutes. The user, watching in real
time, was confident it hadn't run anywhere near 19 minutes -- correctly,
as it turned out.

Root cause, found by checking real file mtimes (not the embedded
filename labels, which are a separate, unrelated bug -- see below):
**`DecodeWorker` was re-decoding old files.** `chunks/` has no cleanup/
retention policy (a known, already-documented gap) -- WAV files from
*every* previous test session were still sitting there. `DecodeWorker`'s
poll loop starts with `_last_processed = None`, and its filter
(`self._last_processed is None or p.name > self._last_processed`) treats
*every* pre-existing file as "new" on the very first poll. So a fresh
`hsctl start` doesn't just watch for new chunks -- it immediately sweeps
up and re-decodes the *entire accumulated backlog* from every earlier
session, all in one rapid burst, before genuinely-new live chunks start
arriving at the real 15-second cadence. That burst is what inflated the
apparent cycle count and spot total.

Confirmed directly: of 126 total `.wav` files present, only 79 postdated
the moment `hsctl start` was actually called for this session -- the
other 47 were stale leftovers from earlier sessions, immediately
re-decoded.

**Fixed:** `DecodeWorker._run()` now seeds `_last_processed` to the
latest pre-existing filename (if any) *before* entering its poll loop,
so a fresh worker only ever reacts to files that arrive after it starts.
Verified real capture pacing separately (a controlled, `hsd`-independent
60-second pipeline run showed consecutive chunk files land at almost
exactly 1-second-per-real-second, with clean `rtl_sdr` output and no
CPU/buffer-overrun issues) -- so the *pipeline itself* was never the
problem, only `DecodeWorker`'s handling of pre-existing backlog.

## The actual GPS "no fix" issue: a flaky USB dongle, not a code bug

Chunk filenames were also stamped with the wrong date (`2026-09-11`
instead of the real date) throughout the live test, because `hsd`'s
`GpsClock` had no fix and fell back to the Pi's own RTC-less (and
essentially frozen) system clock -- the same free-running fallback
behavior that's always been correct by design, just undesirable here.

Investigating *why* there was no fix: `gpsd` itself reported 0-15
satellites in view but consistently 0 used, `mode` stuck at 1, across
multiple checks and even after swapping in a second GPS unit (which the
user suspected was also flaky). The user reported the GPS unit's own LED
was blinking green -- which, from prior experience with the same
hardware on `mobile_aprs_gateway`, normally indicates a real fix. That
contradiction was the key clue.

Checked raw NMEA directly (`cat /dev/ttyACM0`, `gpsd` stopped first to
free the device) -- got **nothing at all**, not even after confirming
`gpsd` had released the port. The device node itself had vanished.
`dmesg` explained why: real USB errors (`dwc_otg_hcd_urb_dequeue` FSM
timeouts) followed by an actual USB disconnect/reconnect event -- and on
reconnecting, the GPS receiver came back as **`/dev/ttyACM1`**, not
`/dev/ttyACM0`. `gpsd` was still configured for the old, now-nonexistent
device node, so it had been reading nothing at all while the receiver
itself was working fine the whole time (matching the blinking LED).

**Fix:** updated `/etc/conf.d/gpsd`'s `DEVICES=` to the new node,
restarted `gpsd` -- picked up a real fix (`mode: 2`) within 15 seconds.
Verified end-to-end: `hsd`'s own `GpsClock.timestamp()` then correctly
returned the real date while the Pi's raw system clock stayed stuck at
`2024-10-09`, and a live one-cycle `hsd` test produced correctly-dated
chunk filenames (`260926_034800.wav`, `260926_034815.wav`) for the first
time.

This has no code fix -- it's a hardware reliability issue with this
particular GPS unit's USB connection (loose cable/connector, marginal
power, or a flaky unit -- not yet isolated further). Worth remembering
for next time: if GPS satellite counts look wrong or stuck, check
`dmesg` for a USB disconnect/reconnect and confirm `gpsd`'s configured
device path still matches reality before assuming a software problem.

## Summary of fixes landed this session

- `bin/_platform.sh`: exports `LD_LIBRARY_PATH`/`DYLD_LIBRARY_PATH` for
  vendored binaries with co-located shared libraries.
- `provisioning/build-bundle.sh`: copies `libcsdr.so*` alongside the
  `csdr` executable during cross-build.
- `src/decode_worker.py`: `DecodeWorker` no longer re-decodes
  pre-existing files at startup (new regression test in
  `tests/test_decode_worker.py`).
- GPS: no code change, but the actual "no fix" root cause is understood
  and resolved for this session (device node drift after a USB
  reconnect).
