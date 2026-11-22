# Spot tagging + session-timestamped TSV — implementation plan

**Status:** implemented and deployed to `minpi`, 2026-11-22 (HSD_VERSION 0.2). First live session: `decodes_20261122_163441_0019_hsd_v0.2.tsv`, 15m FT8. Two details changed from the original proposal while building it, both marked below.

## Goal

Make every decoded message in a session TSV self-describing: when it was heard, where the receiver was, which band and dial frequency it was on, and where it sat inside the audio passband.
Also make the session TSV filename carry the session's start time.

Today a row only has an ISO `utc_timestamp`, the audio offset (`freq_hz`), and the message fields.
Band, dial frequency, and receiver position aren't recorded anywhere, so a TSV can't be interpreted on its own once the config has changed or the Pi has moved.

## Current state (what exists to build on)

- `src/hsd.py` — `SPOT_TSV_FIELDS`, `_spot_to_row()`, `_init_session()` (filename `decodes_{seq}_hsd_v{HSD_VERSION}.tsv`), `_now_utc()` (GPS-disciplined clock, system-time fallback).
- `src/decode_worker.py` — `Spot` dataclass; `decode_chunk()` already overrides each spot's `utc_timestamp` with `chunk_start()` parsed from the WAV filename, so the timestamp is already the start of the FT8 cycle (15 s aligned, 7.5 s for FT4, 120 s for WSPR).
- `src/gps_clock.py` — `GpsdSource.next()` reads gpsd TPV reports but `_TpvReport` keeps only `mode` and `time`, dropping `lat`/`lon`/`alt`.
- `tools/watch_decodes.py` — already has a `_grid_square(lat, lon)` Maidenhead helper and its own separate gpsd poller.
- `src/capture_worker.py` — the csdr USB bandpass is `0 .. 1/3 × 12000 Hz` = **0–4000 Hz**, not 3 kHz. Decodes can appear anywhere in that range; the conventional FT8 sub-band is roughly 200–3000 Hz.

## New row schema (one row per decoded message)

| Column | Example | Source / notes |
|---|---|---|
| `cycle_utc` | `2026/11/22 16:19:45` | Replaces `utc_timestamp`. `chunk_start()` formatted `%Y/%m/%d %H:%M:%S`. Always on a cycle boundary (`:00/:15/:30/:45` for FT8). |
| `band` | `20m` | `config["band"]` at session start. |
| `dial_hz` | `14074000` | `config["dial_frequency"]` parsed to an integer Hz. Config currently holds both `"14074000"` and `"14.095M"` styles, so this needs a small parser. |
| `audio_hz` | `1366` | Renamed from `freq_hz`: the signal's offset inside the 0–4000 Hz passband. |
| `rf_hz` | `14075366` | `dial_hz + audio_hz`: the actual on-air frequency, the value spotting networks (PSK Reporter, RBN) report. |
| `mode`, `snr`, `dt`, `message` | | Unchanged. |
| `tx_call`, `tx_grid`, `dbm`, `drift` | | `call`/`grid` renamed with a `tx_` prefix so the remote station's grid can't be confused with the receiver's own `rx_grid`. Still only populated by the WSPR parser. |
| `rx_lat`, `rx_lon` | `47.612345`, `-122.334567` | Latest GPS fix, 6 decimals. Same `rx_` prefix `mobile_aprs_gateway` already uses (`rx_lat`/`rx_lon` in `process_packets_data.py`). |
| `rx_alt_m` | `52.1` | Blank unless the fix is 3D. |
| `rx_grid` | `CN87ul` | 6-character Maidenhead from `rx_lat`/`rx_lon`. |
| `rx_fix_mode` | `3` | gpsd mode (2 = 2D, 3 = 3D). Blank if no fix has ever been seen this daemon lifetime. |
| `rx_fix_age_s` | `4` | Seconds between the fix time and when the row was written. *(Changed from "relative to `cycle_utc`": the row is written about one cycle after `cycle_utc`, so measuring from write time always gives a non-negative, honest staleness.)* Lets a reader throw out rows whose position is stale (e.g. GPS dropped off USB mid-session, the failure mode documented in STATUS.md 2026-09-26). |

If no fix has been seen yet, all `rx_*` columns are blank. The row is still written.

## New session filename

`sessions/decodes_YYYYMMDD_HHMMSS_NNNN_hsd_v0.2.tsv`, e.g. `decodes_20261122_161930_0019_hsd_v0.2.tsv`.

- The timestamp is the session's start time from `_now_utc()`, taken once in `_init_session()`.
- It comes first so a plain `ls` sorts chronologically. The sequence number stays as a tie-breaker and to keep continuity with the existing `.hsd_log_counter`.
- `HSD_VERSION` goes `0.1` → `0.2`, since the column set changes. Old `_v0.1` files stay as they are; nothing rewrites them.

**Clock caveat:** the Pi has no RTC and its system clock currently reads 2024-10-09. If `hsd start` runs before gpsd has a fix, `_now_utc()` falls back to that wrong clock and the filename gets a 2024 date. Decision for implementation (recommended first option):

1. Name the file with whatever `_now_utc()` returns, and log a warning in `hsd.log` when it came from the system-clock fallback. Simplest; the per-row `cycle_utc` self-corrects once GPS locks.
2. Hold session start until `GpsClock.is_ready()`, with a timeout. More correct, but it makes `hsctl start` block and changes existing behavior.

## Implementation steps

1. **`gps_clock.py`: keep position.**
   Extend `_TpvReport` with `lat`, `lon`, `alt` (gpsd's `altHAE` or `alt`, whichever is present).
   Have `GpsClock` store the last report with mode ≥ 2 alongside its time offset, and add `last_position()` returning `(lat, lon, alt, mode, fix_time)` or `None`.
   Same gpsd connection, no second socket.
   `GpsClock` polls every 30 s (`fixed_interval`) by default. *(Changed: `hsd` now passes `GPS_POLL_INTERVAL_S = 10`, so a spot's position is at most ~10 s old.)*
2. **Move `_grid_square()`** from `tools/watch_decodes.py` into a shared `src/` module (e.g. `src/maidenhead.py`), extend it to 6 characters, and use it from both places.
3. **`decode_worker.py`:** rename `Spot.freq_hz` → `audio_hz`. Nothing else changes; the band, dial, and receiver-location tags are session/daemon-level, not decoder output.
4. **`hsd.py`:**
   - New `SPOT_TSV_FIELDS` per the table above.
   - `_init_session()` records the session's `band` and `dial_hz` and builds the new filename.
   - `_spot_to_row()` formats `cycle_utc`, computes `rf_hz`, and fills `rx_*` from `_gps_clock.last_position()`.
   - Socket `spot` events carry the same row dict so `hsctl` and `watch_decodes` see the new fields.
5. **Consumers:** update `src/hsctl.py:91` and `tools/watch_decodes.py:100-104` for the renamed keys (`cycle_utc`, `audio_hz`). `watch_decodes` can then show `rx_grid` from the event itself instead of polling gpsd separately, which removes its second gpsd connection.
6. **Tests:** update `tests/test_hsd.py` and `tests/test_decode_worker.py` for the renames. Add tests for:
   - the `dial_frequency` parser (`"14074000"`, `"14.074M"`, `"14074k"`)
   - `cycle_utc` formatting
   - `rf_hz` arithmetic
   - 6-character Maidenhead against a few known points
   - blank `rx_*` columns when there's no fix
   - the new filename format
   Use a stub GPS source with a fixed position, the same pattern the existing `GpsClock` tests use.
7. **Deploy and live check:** `deploy.sh` to `minpi`, run a few minutes of 20m FT8, and confirm on real data that `cycle_utc` lands on 15 s boundaries, `rx_grid` matches the real location, `rx_fix_age_s` stays small, and `rf_hz` falls in 14.074–14.078 MHz.

## Out of scope here

These came up in the same discussion but are separate pieces of work:

- Auto-start listening at boot.
- Scheduled band-hopping. This plan does make it possible later, because `band`/`dial_hz` become per-row rather than per-config, so one TSV could span several bands.
- Narrowing the csdr passband from 4000 Hz to 3000 Hz. Not needed for tagging, and it would throw away valid decodes in 3–4 kHz.
