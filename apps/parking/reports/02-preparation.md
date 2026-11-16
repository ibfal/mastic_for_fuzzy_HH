# P2 — Parking data preparation

Prompt P2 of [`PARKING_PLAN.md`](../PARKING_PLAN.md) (§8). Branch `parking`; nothing committed, no Rust code changed; `bench/scripts/*.py` and `scripts/check_heavy_hitters.py` are imported only (unchanged).

This is the final version, after decisions D1–D5 (section "Decisions").

## Summary

- **PARKING_PLAN.md §2–§6 updated (D16)** to the P1 decisions plus your changes, and to this prompt's decisions D1–D4:
  - design B default = **snapshot**: one report per hour h if the stay covers D h:30, at most one per bay and hour;
  - primary capacity = the 2019 StreetMarkers active on the day.
- **Pipeline built** (`prepare.py`). It reuses P1's stitching, cleaning and join code, so **every P1 count is reproduced exactly**: 42,672,743 rows → 17,695,077 stays → 12,601,568 clean → 11,188,093 joined (2c) → 38,400 design A / 79,355 overlap-B on Fri 2019-10-11.
- **Snapshot-B: 40,663 reports** from 19,903 stays at 253 locations (3,574 bays). That's 40,714 minus 51 second reports of a bay in the same hour, removed per D1. Peak 2,577 cars at 11:30, minimum 730 at 04:30.
- **Benchmark set:** a seeded sample of exactly 21,581 snapshot reports → `parking_day_{1of3,2of3,3of3}.csv` (7,194 / 14,387 / 21,581 rows) via the unchanged `bench/scripts/make_subsets.py`, which also writes `manifest.json`. Nested and hashed; reruns are byte-identical.
- **Application set:** `parking_day_snapshot.csv` plus six 4-hour window files. Design A and overlap-B are separate files. Join 2b variant in `data/join2b/` (46,471 snapshot reports).
- **Capacity:** 4,327 markers active on the day, 3,754 located, in 234 Morton / 237 geohash / 193 H3 cells. The 573 markers on segments the join can't place are left out (D4). The 2025 bays are a secondary column.
- **Ground truth at τ = 1 %:** Morton 21, geohash 17, H3 28 heavy cells on the app set (23 / 19 / 28 on 3of3). Each cell has 24 hour buckets that sum to its count. True occupancy from the stays equals the snapshot file exactly (40,663 car-hours).
- **Thresholds confirmed on snapshot data:** Mastic τ = 1 %; Mosaic δ = 3, τ = 10 % per window (2–3 areas) and τ = 10 % on the whole-day benchmark set (96 centres, 3 areas).
- **All 43 checks pass** (`check_preparation.py`, exit code 0).

## Files

New or changed (uncommitted):

| File | What |
|---|---|
| `.gitignore` (changed) | `!apps/parking/data/**/*.json`: manifests, thresholds and ground truth are data |
| `apps/parking/PARKING_PLAN.md` (changed) | §2–§6 rewritten to the decisions (P1 D1–D16, P2 D1–D4) |
| `apps/parking/scripts/parking_common.py` | all Melbourne parameters (day, seed, N, join, cleaning, fault rule, windows, Mastic τ, Mosaic δ / τ per window / τ for the benchmark set, encodings), the P1 reference counts, file paths; reuses `bench_common` helpers and the `check_heavy_hitters` encoders |
| `apps/parking/scripts/prepare.py` | cache → stays → clean → join (`--join 2c|2b`) → day → report files (snapshot deduplicated per bay and hour), benchmark sample + `make_subsets.py`, `prepare_manifest.json` |
| `apps/parking/scripts/capacity.py` | capacity per segment and per cell of each encoding |
| `apps/parking/scripts/ground_truth.py` | (a) Mastic heavy cells with 24 hour buckets; (b) true occupancy per cell and hour from the stays (each bay counted once per hour) |
| `apps/parking/scripts/thresholds.py` | threshold re-check on the snapshot data |
| `apps/parking/scripts/check_preparation.py` | all checks; exits 1 if any fails |
| `apps/parking/data/.gitattributes` | `*.csv -text` (as `bench/data/`), so the hashes survive `core.autocrlf=true` |
| `apps/parking/data/…` | 48 files, 52.5 MB (table below) |
| `apps/parking/reports/02-preparation.md` | this report |

Data files (`apps/parking/data/`, LF line endings, no BOM). Report columns: `start_location_lat, start_location_long, hour, stay_id, street_marker, segment, join_method, fault_flag, arrival, departure`.

| File | Rows | Content |
|---|---|---|
| `parking_day_snapshot.csv` | 40,663 | **design B snapshot, application set** |
| `parking_day_snapshot_w00-04.csv` … `_w20-24.csv` | 3,527 / 3,866 / 8,289 / 9,215 / 7,510 / 8,256 | the same per 4-hour window (Mosaic) |
| `parking_day_overlap.csv` | 79,355 | design B overlap |
| `parking_day_arrivals.csv` | 38,400 | design A |
| `parking_day_sample.csv` | 21,581 | seeded sample of snapshot reports (source of the subsets) |
| `parking_day_1of3.csv`, `_2of3.csv`, `_3of3.csv` | 7,194 / 14,387 / 21,581 | **benchmark set**, nested thirds |
| `manifest.json` | – | `make_subsets.py`'s manifest (seed, method, rows, SHA-256) |
| `prepare_manifest.json` | – | definitions, step counts, rows and SHA-256 of every report file |
| `capacity_segment.csv`, `capacity_{morton,geohash,h3}.csv`, `capacity_summary.json` | 255 / 1,347 / 1,685 / 1,028 | `markers_2019` (primary), `bays_2025` (secondary) |
| `ground_truth/mastic_{1of3,2of3,3of3,app}.json` | – | heavy cells at τ = 1 % per encoding, with `hours[24]`, `markers_2019`, `bays_2025` |
| `ground_truth/occupancy_{morton,geohash,h3}.csv` | 5,616 / 5,688 / 4,632 | bays occupied at h:30 per cell and hour (`occupied`, `occupied_all`), capacity, `free_markers` |
| `thresholds.json` | – | the threshold tables below |
| `join2b/…` (19 files) | – | the report files, capacity and app-set ground truth for join 2b |

Encoding parameters (`parking_common.ENCODINGS`):

- Morton `{min_lat = -37.8445, max_lat = -37.7805, min_lon = 144.9315, max_lon = 144.9955, axis_bits = 6}`, data_bits 12;
- geohash `prefix = "r1r"`, `bits = 21`;
- H3 resolution 10, `prefix = "85be6357fffffff"`, data_bits 15.

## How to run

PowerShell, repo root, after the raw files of P1 are in `apps\parking\raw\` (the Parquet cache is rebuilt automatically if missing). In this order:

```powershell
.\.venv\Scripts\python.exe -I apps\parking\scripts\prepare.py              # ~40 s; join 2c, all outputs incl. benchmark set
.\.venv\Scripts\python.exe -I apps\parking\scripts\prepare.py --join 2b    # ~70 s; data\join2b\
.\.venv\Scripts\python.exe -I apps\parking\scripts\capacity.py
.\.venv\Scripts\python.exe -I apps\parking\scripts\capacity.py --join 2b
.\.venv\Scripts\python.exe -I apps\parking\scripts\ground_truth.py         # --tau to add another threshold
.\.venv\Scripts\python.exe -I apps\parking\scripts\ground_truth.py --join 2b
.\.venv\Scripts\python.exe -I apps\parking\scripts\thresholds.py
.\.venv\Scripts\python.exe -I apps\parking\scripts\check_preparation.py    # 43 checks; exit 0 = all pass
```

`capacity.py` needs the joins, `ground_truth.py` needs capacity and the subsets, `check_preparation.py` needs everything. Parameters live only in `parking_common.py`.

## Checks

`check_preparation.py`: **43 of 43 checks pass** (exit code 0).

### 1. Rows at every step (join 2c)

| Step | Rows |
|---|---|
| sensor rows | 42,672,743 (= P1) |
| vehicle-present rows | 22,591,812 (= P1) |
| stays (stitched) | 17,695,077 (= P1) |
| stays arriving in 2019 | 17,695,077 (= P1) |
| clean stays (2 min – 24 h) | 12,601,568 (= P1) |
| clean stays joined (2c) | 11,188,093 (= P1) |
| clean joined stays arriving on 2019-10-11 | 38,400 (= P1) |
| clean joined stays overlapping the day | 39,491 (4,255 fault-flagged) |
| snapshot reports before deduplication | 40,714 |
| second reports of a bay in the same hour removed (D1) | 51 |
| **design B snapshot reports** | **40,663** (2,284 fault-flagged; 37,188 exact segment matches, 3,475 single-candidate partial) |
| design B overlap reports | 79,355 (= P1) |
| design A reports | 38,400 (= P1) |
| benchmark sample / thirds | 21,581 / 7,194, 14,387, 21,581 |

Join 2b: 12,510,585 joined stays (= P1's 2b), 42,970 design A, 89,725 overlap-B, 46,522 → **46,471** snapshot-B (51 removed).

### 2. Snapshot reports per cell and hour ≤ capacity (2019 markers)

| Check | 2c | 2b |
|---|---|---|
| per Morton cell-hour | pass (4,910 cell-hours, max utilisation 1.00) | pass (5,360) |
| per geohash cell-hour | pass (5,030, max 1.00) | pass (5,466) |
| per H3 cell-hour | pass (4,127, max 1.00) | pass (4,416) |
| per segment-hour | pass (5,302) | pass (6,139) |
| at most one report per (bay, hour) | pass | pass |

Before D1, 51 reports at 14 bays were a bay's second report in an hour. They came from overlapping sensor intervals (P1: 37,887 rows start before the previous row of the bay ends). One of them exceeded capacity: segment 23033, bay `13705W`, 10:30, 18 > 17. Some cells still reach utilisation 1.00, i.e. every active bay is occupied at h:30.

### 3. Subsets and hashes

- `make_subsets.py` (unchanged) confirms:
  - the sample's rows re-serialise byte for byte;
  - each subset contains the smaller ones;
  - SHA-256 and row counts match `manifest.json`.

| File | Rows | SHA-256 |
|---|---|---|
| `parking_day_1of3.csv` | 7,194 | `2166fac70b5f1f224b183bd79d4f57a449dfc8dcfdc6a0001cdf092fbcb69fbe` |
| `parking_day_2of3.csv` | 14,387 | `0af3844cec7a6cd7f13925839b1e9e11af203dcd8cfc905257f351b5a29cc869` |
| `parking_day_3of3.csv` | 21,581 | `87e3b38f2b4aefc7f68e0151d52389261b51efd520ec1dcda9ca8433767b3fb2` |
| `parking_day_sample.csv` (source) | 21,581 | `ba33c127a3f603423316fdd70515fb3f023bc2fd5a6f31202d131b7c921d5706` |

- 3of3 holds exactly the sample's rows, and every sampled row is a snapshot report.
- All 13 (2c) and 9 (2b) report files match `prepare_manifest.json`.
- **Determinism:** after a second full `prepare.py` run, all 13 files and `manifest.json` are byte-identical.
- Method: `random.Random(20261107).sample(range(40663), 21581)` kept in file order (sorted by stay_id, hour), then `make_subsets.py --seed 20261107` (shuffle, first ⌊N/3⌉, ⌊2N/3⌉, N rows).
- These hashes replace those of the first P2 run (pre-D1), which were never committed.

### 4. Every report inside the encodings

0 dropped for Morton, geohash and H3:

- all 253 distinct locations in the 13 report files (2c);
- all 292 distinct locations in the 9 report files (2b).

Capacity: every located 2019 marker lies inside all three areas. The Morton day box leaves out 4,406 of the 29,053 2025 bays, which affects only the secondary column; geohash and H3 contain all of them.

### 5. Mastic heavy cells at τ = 1 % (T = max(1, ⌊τN⌋), count ≥ T)

| Set | N | T | Morton | geohash | H3 |
|---|---|---|---|---|---|
| 1of3 | 7,194 | 71 | 21 | 20 | 30 |
| 2of3 | 14,387 | 143 | 23 | 19 | 29 |
| 3of3 | 21,581 | 215 | 23 | 19 | 28 |
| app | 40,663 | 406 | **21** | **17** | **28** |
| app, join 2b | 46,471 | 464 | 22 | 17 | 25 |

Heavy cells hold 28 % (Morton), 23 % (geohash) and 37 % (H3) of the app set's reports. The busiest Morton cell, morton (35, 27), has 807 car-hours over the day for 62 markers.

### 6. Hour buckets and occupancy

- In every ground-truth file the 24 hour buckets of each heavy cell sum to its count.
- True occupancy, computed straight from the stays in DuckDB (distinct bays per cell and hour), equals the snapshot file aggregated per cell and hour for all three encodings (40,663 car-hours; 2b: 46,471).
- `occupied_all`, which also counts stays the cleaning rule drops, totals 42,887.

### 7. Window files

The six window files partition the snapshot file exactly (2c and 2b).

### Threshold re-check (`thresholds.py`, snapshot data)

**Mastic** heavy cells (app set, N = 40,663 / 3of3, N = 21,581):

| Encoding | 0.5 % | 0.75 % | **1 %** | 1.25 % | 1.5 % |
|---|---|---|---|---|---|
| Morton | 71 / 76 | 41 / 40 | **21 / 23** | 10 / 11 | 4 / 4 |
| geohash | 73 / 78 | 39 / 38 | **17 / 19** | 8 / 9 | 4 / 4 |
| H3 | 82 / 85 | 51 / 49 | **28 / 28** | 12 / 13 | 6 / 6 |

Only τ = 1 % gives 10–30 cells for every encoding on both sets.

**Mosaic**, δ = 3, per 4-hour window of the app set (passing centres / connected areas):

| Window | N_w | max 7×7 / N_w | 5 % | 7.5 % | **10 %** | 12.5 % | 15 % |
|---|---|---|---|---|---|---|---|
| 00–04 | 3,527 | 17.7 % | 301 / 2 | 200 / 2 | **126 / 2** | 55 / 2 | 21 / 3 |
| 04–08 | 3,866 | 16.5 % | 321 / 4 | 164 / 3 | **88 / 2** | 36 / 2 | 6 / 1 |
| 08–12 | 8,289 | 15.8 % | 345 / 4 | 182 / 3 | **90 / 2** | 27 / 4 | 4 / 1 |
| 12–16 | 9,215 | 15.8 % | 343 / 4 | 179 / 2 | **98 / 2** | 38 / 4 | 4 / 2 |
| 16–20 | 7,510 | 15.3 % | 342 / 4 | 186 / 2 | **91 / 3** | 33 / 3 | 4 / 1 |
| 20–24 | 8,256 | 18.2 % | 318 / 3 | 187 / 3 | **116 / 2** | 60 / 3 | 27 / 2 |

**Mosaic**, δ = 3, `parking_day_3of3.csv` as a whole day (N = 21,581, max 7×7 = 16.3 % of N):

| τ | 2 % | 5 % | 7.5 % | **10 %** | 15 % |
|---|---|---|---|---|---|
| passing centres / areas | 781 / 5 | 338 / 3 | 172 / 2 | **96 / 3** | 6 / 1 |

Every τ from 5 % to 15 % gives "a few" areas, because a 7×7 square covers a large part of the small sensor area; τ decides how large the reported areas are. τ = 10 % reports a compact core of 88–126 grid points per window. 15 % is unstable between windows (4–27 points), and 5 % covers about a third of the extent.

## Decisions

Taken after the first P2 run and applied in this version:

| # | Decision | Applied as |
|---|---|---|
| D1 | Overlapping sensor intervals | Snapshot files deduplicated per (bay, hour), keeping the stay that arrived first: 40,714 → 40,663 (2b: 46,522 → 46,471). Stays, overlap-B and design A unchanged. `ground_truth.py` counts distinct bays per cell and hour, so occupancy and reports agree. Sample, subsets and hashes regenerated. |
| D2 | Mosaic τ for the whole-day benchmark set | **10 %** (`parking_common.TAU_MOSAIC_BENCH`; 96 centres, 3 areas on 3of3). Recorded in PARKING_PLAN.md §2 and §6. |
| D3 | Thresholds on snapshot data | **Mastic 1 %, Mosaic 10 % per window** (unchanged). Recorded in PARKING_PLAN.md §6. |
| D4 | 573 active markers whose segment the join can't place | **Left out** of capacity (they produce no reports either); noted in PARKING_PLAN.md §4 step 9 and `capacity_summary.json` (`markers_unlocated`). |
| D5 | Data in git | `apps/parking/data/` committed as normal files (52.5 MB, largest file 9.3 MB), no Git LFS. |

Also applied from your earlier decisions:

- all of `data/` including `join2b/`;
- fault flag on the stay's arrival date: bay-days with > 80 stays, or ≥ 10 stays of which more than half last under 2 min; kept, `fault_flag = 1`;
- secondary capacity at bay coordinates;
- Melbourne driver configs (TOML, `hist_buckets = 24`, `Weight::HourOfDay`) in P3.

**Decisions needed:** none for P2. Next is P3 (`Weight::HourOfDay`, the Melbourne driver configs and runner support).

## What to commit

Raw data and the Parquet cache stay git-ignored (`apps/parking/raw/`); `apps/parking/scripts/__pycache__/` is ignored. Verified with `git status --ignored`.

```powershell
git add .gitignore `
        apps/parking/PARKING_PLAN.md `
        apps/parking/scripts/parking_common.py `
        apps/parking/scripts/prepare.py `
        apps/parking/scripts/capacity.py `
        apps/parking/scripts/ground_truth.py `
        apps/parking/scripts/thresholds.py `
        apps/parking/scripts/check_preparation.py `
        apps/parking/data `
        apps/parking/reports/02-preparation.md
```

`apps/parking/data` adds exactly these 48 files:

- `.gitattributes`;
- 17 CSV and 4 JSON files at the top level;
- 7 files in `ground_truth/`;
- 19 files in `join2b/`, including its `ground_truth/`.

`.gitattributes` keeps the CSVs byte for byte, so the hashes above hold after checkout.
