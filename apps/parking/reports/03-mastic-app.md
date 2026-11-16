# P3: Mastic hour-of-day occupancy for the parking app

Prompt P3 of [`PARKING_PLAN.md`](../PARKING_PLAN.md) (§8), with the revised scope: **the parking app is implemented, not benchmarked**. Branch `parking`. Nothing is committed. The protocol code (`vidpf.rs`, `prg.rs`, `collect.rs`, `server.rs`, `rpc.rs`) and the benchmark scripts are unchanged.

## Summary

- **New weight, `Weight::HourOfDay`.** Each report adds a one-hot vector over hour buckets to β. Mastic therefore returns each heavy cell together with its count per hour. The protocol already supports this through `hist_buckets`.
- **The app uses 24 buckets.** Any divisor of 24 works, for example 6 four-hour blocks or 1 bucket.
- **The `hour` column is optional.** Files without it (RideAustin, ten_locations) still load with `Weight::Constant`.
- **Driver output fix (E7).** The driver now filters printed cells on the counter, not on the last hour bucket, so no heavy cell is hidden.
- **Export.** The driver's `--export` JSON now carries each heavy cell's 24 hour counts and a `params` block (encoding, τ, T, N, buckets, weight). `build_viz.py` keeps working unchanged.
- **Configs.** Three Melbourne driver configs (Morton, geohash, H3; τ = 1 %, 24 buckets) are generated from `parking_common.py`.
- **`run_app.py`.** The new script runs all three encodings on the application set and checks the results.
- **Result:** all three match the plaintext ground truth exactly:
  - Morton: 21 heavy cells;
  - geohash: 17;
  - H3: 28;
  - every count and every one of the 24 hour counts is identical.
- **Cost:** each run takes 35–40 s, and each server peaks at about 4.5–4.7 GB.
- **Tests and Austin:** `cargo test` passes: 48 tests, 9 of them new. The Austin smoke run is unchanged.

## Files

| File | Change | Why |
|---|---|---|
| `src/config.rs` | modified | `Weight::HourOfDay { buckets }`, which also derives `Serialize` and `PartialEq` |
| `src/dataset.rs` | modified | optional `hour` column, `Weight::validate`, `bucket()` returning `Result`, `hour_bucket()`, `load_inputs` checks every row; 7 unit tests |
| `src/bin/driver.rs` | modified | E7 print filter on the counter; export adds `heavy_hitters[].buckets` and `params`; prints `- Weight: …` |
| `src/bin/keygen_bench.rs` | modified | compile fix only: validates the weight and checks every row's bucket before timing |
| `tests/hour_weight_test.rs` | new | in-process crawl at 24 and 6 buckets: each heavy cell's histogram = its hour profile (2 tests) |
| `src/configs/parking-day-{morton,geohash,h3}.toml` | new (generated) | Melbourne driver configs |
| `apps/parking/scripts/parking_common.py` | modified | `HIST_BUCKETS`, `UPLOAD_BATCH`, `SERVERS`, `config_dict`, config writer, `--write-configs` / `--check-configs` |
| `apps/parking/scripts/run_app.py` | new | build, run each encoding with `--export`, check against the ground truth |
| `apps/parking/results/mastic_{morton,geohash,h3}.json` | new | the driver's exports: the app's Mastic output, for the frontend |
| `apps/parking/results/check.json` | new | per-encoding check results, run time and peak memory |
| `.gitignore` | modified | `!apps/parking/results/*.json`, so the results aren't caught by `**/*.json`; `apps/parking/.run/` (run configs and logs) is ignored |
| `apps/parking/PARKING_PLAN.md` | modified | §5: config names and batch size; §8: P3 row with the new scope |
| `apps/parking/reports/03-mastic-app.md` | new | this report |

### Weight and config syntax

```toml
[dataset]
path = "apps/parking/data/parking_day_snapshot.csv"
encoding.morton = { min_lat = -37.8445, max_lat = -37.7805, min_lon = 144.9315, max_lon = 144.9955, axis_bits = 6 }
weight.hour_of_day = { buckets = 24 }   # bucket = hour * buckets / 24; buckets must divide 24
```

- `weight = "constant"`, or no weight line, works as before.
- `num_buckets` must still equal `hist_buckets`.
- With `hour_of_day`, a row is an error if its `hour` is missing, empty, or outside 0–23. The error names the file and the row. This also applies to rows outside the encoded area.

### What changed in the `--export` JSON (and for build_viz.py)

The changes only add keys. `format` stays `"mastic-crawl/1"`, and every existing key is unchanged.

- `heavy_hitters[].buckets`: the cell's 24 hour counts, which is `value` without the counter. `count` is still the counter, and `value` still holds the buckets followed by the counter.
- `params`: `{encoding, encoding_params, data_bits, tau, T, N, hist_buckets, weight}`, the run's parameters in one place. The older top-level keys (`encoding`, `threshold_fraction`, `threshold`, `num_clients`, `input_len`) stay.
- `levels[].candidates[].value` now has 25 entries, where Austin had 2.

**build_viz.py and viz/index.html need no change.**
- build_viz checks `format` and reads only `dataset` and `encoding`.
- The viewer reads `levels[].candidates[].{bits, count, heavy, region}` and `heavy_hitters[].bits`, all of which are unchanged.
- The E7 fix only touches the printed lines. The export's heavy hitters always came from the crawl's keep flags.

The frontend (P6) can read the hour profile from `heavy_hitters[].buckets`.

### Upload batch size

The parking configs use `add_report_share_batch_size = 250`, where Austin uses 1000. A 24-bucket report share is about 4–10 KB per server; geohash is the largest at about 10 KB. A batch of 1000 would exceed tarpc's 8 MiB frame. This setting only changes how the driver sends batches, not the protocol.

## How to run

PowerShell, from the repo root:

```powershell
cargo test
.\.venv\Scripts\python.exe -I apps\parking\scripts\parking_common.py --write-configs   # regenerate src/configs/parking-day-*.toml
.\.venv\Scripts\python.exe -I apps\parking\scripts\parking_common.py --check-configs   # exit 1 if they differ
.\.venv\Scripts\python.exe -I apps\parking\scripts\run_app.py                          # build + 3 encodings, ~2 min
.\.venv\Scripts\python.exe -I apps\parking\scripts\run_app.py --only-encoding h3 --no-build
.\.venv\Scripts\python.exe -I apps\parking\scripts\run_app.py --check-only             # re-check the saved results
```

`run_app.py` does the following for each encoding:
1. Picks two free ports from 18100 (never 8000).
2. Writes the run config to `apps\parking\.run\configs\`.
3. Starts both servers with ⌊physical cores / 2⌋ threads each (5 on this laptop).
4. Runs the driver with `-n 40663 --export apps\parking\results\mastic_<encoding>.json`.
5. Stops the servers, also on failure and on Ctrl+C. Logs go to `apps\parking\.run\logs\`.

Then it checks the export and writes `results\check.json`. Its exit code is 1 if any check fails.

Manual run of one config, with the servers in two other terminals:

```powershell
.\target\release\server.exe --config src\configs\parking-day-morton.toml --server_id 0
.\target\release\server.exe --config src\configs\parking-day-morton.toml --server_id 1
.\target\release\driver.exe --config src\configs\parking-day-morton.toml -n 40663 --export morton.json
```

## Checks

### 1. `cargo test`: pass

48 tests pass: 39 old and 9 new. The release build is clean; the only warnings were already there (`vidpf.rs` lifetimes, deprecated `zipf`).

| Suite | Tests | New |
|---|---|---|
| lib (bench, lib, vidpf, **dataset**) | 14 | 7: hour mapping for 24 / 6 / 1; `bucket()` incl. missing, 24, −1; bucket counts must divide 24; ten_locations loads with Constant (165 rides) and fails with HourOfDay at row 2; a CSV with `hour` loads with 24 / 6 / Constant buckets (one row outside the box dropped); missing, empty, 24 and −1 hours are errors with the row number, also outside the area; TOML syntax (`weight.hour_of_day = {…}`, inline table, `"constant"`, omitted, invalid) |
| `hour_weight_test` | 2 | 2: in-process crawl (both aggregators) at 24 and 6 buckets. The 3 heavy cells' reconstructed `value[..B]` equal their hour profiles and `value[B]` their counts. One cell has no hour-23 report |
| `encoding_pipeline_test` | 7 | – (`every_dataset_config_matches_its_encoder` now also checks the 3 parking configs: data_bits, weight vs hist_buckets, CSV exists) |
| `geo_test`, `collect_test`, `dpf_test`, `flp_test` | 21 / 2 / 1 / 1 | – |

### 2. Configs: pass

`--write-configs` wrote the 3 files, and `--check-configs` reports all 3 as equal. Each file reads back as its `config_dict`.

### 3. Austin smoke run: pass, unchanged

`run_bench.py --smoke` (`ten_locations.csv`, threshold 0.1) found **50 / 40 / 30 / 20** for all 4 encodings, and the self-check is P 1.000, R 1.000, exact.
- Online rounds are 49 / 47 / 53 / 45 (mosaic_grid / geohash / h3 / morton), the same as report 02.
- Report sizes per server are 1,706 / 1,637 / 1,844 / 1,568 B on the wire, also the same as report 02.
- The FLP check accepts the generated reports.

### 4. `run_app.py`, all three encodings on the app set: pass, exact

App set: `parking_day_snapshot.csv`, N = 40,663, 0 dropped, τ = 1 %, T = 406. Its SHA-256 matches the ground truth's source.

| Encoding | Heavy cells (truth) | Counts | 24 hour counts | Max centre diff | Printed lines | Run (s) | Peak memory server 0 / 1 / driver (MiB) | Export |
|---|---|---|---|---|---|---|---|---|
| Morton | **21** (21) | exact | exact | 3 × 10⁻¹⁴° | 21 | 35.3 | 4,721 / 4,720 / 726 | 113 KB |
| geohash | **17** (17) | exact | exact | 0° | 17 | 39.7 | 4,668 / 4,099 / 1,217 | 145 KB |
| H3 | **28** (28) | exact | exact | 3 × 10⁻¹⁴° | 28 | 38.0 | 4,516 / 4,518 / 875 | 288 KB |

For every heavy cell:
- the labels and bit strings are equal;
- the 24 hour counts sum to the count;
- the `params` block equals the ground truth's encoding parameters.

`--check-only` on the saved files also passes. The separate runs per encoding beforehand gave the same results, at 34–39 s.

### 5. E7 (no heavy cell hidden): pass

- **The app data can't show the bug.** No heavy cell of any encoding has 0 cars at 23:30; the smallest hour count is 2. So on this data the old filter would also have printed every cell.
- **End-to-end check instead.** I made a synthetic Melbourne file of 55 reports with 3 heavy locations, one of them without any hour-23 report. It ran through the real servers and driver with the Morton config at threshold 0.1:
  - the driver printed **3 of 3** cells, including `morton (29, 33)` with `buckets[23] = 0`, which the old filter `value[23] > 0` would have hidden;
  - the export has the same 3 cells.
- The in-process test in `hour_weight_test` covers the same case.

### 6. build_viz compatibility: pass

I imported `build_viz` and ran its format check and `encoding_kind` on the three exports. All three are accepted and indexed as `morton` / `geohash` / `h3`, and every candidate has the keys the viewer reads.

One detail: one H3 last-level candidate (`…111`, H3 digit 7, which doesn't exist; count 0, not heavy) has an empty `region`. That is existing `geo.rs` behaviour for non-cells, and the viewer already handles it. All heavy cells have outlines.

I didn't run build_viz itself, because it writes into `viz/`. To view a parking run, copy an export into `viz\results\` and run `python scripts\build_viz.py`.

## Deviations and open questions

1. **PARKING_PLAN §7 still lists experiment 2 (cost of weights: 1 vs 6 vs 24 buckets) and experiment 3 (Mastic vs Mosaic on parking data).** Under the new scope these are benchmarks. Should I mark them as dropped? I only updated §5 and the P3 row in §8.
2. **Memory:** about 4.7 GB per server at 24 buckets on N = 40,663, so the whole run needs about 10 GB. For comparison, an Austin `day_3of3` run (N = 21,581, 1 bucket) uses about 0.6 GB per server. That fits this 15.8 GB laptop, but larger sets (for example the overlap design, 79,355 reports) would not. This matters for P6 only if the frontend is meant to trigger runs itself.
3. **Upload batch of 250** for the parking configs (see Files). It is recorded in PARKING_PLAN §5.
4. **E7 on real data:** see check 5. The fix is verified on synthetic data, because the app data has no heavy cell with an empty last hour.
5. **The results JSON** (546 KB in total) is committed so the frontend can read it. The `commit` field in `check.json` is the current HEAD with `dirty: true`, because the runs used this uncommitted code.

## What to commit

`apps/parking/.run/` (run configs and logs) is git-ignored. No `__pycache__` was written.

```powershell
git add .gitignore `
        src/config.rs `
        src/dataset.rs `
        src/bin/driver.rs `
        src/bin/keygen_bench.rs `
        tests/hour_weight_test.rs `
        src/configs/parking-day-morton.toml `
        src/configs/parking-day-geohash.toml `
        src/configs/parking-day-h3.toml `
        apps/parking/PARKING_PLAN.md `
        apps/parking/scripts/parking_common.py `
        apps/parking/scripts/run_app.py `
        apps/parking/results/mastic_morton.json `
        apps/parking/results/mastic_geohash.json `
        apps/parking/results/mastic_h3.json `
        apps/parking/results/check.json `
        apps/parking/reports/03-mastic-app.md
```

Commit message: `P3: Weight::HourOfDay, hour profiles in the driver export, Melbourne configs and run_app.py`
