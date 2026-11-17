# Application: private parking-demand map (drivers as clients)
 
This file is the source of truth for the parking application. It lives at `mastic_for_fuzzy_HH/apps/parking/PARKING_PLAN.md`, on a new branch `parking`.
 
The data is prepared once in the Mastic repo and copied to the Mosaic repo, as for the benchmark.
 
The decisions of P1 (`reports/01-exploration.md`, D1–D16) and of P2 (`reports/02-preparation.md`, D1–D5) are applied below; no open ⟨decide⟩ items remain in §2–§6.
 
---
 
## 1. The idea in one paragraph
 
Drivers' phones report **where and when they parked**. That is private: it reveals where someone lives, works and shops. Today nobody measures it for street parking, apart from a few sensor pilots. With private heavy hitters, the servers learn only the aggregate: the areas where many cars are parked, and at which hours they are full. No individual parking event is revealed.
 
Combined with the public number of parking bays per area, a user can see "this area is usually full 12:00–14:00; free spaces are likely after 17:00". Garage occupancy is already public in many cities (Zürich publishes it in real time), so this application focuses on **street parking**, where the private data is the only source.
 
## 2. What the protocols compute

| | Mastic (weights) | Mosaic (no weights) |
|---|---|---|
| Client | one parked car | one parked car |
| α / point | the stay's location (street-segment mean, §3) → cell (encodings, §5) | the same location → grid point |
| What is counted | **design B, snapshot (default): one report per hour h = 0–23 in which the car is parked at the instant h:30**, with weight = that hour (one-hot over 24 buckets). The bucket counts per cell are the number of cars present at h:30, i.e. occupancy | the same snapshot reports, run **separately per 4-hour window** (6 windows) |
| Output | heavy cells + an **occupancy profile per hour** for each | per time window: the squares with many parked cars within δ |
| Revealed | counts of all evaluated regions (incl. pruned ones) | one yes/no bit per checked region |

Report designs, each written as its own file (P2):

- **Design B, snapshot (default):** a stay produces a report for hour h if arrival ≤ D h:30 < departure on the chosen day D. Stays that began the day before count. A bay reports at most once per hour (**D1**): where overlapping sensor intervals give one bay two stays covering h:30, only the stay that arrived first reports (51 of 40,714 reports removed on the chosen day). So the hourly count per cell never exceeds the number of bays there. The deduplication applies to the snapshot files only; stays, overlap-B and design A are unchanged.
- **Design B, overlap (variant):** one report for every hour of day D that the stay overlaps (a car parked 09:40–11:15 gives hours 9, 10, 11). It counts short visits that miss the :30 instant, but a bay can contribute more than once per hour.
- **Design A (variant): one report per arrival** on day D, weight = arrival hour. This gives *when people arrive* (demand) rather than occupancy.

**Free spaces (post-processing, no privacy needed):** capacity per cell (§4, step 8) − occupancy per cell and hour.

**Time in Mosaic:** one run per 4-hour window, which uses the existing 2D harness unchanged. The Mastic-vs-Mosaic cost comparison runs on the benchmark set as a whole day, without windows (Mosaic τ = 10 %, **D2**). Optional experiment: time as a third coordinate; cost grows quickly with an extra dimension, so time it first.

## 3. Data sources

| Dataset | What it gives | Size | License |
|---|---|---|---|
| City of Melbourne, *On-street Car Parking Sensor Data – 2019* | one row per sensor status interval: arrival, departure, duration, street marker, device, street and cross streets, sign, vehicle present | 42,672,743 rows, 7.45 GB CSV | CC BY 4.0 |
| City of Melbourne, *On-street Parking Bays* | 2025–26 bay locations, road segment, kerbside ID | 29,053 bays | CC BY 4.0 |
| City of Melbourne, *On-street Parking Bay Sensors* | current sensors: kerbside ID with coordinates (tested as a join candidate only) | 6,324 sensors | CC BY 4.0 |

2019 is the last full year before COVID changed traffic. File hashes: `apps/parking/raw_manifest.json`. Exploration: `apps/parking/reports/01-exploration.md` (P1).

**Stays.** The sensor system cuts one occupancy into several vehicle-present rows at midnight and at restriction boundaries (21.7 % of vehicle-present rows). Contiguous vehicle-present rows of the same bay (gap ≤ 1 s) are stitched into one **stay**: 22,591,812 rows → 17,695,077 stays.

**Location join (decided in P1).** No direct ID join works: the 2019 BayId / DeviceId / StreetMarker values match neither the bay file's nor the Bay Sensors feed's kerbside IDs (all overlaps are coincidental). The join is by **road segment**:

- **2c (default):** street + both cross streets match a bay-file segment description exactly, or one cross street differs and exactly one segment qualifies. Location = mean of that segment's bay coordinates (≈100–200 m precision). Locates 89.7 % of stays; 276 locations.
- **2b (sensitivity variant):** also partial matches with several candidate segments, averaged. Locates 99.4 %.
- The synthetic fallback is not needed.

## 4. Data preparation (pipeline)

Scripts in `apps/parking/scripts/` (parameters in `parking_common.py`), outputs in `apps/parking/data/`. DuckDB queries the CSV through a Parquet cache in `apps/parking/raw/_cache/`.

1. **Download** the three CSVs into `apps/parking/raw/` (git-ignored); SHA-256 in `raw_manifest.json` (`hash_raw.py`).
2. **Stays:** stitch contiguous vehicle-present rows per StreetMarker (§3).
3. **Clean:** keep stays with 2 min ≤ duration ≤ 24 h. Timestamps are Melbourne local wall-clock time (P1).
4. **Join** with method 2c (`--join 2b` for the variant). Columns `start_location_lat` / `start_location_long` (segment mean), `segment`, `join_method`, so both harnesses load the files unchanged.
5. **Fault flag:** a bay-day (StreetMarker × arrival date) is flagged if it has more than 80 stays, or at least 10 stays of which more than half last under 2 minutes. Flagged stays are **kept** and marked `fault_flag`; the flag uses the stay's arrival date.
6. **Day: Friday 2019-10-11**, the busiest regular weekday with normal sensor coverage (±5 % of the weekday median of active devices) and no known event. 38,400 clean located stays arrive that day.
7. **Reports** (columns `start_location_lat, start_location_long, hour, stay_id, street_marker, segment, join_method, fault_flag, arrival, departure`):
   - `parking_day_snapshot.csv`: design B, snapshot (default), all reports of the day = **application set**, used with Mastic and for the plaintext analysis;
   - `parking_day_snapshot_wHH-HH.csv`: the same split into the six 4-hour windows (for Mosaic);
   - `parking_day_overlap.csv`: design B, overlap; `parking_day_arrivals.csv`: design A;
   - `join2b/`: the same report files with join 2b.
8. **Benchmark set:** a seeded random sample (seed 20261107) of exactly **21,581 snapshot reports** (the same N as the ride benchmark), split into nested thirds `parking_day_{1of3,2of3,3of3}.csv` by `bench/scripts/make_subsets.py`, with `manifest.json` (rows, SHA-256).
9. **Capacity table** per encoding (`capacity_<encoding>.csv`) and per segment:
   - **primary:** the number of distinct 2019 StreetMarkers active on the chosen day (any sensor row that day), at their segment location: the bays that could produce reports. **D4:** the 573 of 4,327 active markers (13 %) on segments the join cannot place are left out — they produce no reports either, so capacity and occupancy cover the same 3,754 bays;
   - **secondary:** the number of 2025 bay-file bays, at their own coordinates.
10. **Ground truth** (plaintext):
    - **Mastic:** heavy cells per encoding at τ (§6) for the three subsets and the application set, each with its 24 hour-bucket counts;
    - **True occupancy:** per cell and hour (snapshot), straight from the stays, for evaluating the application;
    - **Mosaic:** qualifying squares per time window — computed in the Mosaic repo.
11. **Checks the scripts report:** rows at each step (rows → stays → clean → joined → day → reports); snapshot reports per cell and hour ≤ capacity; that each subset contains the smaller ones and the hashes match the manifest; every report inside the Morton box, the geohash prefix and the H3 prefix; heavy-cell counts; hour buckets summing to each cell's count.
12. **Optional realism (not in P2):** a second version with GPS noise (about 15 m, normal distribution).

## 5. Encodings for Melbourne

The Austin parameters don't transfer: prefixes, origins and boxes are Austin-specific, and Melbourne's latitudes are negative. The Mastic encoders (`src/geo.rs`) handle Melbourne without code changes (P1).

| Encoding | Melbourne setting | data_bits |
|---|---|---|
| **grid = Morton with 0.001° cells** | `{ min_lat = -37.8445, max_lat = -37.7805, min_lon = 144.9315, max_lon = 144.9955, axis_bits = 6 }`: the chosen day's extent, centred, edges half a cell off the 3-decimal grid (cell i = points rounding to i/1000, as Mosaic's grid) | **12** (k = 6, 64 cells = 0.064° per axis) |
| **geohash** | `prefix = "r1r"`, `bits = 21` (121 m × 76 m ≈ 0.009 km²) | 21 |
| **H3** | resolution 10, `prefix = "85be6357fffffff"` (the common resolution-5 parent) | 15 |

That makes 3 encodings, not 4: in Melbourne, "Mosaic grid" (origin hard-coded to Austin) and Morton are the same thing.

- **Mosaic:** δ = 3 cells (7×7 square, about ±300 m). δ = 5 merges the CBD into one area (P1), so it is not used.
- **Mastic:** `hist_buckets` = 24 for the application (P3: `weight.hour_of_day = { buckets = 24 }`; 6 and 1 buckets are also supported). Driver configs `src/configs/parking-day-{morton,geohash,h3}.toml`, generated by `parking_common.py --write-configs`. They use `add_report_share_batch_size = 250`: a 24-bucket report share is ~4–10 KB, so batches of 1000 would exceed tarpc's 8 MiB frame.

## 6. Thresholds

Chosen from the data (P1, re-checked on the snapshot data in P2), not copied from the ride benchmark. T = max(1, ⌊τN⌋), a cell or square qualifies if its count ≥ T.

- **Mastic:** τ = 1 % on the application set and the benchmark subsets (P1: 18 heavy cells on overlap-B; P2 snapshot: 17–30 per encoding and set; target 10–30).
- **Mosaic:** τ = 10 % per 4-hour window, δ = 3 (P1: 2–4 areas per window; P2 snapshot: 2–3). A 7×7 square covers a large part of the ≈ 4.7 × 4.0 km sensor area, so Mosaic's τ is much larger than Mastic's.
- **Benchmark set (whole day, no windows):** Mastic τ = 1 %; **Mosaic τ = 10 %** (**D2**; δ = 3: 96 qualifying centres in 3 areas on `parking_day_3of3.csv`).
- **D3:** both thresholds were re-checked on the snapshot data in P2 and kept: Mastic τ = 1 % is the only tested value giving 10–30 heavy cells for every encoding (Morton 21, geohash 17, H3 28 on the application set); Mosaic τ = 10 % gives 2–3 areas in every 4-hour window (`reports/02-preparation.md`, `data/thresholds.json`).

## 7. Experiments
 
1. **Application result:**
   - a map of the heavy areas;
   - each area's occupancy profile over 24 hours;
   - estimated free spaces per hour (capacity − occupancy);
   - compared with the true occupancy from the sensor data. Report what share of all car-hours lies in reported cells, and the free-space error for the reported areas.
2. ~~**Cost of weights (Mastic):** 1 vs 6 vs 24 buckets. Measure client report size, client time and online time. This is the cost of richer statistics.~~ **Dropped:** no benchmarking in this application (P3 scope).
3. ~~**Mastic vs Mosaic** on the 21,581-report set: time, rounds, bytes and leakage, as in the ride benchmark. Also compare ride data with parking data at the same N.~~ **Dropped:** no benchmarking in this application (P3 scope).
4. **Exact vs fuzzy:** Mosaic's areas per time window versus Mastic's heavy cells for the same window.
5. **Optional:** GPS-noise version; time as a third coordinate in Mosaic.
## 8. Implementation steps
 
| Step | Repo | What |
|---|---|---|
| P1 | Mastic, branch `parking` | **Exploration only:** download, inspect columns, test the joins, statistics, day candidates, count distributions. No pipeline yet. → report |
| P2 | Mastic | **Preparation pipeline** (§4), capacity tables, Melbourne encoding configs (§5), ground truth |
| P3 | Mastic | `Weight::HourOfDay` in `dataset.rs` (24 buckets; also 6 and 1), with tests; bucket counts in the driver's `--export`; `scripts/run_app.py` runs the app set per encoding and checks it against the ground truth. **Scope (P3): the parking app is implemented, not benchmarked** |
| P4 | Mosaic | Melbourne encodings (check whether Mosaic's encoders accept a custom origin, box and prefix); time-window runs; reuse the harness |
| P5 | both | Runs (Mastic: minutes; Mosaic: one night), evaluation, comparison |
| P6 | Mastic | Frontend: map with hour slider, occupancy %, estimated free spaces. `apps/parking/frontend/index.html` + `data.js` (built by `scripts/build_frontend.py` from the saved results; the page never starts runs). A "Where to park" view (report 05) adds the cells with space per hour, from the last-level counts revealed during the search; it never uses the ground truth. Report 06: every cell with hourly counts is coloured by % full (green < 70 %, red bands 70/80/90 %), hotspots by a thick outline, best bets split into quiet and busy areas, street names from `data/cell_streets_<enc>.csv` |
 
The rules from the benchmark spec (C0) apply to every prompt:
- no git commits, pushes or branch switches;
- measurement and application code only, without changing the protocols;
- PowerShell-compatible scripts;
- a report in `apps/parking/reports/NN-*.md`, with an exact `git add` list.