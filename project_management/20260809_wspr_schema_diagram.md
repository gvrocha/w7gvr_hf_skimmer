# WSPR database schema diagram

Companion to `20260809_wspr_database_schema_notes.md` — that file has the full verified column types, engines, and prose explanation of each join; this file is just the visual.

## What this kind of diagram is called

A picture of tables-as-boxes-of-columns with lines connecting the columns used to combine them is an **Entity-Relationship Diagram (ERD)**, drawn in **crow's-foot notation** (the forked line-ends encode one/many cardinality).
That's the standard term whether or not the underlying database actually enforces the relationships with real foreign keys.

Since ClickHouse has no foreign-key constraints at all (see the notes file), the relationship lines below are more precisely a **logical ERD**: they show joins that make sense and were verified to return real matching rows, not constraints the database enforces.
A *physical* ERD, by contrast, would only show relationships backed by actual `FOREIGN KEY` declarations — this database has none, so a physical ERD of it would be just eight disconnected boxes.

## Entity → real table mapping

Column types below are simplified (e.g. `LowCardinality(String)` → `String`, `Nullable(Int16)` → `Int16`) — see the notes file for exact ClickHouse types.

| Diagram entity | Real table | Where it exists |
|---|---|---|
| `WSPR_RX` | `wspr.rx` | db1.wspr.live, wd1, wd2 (identical columns) |
| `WSPR_BEACONS` | `wspr.beacons` | db1.wspr.live only |
| `WSPR_MONITORS` | `wspr.monitors` | db1.wspr.live only |
| `BANDS` | `wspr.bands` / `wsprdaemon.bands` | db1.wspr.live (`wspr.bands`) and wd2 (`wsprdaemon.bands`) — **not on wd1** |
| `WSPRDAEMON_SPOTS` | `wsprdaemon.spots` | wd1, wd2 (minor drift — see notes file) |
| `WSPRDAEMON_NOISE` | `wsprdaemon.noise` | wd1, wd2 |
| `PSK_SPOTS` | `psk.spots` | wd1, wd2 |
| `PSKREPORTER_RX` | `pskreporter.rx` | wd1 only |

## Diagram

```mermaid
erDiagram
    WSPR_RX {
        UInt64 id
        DateTime time
        Int16 band
        String rx_sign
        Float32 rx_lat
        Float32 rx_lon
        String rx_loc
        String tx_sign
        Float32 tx_lat
        Float32 tx_lon
        String tx_loc
        UInt16 distance
        UInt16 azimuth
        UInt16 rx_azimuth
        UInt64 frequency
        Int8 power
        Int8 snr
        Int8 drift
        String version
        Int8 code
    }

    WSPR_BEACONS {
        UInt32 id
        String sign
        Float32 lat
        Float32 lon
        Int32 hagl
        Int32 hamsl
        Int8 power
        Int8 max_gain
        String antenna
    }

    WSPR_MONITORS {
        UInt32 id
        String sign
        String locator
        Float32 lat
        Float32 lon
        Int32 hagl
        Int32 hamsl
        Int8 max_gain
        String antenna
        String receiver
        String compute
    }

    BANDS {
        Int16 band
        UInt64 frequency
        String display
        UInt8 is_beacon_band
    }

    WSPRDAEMON_SPOTS {
        DateTime time
        Int16 band
        String rx_sign
        Float32 rx_lat
        Float32 rx_lon
        String rx_loc
        String tx_sign
        Float32 tx_lat
        Float32 tx_lon
        String tx_loc
        Int32 distance
        Int16 azimuth
        Int16 rx_azimuth
        UInt64 frequency
        Int8 power
        Int8 snr
        Int8 drift
        String version
        Int8 code
        Float64 frequency_mhz
        String rx_id
        Float32 v_lat
        Float32 v_lon
        Float32 c2_noise
        UInt16 sync_quality
        Float32 dt
        UInt32 decode_cycles
        Int16 jitter
        Float32 rms_noise
        UInt16 blocksize
        Int32 metric
        UInt8 osd_decode
        UInt16 nhardmin
        UInt8 ipass
        UInt8 proxy_upload
        UInt32 ov_count
        String rx_status
        Int16 band_m
        UInt64 id
    }

    WSPRDAEMON_NOISE {
        DateTime time
        String site
        String receiver
        String rx_loc
        String band
        Float32 rms_level
        Float32 c2_level
        Int32 ov
    }

    PSK_SPOTS {
        DateTime time
        DateTime ingested_at
        String mode
        String decoder_kind
        String rx_sign
        String rx_loc
        String rx_site
        String receiver
        String host_id
        String tx_call
        String grid
        Int16 report
        String message
        UInt64 frequency
        Int16 band
        Int16 snr_db
        Int16 score
        Float32 dt
        Float32 spectral_width_hz
        UInt8 forward_to_pskreporter
        String processing_version
    }

    PSKREPORTER_RX {
        DateTime time
        Int16 band
        String mode
        String rx_sign
        Float32 rx_lat
        Float32 rx_lon
        String rx_loc
        String tx_sign
        Float32 tx_lat
        Float32 tx_lon
        String tx_loc
        UInt16 distance
        UInt16 azimuth
        UInt16 rx_azimuth
        UInt32 frequency
        Int8 snr
        String version
    }

    WSPR_RX }o--|| WSPR_BEACONS : "tx_sign = sign — verified"
    WSPR_RX }o--|| WSPR_MONITORS : "rx_sign = sign — same shape, not retested"
    WSPR_RX }o--|| BANDS : "band = band, numeric code — verified"
    WSPRDAEMON_SPOTS }o--|| BANDS : "band(meters)->code via display, strip 'm' — verified"
    WSPR_RX ||--o{ WSPRDAEMON_SPOTS : "time+rx_sign+tx_sign, band needs code<->meters conversion — verified"
    WSPRDAEMON_SPOTS ||--o{ WSPRDAEMON_NOISE : "rx_sign=site (not receiver!), band cast to string — verified"
    PSK_SPOTS ||--o| PSKREPORTER_RX : "time+rx_sign+tx_call=tx_sign, band needs conversion — NOT verified, timing mismatch"
```

## Reading the crow's-foot symbols

- `||` — exactly one
- `o{` / `}o` — zero or many
- `o|` — zero or one

## The one thing this diagram can't show cleanly

Mermaid draws relationship lines edge-to-edge between whole boxes, not from one specific column's text to another's — so the line between `WSPR_RX` and `WSPRDAEMON_SPOTS` doesn't visually pin to `time`/`rx_sign`/`tx_sign` the way a hand-drawn diagram might.
The relationship label carries that detail as text instead.
The full join SQL for every line above (including the `band` conversion, which is the part most likely to trip someone up) is in `20260809_wspr_database_schema_notes.md`, under "Example cross-table join queries."
