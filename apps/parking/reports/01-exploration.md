# P1 — Exploration of the Melbourne 2019 parking data

Prompt P1 of [`PARKING_PLAN.md`](../PARKING_PLAN.md) (§8): exploration only, no pipeline. Branch `parking`, nothing committed.
All numbers come from `apps/parking/scripts/explore.py --step all`; the full tables are in [`01-exploration-output.md`](01-exploration-output.md) (section numbers § below refer to that file).

## Summary

- **Data is in place and hashed.** Three raw files in `apps/parking/raw/` (git-ignored), SHA-256 in `apps/parking/raw_manifest.json`. The sensor file has **42,672,743 rows**, all of 2019, no unparseable timestamps; **timestamps are Melbourne local wall-clock time** (DST anomalies at exactly the local change hours).
- **One parked car is often split into several rows.** Each bay's rows tile its timeline with no gaps, and the system cuts a vehicle-present interval at midnight and at restriction boundaries (07:30, 18:30, 20:30 …). 21.7 % of vehicle-present rows continue the previous one. Stitched, the 22.59 M vehicle-present rows become **17.70 M stays** (12.60 M after the 2 min – 24 h cleaning rule). Counting rows instead would double-count arrivals (design A) and occupied hours (design B).
- **No direct ID join works**, neither against the bay file nor against the On-street Parking Bay Sensors feed: every numeric overlap (BayId, DeviceId) lands on bays in other streets (0–6 % plausible). The 2019 identifiers belong to the retired sensor system; the current datasets use a different kerbside-ID space.
- **The road-segment join works**: 84.6 % of stays exactly (street + both cross streets), **89.7 % with the recommended variant 2c** (also accept a match with one cross street different when exactly one segment qualifies), 99.4 % if ambiguous partial matches are averaged (2b). Precision is one street segment (~100–200 m): **276 distinct locations in 251 cells of 0.001°** over the year.
- **Day: 2019-10-11 (Friday)**, the busiest regular weekday with normal sensor coverage and no known event: 38,400 clean located stays → **38,400 design-A reports, 79,355 design-B reports**. The raw top days (Dec 19/13, Sep 13/26) fall in Christmas shopping or the AFL finals and/or have inflated coverage.
- **Coverage grows through the year** (≈3,700 active devices in January → ≈4,600 in December), so raw counts favour late months; normalised per active device, 2019-10-11 is still 3rd of 250.
- **Thresholds:** Mastic **τ = 1 % → 18 heavy cells** (10–30 cells for τ ≈ 0.8–1.28 %). Mosaic needs much larger τ because a 7×7 square covers a big part of the CBD (max 17–23 % of a window's reports): **δ = 3, τ = 10 % → 2–4 areas per 4-h window**.
- **Encodings:** geohash prefix `r1r` (+21 bits ≈ 0.009 km²), H3 common parent at resolution 5 `85be6357fffffff` (res 10 → 15 bits), Morton box with 0.001° cells **k = 6 / n = 12** for the chosen day (k = 7 / n = 14 for all of 2019 or all bays). **`src/geo.rs` / `src/config.rs` need no change** for Melbourne.

## Files

Created or changed (uncommitted):

| File | What |
|---|---|
| `.gitignore` | ignores `apps/parking/raw/` (raw data and the Parquet cache); re-includes `apps/parking/raw_manifest.json` (the repo ignores `**/*.json`) |
| `apps/parking/requirements.txt` | `duckdb>=1.1` (tested 2.0.0), `h3>=4,<5` (tested 4.5.0); both were already in `.venv` |
| `apps/parking/raw_manifest.json` | name, size, SHA-256, source URL, licence of the three raw files |
| `apps/parking/scripts/hash_raw.py` | writes the manifest; `--check` verifies `raw/` against it |
| `apps/parking/scripts/explore.py` | all P1 statistics (`--step cache, profile, coverage, faults, joins, days, dist, encodings, all`) |
| `apps/parking/reports/01-exploration.md` | this report |
| `apps/parking/reports/01-exploration-output.md` | raw output tables of `explore.py --step all` |

Not for git: `apps/parking/raw/*.csv` (7.45 GB + 3.7 MB + 0.7 MB) and `apps/parking/raw/_cache/sensor_2019.parquet` (776 MB, rebuilt in ≈15 s).

Raw files (in `raw_manifest.json`):

| File | Dataset (City of Melbourne, CC BY 4.0) | Bytes | SHA-256 |
|---|---|---|---|
| `On-street_Car_Parking_Sensor_Data_-_2019.csv` | On-street Car Parking Sensor Data – 2019 | 7,450,489,331 | `9630417bbaba1876a8a95670008f922c56322f0e6f5687a5f36600e05debc60f` |
| `on-street-parking-bays.csv` | On-street Parking Bays | 3,701,640 | `b4ba19115e1cb475f4049f62e465a3e6ca4af903e8971493b4218b935d790bb9` |
| `on-street-parking-bay-sensors.csv` | On-street Parking Bay Sensors (join candidate) | 711,287 | `0f63ee87b3bfe7b076e759a0a9f1bb5110b3607d68cd22c3d2270b6781e4cec3` |

## How to run

PowerShell, from the repo root:

```powershell
.\.venv\Scripts\python.exe -m pip install -r apps\parking\requirements.txt
# raw files: see the table above; save them under apps\parking\raw\ with exactly these names
.\.venv\Scripts\python.exe -I apps\parking\scripts\hash_raw.py           # writes raw_manifest.json
.\.venv\Scripts\python.exe -I apps\parking\scripts\hash_raw.py --check   # verifies raw\ against it
.\.venv\Scripts\python.exe -I apps\parking\scripts\explore.py --step all # ~2.5 min; writes reports\01-exploration-output.md
.\.venv\Scripts\python.exe -I apps\parking\scripts\explore.py --step joins   # any single step
```

The first run of `explore.py` scans the CSV once into `raw/_cache/sensor_2019.parquet` (`--rebuild-cache` to redo it). `-I` keeps the interpreter isolated from the data folders; the script adds `scripts/` to `sys.path` itself to reuse `scripts/check_heavy_hitters.py` (geohash and Morton, identical to `src/geo.rs`).

## Findings

### 1. Sensor file (§1–§4)

**Columns** (CSV header order; DuckDB's sniffed type; missing values):

| Column | Type | Missing |
|---|---|---|
| DeviceId | BIGINT | 0 |
| ArrivalTime | TIMESTAMP (`%m/%d/%Y %I:%M:%S %p`) | 0 |
| DepartureTime | TIMESTAMP | 0 |
| DurationMinutes | BIGINT (11,845 values written as `"1,440"`) | 0 |
| StreetMarker | VARCHAR | 0 |
| SignPlateID | BIGINT | 15,022,691 (35.2 %, no restriction at that time) |
| Sign | VARCHAR | 15,022,691 (35.2 %) |
| AreaName | VARCHAR | 18,229 (0.04 %) |
| StreetId | BIGINT | 0 |
| StreetName | VARCHAR | 0 |
| BetweenStreet1ID / BetweenStreet1 | BIGINT / VARCHAR | 0 |
| BetweenStreet2ID / BetweenStreet2 | BIGINT / VARCHAR | 32,251 (0.08 %) |
| SideOfStreet / SideOfStreetCode / SideName | BIGINT / VARCHAR / VARCHAR | 0 |
| BayId | BIGINT | 0 |
| InViolation | BOOLEAN | 0 |
| VehiclePresent | BOOLEAN | 0 |

The file starts with a UTF-8 BOM. DuckDB's sniffer misses the `"` quoting (it only appears in `"1,440"`), so the script sets `quote='"'` explicitly.

- **Rows:** 42,672,743. **Date range:** arrivals 2019-01-01 00:00:00 – 2019-12-31 23:59:22, departures up to 2019-12-31 23:59:54; no row outside 2019. 166 rows have departure < arrival, 21,046 departure = arrival.
- **`VehiclePresent`:** `true` 22,591,812 (of which `InViolation = true` 1,558,893), `false` 20,080,931.
- **Durations** (`DurationMinutes`, vehicle-present rows): p5 0, p25 1, p50 7, p75 48, p95 200, p99 450, max 468,273; 29.8 % under 2 min; >24 h: 0.00 % of rows (a handful). `DurationMinutes` = ⌊(departure − arrival)/60 s⌋ or one more (rounding up partial minutes).
- **Identifiers:** 6,665 DeviceIds, 5,013 StreetMarkers, 5,013 BayIds (marker ↔ BayId is 1:1), 123 StreetIds, 343 street segments (street + two cross streets; names and IDs agree), 37 areas, 429 sign plates. 1,626 markers had their device replaced during the year (a device never moves to another marker), so **StreetMarker is the stable bay identifier**. Ranges: DeviceId 10036–28532, BayId 567–8826.
- **Timezone: Australia/Melbourne local wall-clock time, no offset.** Evidence (§4): the non-existent hour 2019-10-06 02:00–02:59 (DST start) has only 381 arrivals against ≈2,000 on neighbouring Sundays; the doubled hour 2019-04-07 02:00–02:59 (DST end) has 6,880 against ≈1,500; intervals restart at sign times 07:30, 18:30, 20:30 (1.24 M, 0.82 M, 0.42 M rows), which are local; arrivals peak at 07–18 h. `DurationMinutes` is computed from the same wall-clock strings (difference 0 across both DST changes), so the ~4,600 stays spanning a DST change are off by ±60 min — negligible.

### 2. Split occupancies → stays (§3b)

98.8 % of rows start exactly when the previous row of the same bay ended. 4,896,735 vehicle-present rows (21.7 %) directly follow another vehicle-present row of the same bay; the split times are 00:00:00 (486 k), 18:30 (480 k), 07:30 (433 k), 20:30 (293 k) … . A real departure would leave a vehicle-absent row between, so these are the same car. The script stitches contiguous vehicle-present rows (gap ≤ 1 s, same StreetMarker) into one stay: **22,591,812 rows → 17,695,077 stays**, 3.47 M stays span more than one row (max 133). Stays arriving in 2019 after cleaning (2 min ≤ duration ≤ 24 h): **12,601,568 (71.2 %)**; 28.7 % of stays are under 2 min, 0.13 % over 24 h. Stay duration p50 9 min, p75 54, p95 230, p99 625.

### 3. Coverage and sensor faults (§6, §7)

- **Active DeviceIds per day** (any row that day): min 2,591 (2019-01-27), median 4,170, max 4,619. Monthly medians rise steadily from 3,736 (Jan) to 4,551 (Dec) — the network was expanded, not interrupted. Only 4 days fall below 90 % of the rolling 28-day median (three January weekend/holiday days and 2019-03-24).
- With "normal coverage" = within ±5 % of the regular-weekday median (4,176), **147 of 250 regular weekdays** qualify (essentially mid-May – mid-November); January–early May fall below, mid-November–December above.
- **Stays per (bay, day):** p50 8, p90 25, p99 65, max 693. Your rule (> 40 stays, or ≥ 10 stays with > 50 % under 2 min) flags **92,425 bay-days at 2,555 bays, holding 23.9 % of stays (14.6 % of clean stays)**. By rule alone: > 40 stays 18.3 %, > 60 9.9 %, > 80 6.2 %, > 120 3.3 %; the blip rule 14.7 % of stays (5.9 % of clean stays, since blips are removed by cleaning anyway).
- The flagged set mixes legitimate fast turnover with noise: short-stay bays (P10, ¼P, loading zones; 365 bays) average 22 stays per day, the rest 12. Bays flagged on ~300+ days include P10 and loading-zone bays (plausible) and 2P/disabled bays where 54–88 % of "stays" last under 2 min (sensor noise).
- On the chosen day 8,023 of 38,400 clean located stays (20.9 %) are on flagged bay-days.

### 4. Location joins (§8)

Base: 17,695,077 stays (12,601,568 clean), 6,399 distinct sensor identities. Plausibility: a real ID match should land on a bay of the sensor's own street and near its street segment.

| Method (§3 order) | Stays located | Clean located | Distinct locations | 0.001° cells | Plausible? |
|---|---|---|---|---|---|
| 1a BayId = bays.kerbsideid | 0.06 % | 0.08 % | 4 | 2 | no: 0 % on the sensor's street |
| 1b StreetMarker = bays.kerbsideid | 0.04 % | 0.04 % | 2 | 1 | yes, but only via 2 kerbside IDs that are really street markers (`7568N`, `7570N`) |
| 1c DeviceId = bays.kerbsideid | 4.86 % | 4.93 % | 334 | 73 | no: 5.9 % on the street, 2.0 % within 300 m |
| 1d BayId = sensors.kerbsideid | 0.94 % | 0.87 % | 65 | 15 | no: 0 % within 300 m |
| 1e StreetMarker = sensors.kerbsideid | 0 | 0 | 0 | 0 | – |
| 1f DeviceId = sensors.kerbsideid | 9.83 % | 10.27 % | 593 | 111 | no: 1.0 % within 300 m |
| 2a segment, exact (street + both cross streets) | 84.60 % | 83.11 % | 253 | 235 | yes |
| **2c segment, exact or single-candidate partial** | **89.65 %** | **88.78 %** | **276** | **251** | yes (recommended) |
| 2b segment, exact or any partial (one cross street) | 99.38 % | 99.28 % | 317 | 271 | ambiguous for 1.72 M stays |
| 3 fallback: synthetic on real bays | 100 % | 100 % | 29,053 | 1,610 | synthetic |

- **Bay file:** 29,053 bays (not 23,864 as in the plan), 1,413 road segments, `lastupdated` 2025-06-03 – 2026-03-31; only 5,072 bays have a `kerbsideid` (numeric 6370–94721, plus the two marker-like values); no `marker_id`/`bay_id` field. Descriptions: "X between Y and Z" (28,018 bays), "Intersection of X and Y" (511), "X from Y" (447), other (77). Coordinates: −37.8505 … −37.7756, 144.9006 … 144.9906, mostly 7 decimals; no duplicated coordinates.
- **Bay Sensors feed:** 6,324 current sensors (status timestamps 2022-09 – 2026-11), semicolon-separated, `kerbsideid` 5691–90812 with coordinates. 4,825 of its kerbside IDs appear in the bay file — the two current datasets share one ID space; neither shares it with the 2019 identifiers.
- **Segment join details:** street names are normalised (upper case, punctuation removed, abbreviations spelt out, leading `ST` = Saint) and matched on street + unordered pair of cross streets; the location is the mean of the segment's bay coordinates (2 of 1,237 segment keys span two roadsegmentids and are averaged). Unmatched by 2a are mostly renamed or new streets (Docklands: Collins St between Seafarer Lane and Bourke St; Caravel Lane; City Rd between Kings Way and Power St). Of the partial matches, 893 k stays have exactly one candidate segment (other cross street renamed — kept in 2c), 1.19 M have two and 533 k three or more (2b averages them; 640 k stays end up > 150 m from one of their candidate segments).
- **Concentration (2c, clean stays of 2019):** 276 locations; clean stays per location p50 28,765, p90 87,656, max 345,041 (one segment ≈ 3 % of all located stays); 251 distinct 0.001° cells (per cell p50 31,454). On the chosen day: 253 locations in 232 cells. Each location is a segment mean, so each Mastic cell / Mosaic point receives all cars of 1–3 segments.

**Recommendation: 2c (road segment, exact or single-candidate partial).** It is the only real join that works, locates 89.7 % of stays at segment precision (≈100–200 m, enough for 0.001° cells), and never invents a location between segments. 2b would add 9.7 % of stays at averaged, less accurate locations; it can be the sensitivity variant.

### 5. Day candidates (§9)

Regular weekday = Mon–Fri except the Victorian 2019 public holidays (1 Jan, 28 Jan, 11 Mar, 19 & 22 Apr, 25 Apr, 10 Jun, 27 Sep, 5 Nov, 25 & 26 Dec). Counts = clean stays located by 2c, by arrival date. "Known events" are hard-coded from memory and **unverified**: Australian Open 14–27 Jan, Moomba 8–11 Mar, Grand Prix 14–17 Mar, AFL finals 6–28 Sep, Melbourne Cup week 2–9 Nov, Christmas shopping 1–24 Dec.

Top 5 regular weekdays by count:

| Day | Clean located stays | Active devices (ratio) | Stays / device | Note |
|---|---|---|---|---|
| Thu 2019-12-19 | 38,803 | 4,578 (1.096) | 8.48 | coverage above band; Christmas shopping |
| Fri 2019-12-13 | 38,498 | 4,558 (1.091) | 8.45 | coverage above band; Christmas shopping |
| **Fri 2019-10-11** | **38,400** | 4,341 (1.040) | 8.85 | **eligible** |
| Fri 2019-09-13 | 38,180 | 4,319 (1.034) | 8.84 | AFL finals |
| Thu 2019-09-26 | 38,086 | 4,317 (1.034) | 8.82 | AFL finals (eve of Grand Final Friday) |

The two busiest days coincide with unusually high sensor counts (+9 % devices) and the Christmas period; per active device they rank low (8.45–8.48 vs 8.85). Next eligible days: Fri 2019-08-30 (37,745), Fri 2019-08-02 (37,679; the highest per device, 8.96), Thu 2019-10-10 (37,625), Thu 2019-08-29 (37,214). Regular-weekday median: 33,793.

**Chosen day 2019-10-11 (Friday):**

| Design | Reports |
|---|---|
| A — one per arrival | **38,400** |
| B — one per occupied hour (default; hours of that day of every stay overlapping it) | **79,355** (5,131 from stays that began on 10 Oct) |
| B, unclipped (all hours of the stays arriving that day, also after midnight) | 80,144 |

Design-B reports per arriving stay: 1 → 19,492, 2 → 11,098, 3 → 3,984, 4 → 1,599, ≥ 5 → 2,227. Cars present per hour peak at 11:00 (5,300) and fall to 1,109 at 03:00.

**Bounding box of the located stays:** chosen day lat −37.8312 … −37.7951, lon 144.9369 … 144.9899 (≈ 4.7 km × 4.0 km); all of 2019 lat −37.8341 … −37.7890, lon 144.9239 … 144.9899.

### 6. Count distributions for thresholds (§10, 2019-10-11, design B)

Cell = location rounded to 0.001° (Mosaic's rounding; identical to the Morton cell with the half-off box, §7). Thresholds as in the benchmark: T = max(1, ⌊τN⌋), pass if count ≥ T.

**Mastic** (whole day, N = 79,355 reports in 232 non-empty cells; per cell p50 268, p90 710, p99 1,404, max 1,693 = 2.13 % of N):

| τ | 0.05 % | 0.1 % | 0.25 % | 0.5 % | **1 %** | 2 % | ≥ 3 % |
|---|---|---|---|---|---|---|---|
| heavy cells | 219 | 200 | 144 | 77 | **18** | 2 | 0 |
| share of N in them | 99.7 % | 98.3 % | 88.5 % | 64.6 % | 24.9 % | 4.2 % | 0 |

10 cells: 1.229 % ≤ τ < 1.283 %; 30 cells: 0.804 % ≤ τ < 0.823 %; 20 cells falls on a tie (cells 20 and 21 both hold 782).

**Mosaic** (per 4-h window; centres = every grid point within δ of the data extent; table entry = passing centres / connected areas):

| δ = 3 (7×7) | 00–04 | 04–08 | 08–12 | 12–16 | 16–20 | 20–24 |
|---|---|---|---|---|---|---|
| N_w (reports) | 5,720 | 7,877 | 17,579 | 19,522 | 15,254 | 13,403 |
| max square / N_w | 23.0 % | 18.9 % | 17.0 % | 16.9 % | 17.4 % | 19.3 % |
| τ = 0.1–1 % | 1,026–1,376 / 1–2 | 1,100–1,385 / 1–2 | ≈1,080–1,390 / 1 | ≈1,055–1,380 / 1 | ≈1,050–1,385 / 1 | ≈1,020–1,345 / 1 |
| τ = 2 % | 629 / 5 | 723 / 2 | 764 / 3 | 752 / 3 | 780 / 4 | 691 / 2 |
| τ = 5 % | 294 / 2 | 322 / 3 | 348 / 4 | 338 / 5 | 336 / 4 | 312 / 3 |
| **τ = 10 %** | **152 / 2** | **115 / 2** | **94 / 4** | **104 / 3** | **102 / 2** | **128 / 2** |
| τ = 15 % | 51 / 2 | 17 / 2 | 7 / 1 | 6 / 1 | 15 / 2 | 35 / 2 |
| τ = 20 % | 8 / 1 | 0 | 0 | 0 | 0 | 0 |

With δ = 5 (11×11) a square holds up to 29–33 % of a window; even τ = 20 % leaves 97–205 centres in 1–2 areas, i.e. one blob covering the CBD. δ = 5 is too coarse for this ≈ 4.7 × 4.0 km area.

Why the numbers differ so much from Austin: all events lie in a few km², on only ≈ 250 segment points, so every 7×7 square in the CBD collects thousands of reports. τ below ≈ 5 % makes almost the whole extent "hot".

### 7. Encodings for Melbourne (§11)

Computed over the 276 located points of 2019, the 253 of the chosen day, and all 29,053 bays.

- **Geohash:** common prefix 19 bits (`r1r` + `0000`) for 2019 and for all bays; 24 bits (`r1r0` + `0111`) for the chosen day. The config takes whole base32 characters, so **`prefix = "r1r"`**. Cells of ≈ 0.01 km² need 36 total bits (121 m × 76 m = 0.0092 km²) → **`bits = 21`** (data_bits 21). With `r1r0` (day only) → 16 bits.
- **H3:** one common parent at every resolution 2–5: res 2 `82be67fffffffff`, res 3 `83be63fffffffff`, res 4 `84be635ffffffff`, **res 5 `85be6357fffffff`** (3–7 parents at res 6–7). Resolution 10 cells → data_bits 24 (res-2 prefix) … **15 (res-5 prefix)**.
- **Morton (0.001° cells):** coordinates are segment means / bay positions with 7 decimals, not 3, so data points never sit on a 3-decimal grid line. The box edges are still placed half a cell off the grid ((i − 0.5)/1000): then Morton cell i contains exactly the points that round to i/1000, i.e. Mastic's cells equal Mosaic's grid points. Verified: 0 points dropped and 0 cell ≠ rounded-grid mismatches for the located points (1–3 of 29,053 bays sit exactly on a .0005 edge).

| Points | Extent (lat × lon cells) | k | n = 2k | Box (centred) |
|---|---|---|---|---|
| chosen day | 37 × 54 | **6** | **12** | `{ min_lat = -37.8445, max_lat = -37.7805, min_lon = 144.9315, max_lon = 144.9955, axis_bits = 6 }` |
| all 2019 | 46 × 67 | 7 | 14 | `{ min_lat = -37.8755, max_lat = -37.7475, min_lon = 144.8935, max_lon = 145.0215, axis_bits = 7 }` |
| all bays | 76 × 91 | 7 | 14 | anchored `{ min_lat = -37.8515, max_lat = -37.7235, min_lon = 144.9005, max_lon = 145.0285, axis_bits = 7 }` |

(Anchored variants for day and year are in §11.) The centred 2019 box contains all 29,053 bays; the anchored one 91.6 %.

### 8. Code check: `src/geo.rs`, `src/config.rs` (read-only)

**No code change is needed to configure Melbourne as in §5:**

- `Geohash::point_to_bits` bisects [−90, 90] / [−180, 180]; negative latitudes work. Any base32 prefix is accepted (`Encoding::Geohash { prefix, bits }`); points outside it return `None` and are counted as dropped.
- `H3` takes any prefix cell coarser than the resolution and checks `cell.parent(prefix_res) == prefix`; h3o handles the southern hemisphere.
- `Morton { min_lat, max_lat, min_lon, max_lon, axis_bits }` accepts any finite box with min < max (e.g. −37.8445 < −37.7805) and 1–31 bits per axis; the quantisation `floor((v − min)/(max − min) · 2^k)` is the same in `scripts/check_heavy_hitters.py`, whose `--bbox` also parses negative values (checked).
- `dataset.rs` reads `start_location_lat` / `start_location_long` by header and ignores other columns, so `bay_ref`, `segment`, `join_method`, `hour` can be added to the CSVs.

Limitations and later changes (not needed for §5):

1. **`MosaicGrid`** hard-codes Austin's origin and clamps out-of-grid points to the edge — every Melbourne point would land in one corner cell. Don't use it; §5 already replaces it by Morton.
2. **Geohash prefix granularity:** only whole base32 characters, so the 4 extra shared bits (`r1r` + `0000`) are still sent as tree levels (21 instead of 17). Optional change: accept a bit-level prefix (e.g. a `prefix_bits` field). Cost: 4 cheap top levels.
3. **Weights:** `Weight` has only `Constant` (1 bucket). `HourOfDay` (24, 6, 1 buckets) is P3.
4. **Bench scripts are Austin-specific:** `bench/scripts/bench_common.py` (`ENCODINGS`, `MORTON_BBOX`, `compare_configs(prefix="ride-austin-day")`) and the `MosaicGrid` constants in `scripts/check_heavy_hitters.py`. They need parametrising per dataset in P2/P3.

## Decisions needed

| # | Decision | Recommended default | Why |
|---|---|---|---|
| D1 | Stitch contiguous vehicle-present rows into stays | **Yes**, gap ≤ 1 s, same StreetMarker | 21.7 % of rows are artificial splits at midnight/sign times; without stitching design A double-counts arrivals and design B double-counts hours |
| D2 | Location join | **2c** (segment, exact or single-candidate partial), 89.7 % of stays; 2b (99.4 %) as sensitivity variant; synthetic fallback not needed | only real join that works; no direct ID matches in either current dataset |
| D3 | Look for an older bay dataset with 2019 `marker_id`/`bay_id` (bay-level positions) | **No**, continue with segments | segments (~100–200 m) suffice for 0.001° cells; an archived snapshot may exist but is not in the current portal |
| D4 | Coverage rule for "normal" days | **±5 % of the regular-weekday median of active devices** | removes the Jan–Apr low and Nov–Dec high coverage; 147 of 250 weekdays qualify |
| D5 | Exclude known-event days | **Yes** (list above, to be verified by you) | the raw top days are Christmas/AFL-finals days |
| D6 | Day | **Friday 2019-10-11** (38,400 stays; 79,355 design-B reports) | busiest eligible weekday; 3rd per active device. Alternative: 2019-08-02 (highest per device) |
| D7 | Fault rule and handling in P2 | **Flag only > 80 stays per bay-day or the blip rule; report them but keep them** (cleaning already drops < 2 min stays) | > 40 flags 18 % of stays incl. legitimate P10/¼P/loading-zone bays; dropping bay-days would create artificial occupancy holes |
| D8 | Design-B day boundary | **Clip to the day and include stays that began the day before** (79,355) | gives true occupancy per hour 0–23 of that day |
| D9 | Mastic τ | **1 %** (18 heavy cells); 0.8 % for ≈ 30 | plan target 10–30 cells |
| D10 | Mosaic δ | **δ = 3** (7×7); δ = 5 only as comparison | δ = 5 merges the CBD into one area at any τ that leaves anything |
| D11 | Mosaic τ per 4-h window | **10 %** (2–4 areas, 94–152 centres per window); 15 % as a tighter variant | plan target "a few areas" per window |
| D12 | Morton box | **chosen-day box, centred, k = 6, n = 12**: `min_lat = -37.8445, max_lat = -37.7805, min_lon = 144.9315, max_lon = 144.9955` | covers every report of the day; cheapest tree. Use the k = 7 bay box if capacity tables must cover all bays or the day changes |
| D13 | Geohash | **`prefix = "r1r"`, `bits = 21`** (121 m × 76 m) | valid for every day and all bays; optional bit-level prefix change saves 4 levels |
| D14 | H3 | **resolution 10, prefix res 5 `85be6357fffffff`** (15 bits) | fewest levels; res-2 prefix `82be67fffffffff` (24 bits) if you want the same depth as Austin |
| D15 | Capacity from 2025–26 bay data for 2019 | **Accept, with a caveat in the results** | the only bay inventory available; restrictions and bays have changed since 2019 |
| D16 | Plan text | update §3 (29,053 bays; no ID join; stays) when P2 starts | keeps PARKING_PLAN.md as source of truth |

## What to commit

Raw data and the cache stay out of git (`apps/parking/raw/` is ignored, verified with `git status --ignored`).

```powershell
git add .gitignore `
        apps/parking/PARKING_PLAN.md `
        apps/parking/requirements.txt `
        apps/parking/raw_manifest.json `
        apps/parking/scripts/hash_raw.py `
        apps/parking/scripts/explore.py `
        apps/parking/reports/01-exploration.md `
        apps/parking/reports/01-exploration-output.md
```

Do not add: `apps/parking/raw/` (CSV files, `_cache/`), `apps/parking/scripts/__pycache__/` (already ignored). `apps/parking/data/` is empty and is not tracked by git.
