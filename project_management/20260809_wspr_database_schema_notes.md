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
Any cross-table relationship has to be built manually in the query, by joining on shared columns that carry the same real-world meaning — and, critically, **`band` is not one consistent column across this whole database**, even though it's named `band` and typed `Int16` everywhere.
There are two different encodings in play:

- **Numeric band code** (`wspr.bands.band`, `wsprdaemon.bands.band`, `wspr.rx.band`, `pskreporter.rx.band`) — an arbitrary internal code, e.g. `7` = 40m, `14` = 20m, `-1` = LF.
- **Wavelength in meters** (`wsprdaemon.spots.band`, `wsprdaemon.noise.band`, `psk.spots.band`) — the literal band name as a number, e.g. `40` = 40m, `20` = 20m.

This was only caught by testing actual joins, not by reading `DESCRIBE TABLE` output (both look like a column called `band` of type `Int16`) — a join written as `a.band = b.band` across these two families silently returns zero rows or, worse, matches the wrong band.
Convert through the `bands` lookup table (`wspr.bands` on db1.wspr.live, `wsprdaemon.bands` on wd2 — **not present on wd1**, see example below) or hardcode the small mapping table.
Note `wspr.bands.display` uses literal `"LF"`/`"MF"` for the two lowest bands instead of a meters figure (`2200`/`630`), so a mechanical "strip the `m` suffix" conversion needs a special case for those two.

Relationships, with verification status from live testing on 2026-08-09:

- **`wspr.rx` (any host) ↔ `wsprdaemon.spots`** — same conceptual spot. Join on `(time, rx_sign, tx_sign)` plus band *converted* (see above) — a naive `r.band = s.band` returns 0 rows. Verified working.
`wsprdaemon.spots` is roughly a superset — every wsprdaemon-software upload should also appear in `wspr.rx` (forwarded to wsprnet.org too) — but the reverse isn't true.
- **`wsprdaemon.spots` ↔ `wsprdaemon.noise`** — join on `s.rx_sign = n.site` (**not** `n.receiver` — `receiver` is a hardware/antenna identifier local to the station, e.g. `KIWI_2`; `site` is the callsign) plus `toString(s.band) = n.band` (both already use the meters encoding, so no conversion needed here). Verified working.
Noise is a station-level periodic reading, not one row per spot, so this is a many-to-one join.
- **`psk.spots` ↔ `pskreporter.rx`** (wd1 only — `pskreporter` database doesn't exist on wd2) — intended join on `(time, rx_sign, tx_call = tx_sign)` plus band converted (`psk.spots.band` is meters, `pskreporter.rx.band` is numeric code — same mismatch as above). **Not verified**: an exact `time` match returned no rows in testing, even for rows minutes old, and `pskreporter.rx` is a 14-billion-row table sorted `(tx_loc, rx_loc, band, tx_sign, rx_sign, time)`, so a time-only filter forces a near-full scan and previous attempts on it timed out. If pursuing this, try a small time-window (`BETWEEN p.time - 5 AND p.time + 5`) rather than exact equality, and always constrain `tx_sign`/`rx_sign` too so the sort key is used.
- **`wspr.rx` ↔ `wspr.beacons`**: `beacons.sign = rx.tx_sign`. Verified working (69,547 matching rows in a 1-day window on db1.wspr.live).
- **`wspr.rx` ↔ `wspr.monitors`**: `monitors.sign = rx.rx_sign` — the receiver-side mirror of the beacons join above. Same simple string-equality shape as the verified beacons join; not independently re-tested.
- **`wspr.rx` ↔ `wspr.bands`**: join on `band` directly (both use the numeric code, no conversion needed). Verified working.

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

## Example cross-table join queries

All verified live against the stated host on 2026-08-09.
Run via `curl --get --data-urlencode "query=..." "https://<host>/"` per the query-mechanism section above.

**`wspr.rx` × `wsprdaemon.spots`** — get wsprdaemon's decode diagnostics (noise, sync quality, drift metrics) for a spot that also made it to the plain wsprnet.org feed. Run on wd1 or wd2 (needs a `bands` table to convert the two `band` encodings; wd1 doesn't have one, so this example uses wd2's `wsprdaemon.bands`):

```sql
SELECT s.time, s.rx_sign, s.tx_sign, s.band AS band_m, r.snr AS wsprnet_snr, s.snr AS wsprdaemon_snr,
       s.c2_noise, s.sync_quality, s.dt
FROM wsprdaemon.spots s
INNER JOIN wsprdaemon.bands bd ON toInt16OrNull(replaceAll(bd.display, 'm', '')) = s.band
INNER JOIN wspr.rx r ON r.band = bd.band AND r.time = s.time AND r.rx_sign = s.rx_sign AND r.tx_sign = s.tx_sign
WHERE s.time > now() - INTERVAL 1 HOUR
ORDER BY s.time DESC
LIMIT 20
```

**`wsprdaemon.spots` × `wsprdaemon.noise`** — attach the receiving station's noise floor to each spot it made. Run on wd1 or wd2:

```sql
SELECT s.time, s.rx_sign, s.tx_sign, s.band, s.snr, n.rms_level, n.c2_level
FROM wsprdaemon.spots s
INNER JOIN wsprdaemon.noise n ON toString(s.band) = n.band AND s.rx_sign = n.site AND s.time = n.time
WHERE s.time > now() - INTERVAL 1 HOUR
ORDER BY s.time DESC
LIMIT 20
```

**`wspr.rx` × `wspr.beacons`** — attach transmitter station metadata (antenna, power, height) to spots of known WSPR beacons. Run on db1.wspr.live:

```sql
SELECT r.time, r.tx_sign, r.snr, b.antenna, b.power AS beacon_power_dbm, b.hagl, b.hamsl
FROM wspr.rx r
INNER JOIN wspr.beacons b ON r.tx_sign = b.sign
WHERE r.time > now() - INTERVAL 1 DAY
ORDER BY r.time DESC
LIMIT 20
```

**`wspr.rx` × `wspr.bands`** — resolve the numeric band code to a display name/reference frequency. Run on db1.wspr.live (or any host, since both columns use the numeric-code encoding everywhere `wspr.bands`/`wsprdaemon.bands` exists):

```sql
SELECT r.time, r.tx_sign, r.band, bd.display AS band_name, r.frequency
FROM wspr.rx r
INNER JOIN wspr.bands bd ON r.band = bd.band
WHERE r.time > now() - INTERVAL 10 MINUTE
ORDER BY r.time DESC
LIMIT 20
```

## Bulk export tool

`wspr.live/wspr_downloader.php` ("Wspr Exporter") supports CSV, JSON, "JSON Compact," "JSON Rows," and XML — plain text output, not a compressed archive.
Filters: start/end time (UTC), sender callsign (wildcard `%`), receiver callsign (wildcard `%`).

No file-size or row-count preview is offered before generating a download.
The only numbers surfaced are the rate-limit caps described above, plus a general database-total figure ("more than 4,000,000,000 spots") — nothing that estimates the size of a specific filtered export in advance.
The exporter also caps the selectable date range to 31 days per request.

A real sample confirms the row cap applies here too: an export requested for all of `wspr.rx` over 2026-07-01 to 2026-07-31 (no other filter) came back as a 54MB XML file — but its own footer reports `<rows>100000</rows>` against `<rows_before_limit_at_least>204799602</rows_before_limit_at_least>`.
In other words, ~205 million rows matched and only the first 100,000 (per the standard per-request cap) were actually returned — the 31-day *date-range* limit and the 100,000-row *result* limit are independent constraints, and hitting the date-range cap doesn't mean you got everything in that range.
Pulling a full month for an unfiltered global query would need many follow-up requests (e.g. paged by band or by hour), not one.
Also worth noting: XML is verbose — this 100k-row/54MB ratio is roughly 540 bytes/row, well above what CSV or JSON Compact would need for the same data.
Saved locally (gitignored, not committed — 54MB is too large for this repo) at `project_management/reference_data/wspr_live_rx_2026-07_100k-row-sample.xml`.
Its `<meta><columns>` header is itself a live schema description and matches the `wspr.rx` schema documented above exactly.
