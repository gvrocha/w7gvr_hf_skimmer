# WSPR public database schema notes

Reference notes on the public WSPR spot databases, gathered while scoping whether `w7gvr_hf_skimmer` could cross-check its own decodes against external sources.
Verified 2026-08-09 by querying the live HTTP endpoints directly (not just reading docs), since docs for these services are informal and can drift from the running schema.

## How the pieces relate

- **wsprnet.org** is the original spot database — the upload destination for stock WSJT-X/WSPR clients.
- **wspr.live** is a read-only ClickHouse mirror of wsprnet.org, going back to 2008.
Hosted at `db1.wspr.live`.
- **wsprdaemon** is separate receive-station software that uploads richer spot data than stock WSJT-X (noise floor, sync quality, decode diagnostics).
It runs its own servers, `wd1.wsprdaemon.org` and `wd2.wsprdaemon.org`.
- **wspr.rocks** is not a database — it's a client-side SQL sandbox/dashboard that lets a user pick which backend (wspr.live, WD1, WD2) to query, then charts/maps the result.
It has no API of its own.

## Query mechanism

All three hosts (`db1.wspr.live`, `wd1.wsprdaemon.org`, `wd2.wsprdaemon.org`) expose the standard **ClickHouse HTTP interface**: a plain `GET` with the SQL in a `query` parameter, no authentication required for reads.

```
https://db1.wspr.live/?query=SELECT+1
https://wd1.wsprdaemon.org/?query=SELECT+1
https://wd2.wsprdaemon.org/?query=SELECT+1
```

Append `FORMAT JSON`, `FORMAT TSV`, etc. to control output shape.
Any HTTP client works — no ClickHouse native protocol (port 9000) needed.

**Use GET, not POST-with-form-body.** `db1.wspr.live` returns a bare 403 on POST entirely.
wd1/wd2 accept POST, but only if the raw SQL is the POST body itself — sending a form-encoded `query=...` body (the naive `curl --data-urlencode "query=..."` default) makes ClickHouse try to parse the literal string `query=SELECT ...` as SQL and fail with a syntax error.
`curl --get --data-urlencode "query=..."` (forces GET, URL-encodes correctly) works uniformly across all three hosts.

**Rate limits / etiquette (documented on wspr.live):** 100,000 rows per query, up to 1,000 queries/day before throttling to 10,000-row results, ~5s cooldown expected between requests, and table-format output capped at 10,000 rows (never use table format for automated queries).
Always filter by time range and band rather than pulling unbounded data.

**Introspection caveat:** `SHOW TABLES` / `system.tables` return empty on `db1.wspr.live` (the anonymous account's grants don't expose schema listing) and were only partially usable on wd1/wd2.
The table lists below were built by combining `SHOW DATABASES` + `SHOW TABLES FROM <db>` (which worked on wd1/wd2) with targeted `DESCRIBE TABLE` probing by guessed name.
This is not guaranteed complete — an ungranted or unguessed table would be invisible to this method.

## Tables found, by host

### db1.wspr.live

- `wspr.rx` — every WSPR spot since 2008 (the wsprnet.org mirror)
- `wspr.beacons` — WSPR beacon operator info
- `wspr.monitors` — WSPR monitor operator info
- `wspr.bands` — static band/frequency reference table

`psk`, `pskreporter`, and `wsprdaemon` databases do not exist on this host (confirmed via `Database ... does not exist` errors, not just absence from a list).

### wd1.wsprdaemon.org

- `wspr.rx` — mirror of wspr.live's spot table, identical schema
- `wsprdaemon.spots` — wsprdaemon-only extended spot data (superset of `wspr.rx` plus diagnostics)
- `wsprdaemon.noise` — receiver noise-floor reports from wsprdaemon-software users
- `psk.spots` — pre-forward PSKReporter staging data (has `decoder_kind`, `forward_to_pskreporter`, `spectral_width_hz`, etc. — this is wsprdaemon's own decode pipeline, not just wspr)
- `pskreporter.rx` — normalized/forwarded PSKReporter spot data, same shape as `wspr.rx`

`default` database exists but has no tables.

### wd2.wsprdaemon.org

- `wspr.rx` — same as wd1
- `wsprdaemon.spots` — same as wd1
- `wsprdaemon.noise` — same as wd1
- `wsprdaemon.bands` — band/frequency reference table (not present on wd1)
- `psk.spots` — same as wd1
- `default.wsprdaemon_spots` — a **view** (`CREATE VIEW ... AS SELECT * FROM wsprdaemon.spots`), not a separate physical table
- `default.wsprdaemon_noise` — a **view** over `wsprdaemon.noise`, but its declared column list includes `seqnum`, `running_jobs`, `receiver_descriptions`, which do not exist in the current `wsprdaemon.noise` table definition — the view's cached header looks stale relative to the base table (see caveat below)

No `pskreporter` database on wd2 — asymmetric with wd1.

## Table engines and sorting keys

Pulled via `SHOW CREATE TABLE` (GET request — these hosts reject the query-in-POST-body form; use `?query=...` in the URL, same as everything else here).
ClickHouse has no primary-key/foreign-key concept in the relational sense.
What it has instead is a **sorting key** (`ORDER BY` in the table definition), which determines which column filters are cheap versus which force a full scan, plus optional secondary "skip" indexes.
There is no referential integrity anywhere — nothing stops a row in one table from pointing at a callsign or grid that doesn't exist in another table.

| Table | Engine | Partition | Sorting key (`ORDER BY`) | Skip indexes |
|---|---|---|---|---|
| db1.wspr.live `wspr.rx` | ReplacingMergeTree | `toYYYYMM(time)` | `(band, time, id)` | minmax on `id` |
| wd1/wd2 `wspr.rx` | ReplacingMergeTree | `toYYYYMM(time)` | `(rx_sign, band, time, id)` | minmax on `id` |
| wd1/wd2 `wsprdaemon.spots` | ReplacingMergeTree | `toYYYYMM(time)` | `(rx_sign, tx_sign, band, rx_id, time)` | none |
| wd1/wd2 `wsprdaemon.noise` | ReplacingMergeTree | `toYYYYMM(time)` | `(site, band, receiver, time)` | none |
| wd1/wd2 `psk.spots` | ReplacingMergeTree | `toYYYYMM(time)` | `(mode, rx_sign, receiver, time, frequency)` | bloom_filter on `rx_sign`, `tx_call` |
| wd1 `pskreporter.rx` | MergeTree | `toYYYYMM(time)` | `(tx_loc, rx_loc, band, tx_sign, rx_sign, time)` | none |
| db1.wspr.live `wspr.beacons`/`wspr.monitors`/`wspr.bands` | MergeTree | none | `id` (or `band` for `bands`) | none |

**Practical implication:** filtering by the leading sorting-key column(s) is what makes a query fast.
db1.wspr.live's `wspr.rx` is sorted `(band, time, id)`, so `WHERE band = 20 AND time > ...` is efficient there.
wd1/wd2's copy of the *same-looking* table is sorted `(rx_sign, band, time, id)` instead — a query that's fast on db1.wspr.live because it filters by band+time first will scan more on wd1/wd2 unless it also filters by `rx_sign`.
Don't assume "same columns" means "same query plan" across hosts.

**Schema drift between wd1 and wd2, found via `SHOW CREATE TABLE` (not visible from `DESCRIBE TABLE` alone):**
- `wsprdaemon.spots.id` is a computed `ALIAS` column on wd1 (`cityHash64(rx_sign, tx_sign, band, rx_id, time, frequency)` — not stored) but **does not exist at all** on wd2.
- `wsprdaemon.spots.azimuth` / `rx_azimuth` are `Int16`/`Int32` on wd1 but `Float32` on wd2.
- `pskreporter` database exists only on wd1, not wd2.
- `wsprdaemon.bands` exists only on wd2.

Treat wd1 and wd2 as similar, not interchangeable — a query built against one may error or behave differently against the other.

## How the tables relate

There are no declared foreign keys.
Any cross-table relationship has to be built manually in the query, by joining on shared columns that carry the same real-world meaning:

- **`wspr.rx` (any host) ↔ `wsprdaemon.spots`**: same conceptual spot, joinable on `(time, rx_sign, tx_sign, band, frequency)`.
`wsprdaemon.spots` is roughly a superset — every wsprdaemon-software upload should also appear in `wspr.rx` (since it's forwarded to wsprnet.org too), but the reverse isn't true.
- **`wsprdaemon.spots` ↔ `wsprdaemon.noise`**: joinable on `(time, band, receiver)`/`site` — noise is a station-level periodic reading, not one row per spot, so this is a many-to-one join (many spots share one noise reading for the same station/band/time bucket).
- **`psk.spots` ↔ `pskreporter.rx`**: `psk.spots` is wsprdaemon's pre-forward staging row (has `forward_to_pskreporter` flag); `pskreporter.rx` is the normalized post-forward view, joinable on `(time, rx_sign, tx_call/tx_sign, band, frequency)`.
- **`wspr.rx` ↔ `wspr.beacons` / `wspr.monitors`**: join on callsign (`beacons.sign` = `rx.tx_sign`, `monitors.sign` = `rx.rx_sign`) to get static station metadata (antenna, height, power) for a spot's transmitter/receiver.
- **`wspr.rx` ↔ `wspr.bands`**: join on `band` to get the display name/reference frequency for a numeric band code.

None of these joins are enforced or guaranteed consistent — they're conventions inferred from column naming and shared meaning across independently-populated tables, not schema-level constraints.

## Key schemas

### `wspr.rx` (identical on db1.wspr.live, wd1, wd2)

```
id UInt64, time DateTime, band Int16, rx_sign LowCardinality(String),
rx_lat Float32, rx_lon Float32, rx_loc LowCardinality(String),
tx_sign LowCardinality(String), tx_lat Float32, tx_lon Float32, tx_loc LowCardinality(String),
distance UInt16, azimuth UInt16, rx_azimuth UInt16, frequency UInt64,
power Int8, snr Int8, drift Int8, version LowCardinality(String), code Int8
```

### `wsprdaemon.spots` (wd1/wd2 only)

Superset of `wspr.rx` plus: `frequency_mhz, rx_id, v_lat, v_lon, c2_noise, sync_quality, dt, decode_cycles, jitter, rms_noise, blocksize, metric, osd_decode, nhardmin, ipass, proxy_upload, ov_count, rx_status, band_m`.
This is the table of interest if `w7gvr_hf_skimmer` ever wants to compare its own decode diagnostics against a reference wsprdaemon station.

### `wsprdaemon.noise` (wd1/wd2)

```
time DateTime, site LowCardinality(String), receiver LowCardinality(String),
rx_loc LowCardinality(String), band LowCardinality(String),
rms_level Float32, c2_level Float32, ov Int32
```

### `psk.spots` (wd1/wd2)

```
time DateTime, ingested_at DateTime, mode LowCardinality(String), decoder_kind LowCardinality(String),
rx_sign LowCardinality(String), rx_loc LowCardinality(String), rx_site LowCardinality(String),
receiver LowCardinality(String), host_id LowCardinality(String), tx_call LowCardinality(String),
grid LowCardinality(String), report Nullable(Int16), message String, frequency UInt64, band Int16,
snr_db Nullable(Int16), score Nullable(Int16), dt Float32, spectral_width_hz Nullable(Float32),
forward_to_pskreporter UInt8, processing_version LowCardinality(String)
```

### `pskreporter.rx` (wd1 only)

```
time DateTime, band Int16, mode LowCardinality(String), rx_sign LowCardinality(String),
rx_lat Float32, rx_lon Float32, rx_loc LowCardinality(String), tx_sign LowCardinality(String),
tx_lat Float32, tx_lon Float32, tx_loc LowCardinality(String), distance UInt16, azimuth UInt16,
rx_azimuth UInt16, frequency UInt32, snr Int8, version LowCardinality(String)
```

## Bulk export tool

`wspr.live/wspr_downloader.php` ("Wspr Exporter") supports CSV, JSON, "JSON Compact," "JSON Rows," and XML — plain text output, not a compressed archive.
Filters: start/end time (UTC), sender callsign (wildcard `%`), receiver callsign (wildcard `%`).

No file-size or row-count preview is offered before generating a download.
The only numbers surfaced are the rate-limit caps described above, plus a general database-total figure ("more than 4,000,000,000 spots") — nothing that estimates the size of a specific filtered export in advance.
