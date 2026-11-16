# Application: private parking-demand map (drivers as clients)
 
This file is the source of truth for the parking application. It lives at `mastic_for_fuzzy_HH/apps/parking/PARKING_PLAN.md`, on a new branch `parking`.
 
The data is prepared once in the Mastic repo and copied to the Mosaic repo, as for the benchmark.
 
Items marked **⟨decide⟩** are open decisions. Each one has a recommended default.
 
---
 
## 1. The idea in one paragraph
 
Drivers' phones report **where and when they parked**. That is private: it reveals where someone lives, works and shops. Today nobody measures it for street parking, apart from a few sensor pilots. With private heavy hitters, the servers learn only the aggregate: the areas where many cars are parked, and at which hours they are full. No individual parking event is revealed.
 
Combined with the public number of parking bays per area, a user can see "this area is usually full 12:00–14:00; free spaces are likely after 17:00". Garage occupancy is already public in many cities (Zürich publishes it in real time), so this application focuses on **street parking**, where the private data is the only source.
 
## 2. What the protocols compute
 
| | Mastic (weights) | Mosaic (no weights) |
|---|---|---|
| Client | one parked car | one parked car |
| α / point | the bay's location → cell (encodings, §5) | the bay's location → grid point |
| What is counted | **design B (recommended): one report per hour the car was parked**, with weight = that hour (one-hot over 24 buckets). The bucket counts per cell are then the number of cars present per hour, i.e. occupancy | the same occupied-hour reports, run **separately per time window** (e.g. 6 windows of 4 h) |
| Output | heavy cells + an **occupancy profile per hour** for each | per time window: the squares with many parked cars within δ |
| Revealed | counts of all evaluated regions (incl. pruned ones) | one yes/no bit per checked region |
 
**Design A (simpler, optional): one report per arrival**, weight = arrival hour. This gives *when people arrive* (demand) rather than occupancy.
 
**Free spaces (post-processing, no privacy needed):** capacity per cell = number of bays in the cell, from the public bay dataset. Estimated free spaces per cell and hour = capacity − occupancy.
 
**Time in Mosaic ⟨decide⟩:** the default is one run per time window, which uses the existing 2D harness unchanged. Optional experiment: time as a third coordinate ("nearby in space *and* time"). Cost grows quickly with an extra dimension, as H3's third coordinate showed, so time it first.
 
## 3. Data sources
 
| Dataset | What it gives | Size | License |
|---|---|---|---|
| City of Melbourne, *On-street Car Parking Sensor Data – 2019* | one row per sensor status interval: arrival time, departure time, duration, the bay's street marker/device, street and cross streets, restriction sign, whether a vehicle was present | 42.7 M rows, 717 MB compressed CSV | CC BY 4.0 |
| City of Melbourne, *On-street Parking Bays* | bay locations (latitude/longitude), road segment, kerbside ID | 23,864 bays | CC BY 4.0 |
| (optional) *On-street Car Park Bay Restrictions* | time limits per bay | – | CC BY 4.0 |
 
2019 is chosen because it is the last full year before COVID changed traffic. The exact column names must be confirmed from the files (prompt P1).
 
**The main risk is the location join.** The sensor events identify bays by street marker / device ID. The current bay dataset lists `kerbsideid` and road segments, and its description mentions `marker_id`/`bay_id` without listing them as fields. Prompt P1 checks which join works, in this order:
 
1. **Direct ID join:** the sensor marker or ID matches a field in the bay dataset (or in an older version of it).
2. **Road-segment join:** street + "between street 1 and street 2" in the sensor data is matched to the bay dataset's road segment description. The location is the mean of that segment's bay coordinates, about 100–200 m precision. That is enough for cells of about 100 m.
3. **Fallback, synthetic on real geometry:** generate events on the 23,864 real bay locations, with hourly patterns taken from the 2019 data per street segment.
## 4. Data preparation (pipeline)
 
All scripts live in `apps/parking/scripts/` and write to `apps/parking/data/`. Use DuckDB (`pip install duckdb`) to query the 717 MB CSV directly, without loading it into memory.
 
1. **Download** the sensor CSV and the bay CSV into `apps/parking/raw/` (git-ignored, too large). Record their SHA-256 in `raw_manifest.json`.
2. **Clean the events:**
   - keep only intervals where a vehicle was present;
   - drop rows with missing or invalid times, durations under 2 minutes (sensor blips), or durations over 24 hours;
   - keep events inside the city boundary;
   - use local time (Melbourne).
3. **Join locations** (method from P1). Add `start_location_lat` / `start_location_long` (the bay or segment location), so both harnesses load the file unchanged. Also add `bay_ref`, `segment`, `join_method`.
4. **Choose the day** ⟨decide⟩. Default: the **busiest regular weekday of 2019** (most events; no public holiday). Report the 5 busiest weekdays with their event counts so we can choose.
5. **Build the reports:**
   - **Design B, default:** one row per (event, hour the car was present), with `hour` 0–23. A car parked 09:40–11:15 gives rows for hours 9, 10 and 11.
   - **Design A:** one row per event, `hour` = arrival hour.
6. **Size for the benchmark.** Mosaic needs about 5.5 minutes per 21,581 clients on your laptop, and its cost grows with N. So:
   - **Benchmark set:** a seeded random sample of exactly **21,581 reports** (the same N as the ride benchmark, so the two applications are directly comparable), split into nested thirds as before (seed 20261107, SHA-256 manifest). File: `parking_day_{1of3,2of3,3of3}.csv`.
   - **Application set:** all reports of the chosen day, used with Mastic (cheap) and for the plaintext analysis.
7. **Optional realism ⟨decide⟩:** a second version with GPS noise added (about 15 m, normal distribution), because a phone doesn't know the exact bay. Default: exact bay locations first, the noisy version as an extra.
8. **Capacity table:** the number of bays per cell, for each encoding, from the bay dataset. File `capacity_<encoding>.csv`.
9. **Ground truth** (plaintext), per encoding, subset and threshold:
   - **Mastic:** heavy cells with their 24 bucket counts.
   - **Mosaic:** qualifying squares per time window.
   - **True occupancy:** per cell and hour, straight from the sensor data, for evaluating the application.
10. **Checks the scripts must report:**
    - rows at each step (raw → cleaned → joined → day → reports);
    - the share of events that couldn't be located;
    - the bounding box of the locations;
    - the reports-per-event distribution;
    - that each subset contains the smaller ones;
    - that the hashes match the manifest.
## 5. Encodings for Melbourne
 
The Austin parameters don't transfer, because the prefixes, origins and boxes are all Austin-specific. Melbourne is also in the southern hemisphere, so latitudes are negative.
 
| Encoding | Melbourne setting | Notes |
|---|---|---|
| **grid = Morton with 0.001° cells** | box from the data extent, half a cell off the coordinate grid, 2^k cells per axis | Replaces "Mosaic grid", whose origin is hard-coded to Austin. The CBD sensor area is a few km wide, so probably k = 6 (64 cells, 0.064°) → **n = 12 levels**, much cheaper than Austin's 20–22 |
| **geohash** | prefix = the longest common geohash prefix of all locations, plus enough bits for about 0.01 km² cells | compute the prefix from the data |
| **H3** | resolution 10, prefix = the common resolution-2 (or finer) parent | compute from the data |
 
That makes 3 encodings, not 4: in Melbourne, "Mosaic grid" and Morton are the same thing.
 
- **Mosaic:** δ ⟨decide⟩. Default **δ = 3 cells** (about ±300 m, a short walk), with δ = 5 as a comparison. Threshold ⟨decide⟩ after exploration (§6).
- **Mastic:** `hist_buckets` = 24 for the application. For the cost benchmark, also run 1 and 6 buckets.
## 6. Thresholds ⟨decide after P1⟩
 
Choose them from the data, not by copying the ride values:
 
- **Mastic:** τ such that the full day gives about 10–30 heavy cells.
- **Mosaic:** τ per time window, such that each window gives a few areas.
P1 reports the distribution of counts per cell and per neighbourhood, which shows which τ gives how many hotspots.
 
## 7. Experiments
 
1. **Application result:**
   - a map of the heavy areas;
   - each area's occupancy profile over 24 hours;
   - estimated free spaces per hour (capacity − occupancy);
   - compared with the true occupancy from the sensor data. Report what share of all car-hours lies in reported cells, and the free-space error for the reported areas.
2. **Cost of weights (Mastic):** 1 vs 6 vs 24 buckets. Measure client report size, client time and online time. This is the cost of richer statistics.
3. **Mastic vs Mosaic** on the 21,581-report set: time, rounds, bytes and leakage, as in the ride benchmark. Also compare ride data with parking data at the same N.
4. **Exact vs fuzzy:** Mosaic's areas per time window versus Mastic's heavy cells for the same window.
5. **Optional:** GPS-noise version; time as a third coordinate in Mosaic.
## 8. Implementation steps
 
| Step | Repo | What |
|---|---|---|
| P1 | Mastic, branch `parking` | **Exploration only:** download, inspect columns, test the joins, statistics, day candidates, count distributions. No pipeline yet. → report |
| P2 | Mastic | **Preparation pipeline** (§4), capacity tables, Melbourne encoding configs (§5), ground truth |
| P3 | Mastic | `Weight::HourOfDay` in `dataset.rs` (24 buckets; also 6 and 1), with tests; runner and evaluation support for the parking dataset and for the bucket counts in the output |
| P4 | Mosaic | Melbourne encodings (check whether Mosaic's encoders accept a custom origin, box and prefix); time-window runs; reuse the harness |
| P5 | both | Runs (Mastic: minutes; Mosaic: one night), evaluation, comparison |
| P6 | Mastic | Frontend: map with hour slider, occupancy %, estimated free spaces |
 
The rules from the benchmark spec (C0) apply to every prompt:
- no git commits, pushes or branch switches;
- measurement and application code only, without changing the protocols;
- PowerShell-compatible scripts;
- a report in `apps/parking/reports/NN-*.md`, with an exact `git add` list.