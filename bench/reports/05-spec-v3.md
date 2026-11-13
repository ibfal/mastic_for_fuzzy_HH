# 05: Spec v3 (τ per system, area-based accuracy)

## Summary

The harness now follows spec v3 (A2, A5, A10).
- **τ is set per system:** Mastic 0.005, Mosaic 0.02. `threshold_abs(n, tau)` has no default τ
  any more.
- **Summary key:** `evaluate.py` keys every summary row by system × encoding × subset × τ.
- **Reference:** it reads only the A5 reference format, checks it, and computes `area_recall`,
  `precision`, `f1` and `areas_found` on the Mosaic grid. The count error is gone.
- **New `--tau` options:** `ground_truth.py` and `run_bench.py`. Nothing was benchmarked.

At τ = 0.02, Mastic's ground truth is empty for all four encodings in all three subsets. The
busiest cell has 214 rides (231 for H3), and T is 431.

I re-evaluated the existing 72 runs. All 1,536 values the old and new summaries share are
identical, including every timing, byte, round and leakage value.

I tested the reference path against a synthetic A5 reference in the scratchpad. All 12 rows equal
an independent brute-force computation, and two rows also match my hand calculation.

## Files

Modified:
- `bench/scripts/bench_common.py`:
  - `THRESHOLDS = {"mastic": 0.005, "mosaic": 0.02}`. `THRESHOLD_FRAC` is Mastic's τ.
  - `REFERENCE_TAU = 0.02` and `REFERENCE_DELTA = 5`.
  - `threshold_abs(n, tau)`: τ is required.
  - `ground_truth_path(dataset, subset, tau)` names the ground truth for a τ.
- `bench/scripts/evaluate.py`:
  - **Columns:** `threshold_frac` is a column and part of the row key. The accuracy columns are
    `area_recall`, `precision`, `f1`, `areas_found` (median/min/max each), plus
    `reference_areas` and `reference_squares`. `count_error` and `reference_hotspots` are gone.
  - **Reader:** the old permissive reader is replaced by a strict A5 parser
    (`parse_reference`, `connected_areas`).
  - **Self-check:** each run is checked against the ground truth at its own τ.
  - **Outputs:** `tables.md` has a τ column, and section 5 is rewritten. Plot 5 has one row of
    panels per τ.
  - **Several τ:** in the plots, higher τ values get dashed lines and hollow markers.
    `--combine` shows one column per system × τ and checks the parameters per τ.
  - **New options:** `--reference-dir` and `--out-dir`.
- `bench/scripts/ground_truth.py`: `--tau` (default 0.005). For τ ≠ 0.005 it writes
  `day_<subset>_tau<τ>.json`. The console shows the busiest cell per encoding.
- `bench/scripts/run_bench.py`:
  - `--tau` (default 0.005) goes into the driver config, and the driver records it in
    `params.threshold_frac`. The runner checks it after each run.
  - It needs the ground truth at that τ. If the file is missing, it stops with a clear error
    (exit 1).
  - Configs and logs for τ ≠ 0.005 get a `-tau<τ>` suffix. `--smoke --tau` is refused.
- `bench/tests/test_evaluate.py`: the old reference tests are replaced. 33 tests (22 before).
- `bench/README.md`: commands and descriptions for `--tau`, the A5 reference, several τ,
  `--reference-dir` and `--out-dir`.
- `bench/results/summary_mastic.csv`, `tables.md`, `plots/5_accuracy.png/.pdf`: regenerated.
  Plots 1–4 and 6 came out byte-identical.

Created:
- `bench/ground_truth/day_1of3_tau0.02.json`, `day_2of3_tau0.02.json`, `day_3of3_tau0.02.json`:
  Mastic's ground truth at τ = 0.02. Every encoding has 0 heavy cells.
- `bench/reports/05-spec-v3.md`: this report.

Not touched: the protocol and driver code, the raw results, the τ = 0.005 ground truth (it
regenerates byte for byte), `BENCHMARK_MASTIC.md`, `.gitignore`. All test inputs and outputs of
step 6 are in the session scratchpad. `bench/reference/` was never created.

## How to run

```powershell
.\.venv\Scripts\python.exe -m pytest bench\tests -p no:cacheprovider
.\.venv\Scripts\python.exe bench\scripts\ground_truth.py              # tau = 0.005 (unchanged files)
.\.venv\Scripts\python.exe bench\scripts\ground_truth.py --tau 0.02   # Mastic at Mosaic's tau
.\.venv\Scripts\python.exe bench\scripts\evaluate.py
# once the Mosaic harness has written bench\reference\day_<subset>.json (A5 format):
.\.venv\Scripts\python.exe bench\scripts\evaluate.py
.\.venv\Scripts\python.exe bench\scripts\evaluate.py --combine bench\results\summary_mosaic.csv
# test a reference without touching bench\results\:
.\.venv\Scripts\python.exe bench\scripts\evaluate.py --reference-dir <dir> --out-dir <dir2>
# another tau for the runner (not needed by the spec; dry run only prints the plan):
.\.venv\Scripts\python.exe bench\scripts\run_bench.py --tau 0.02 --dry-run
```

## Checks

| Check | Numbers | Status |
|---|---|---|
| Tests | 33 of 33 pass | pass |
| Two-area reference (test) | Areas: 3 squares, one joined only at a corner; and 2 squares. Locations: 2 hit area 0, 1 misses → area recall 1/2, P 2/3, F1 4/7. Adding one location exactly 5 from area 1 → R 1, P 3/4, F1 6/7 | pass |
| δ boundary (test) | L∞ 5 hits, including (5, −5) at Euclidean 7.07; L∞ 6 misses. With δ = 4 in the file, (5, −5) misses | pass |
| Diagonal rule (test) | Squares touching at a corner form one area; a chain of corners is one area; a one-square gap makes two areas. A file that splits corner neighbours, or merges squares two apart, is rejected | pass |
| A5 format only (test + run) | Rejected with exit code 2, a message naming the problem and nothing written: the old list/`hotspots` formats, missing or extra fields, degrees instead of grid squares, count < T, a duplicate square, an unknown area, a wrong size or peak, the wrong subset, non-JSON | pass |
| Existing results stay valid | All 72 lines pass the A9 check; T = max(1, ⌊τ·N⌋) in all 72 | pass |
| Summary unchanged | 12 rows, 135 → 137 columns. Added: `threshold_frac`, `area_recall_*`, `areas_found_*`, `reference_areas`, `reference_squares`. Removed: `recall_*`, `count_error_*`, `reference_hotspots`. Shared values compared: 408 timing, 420 bytes, 108 rounds, 216 leakage, 384 other: **0 differ** | pass |
| Self-check (A5) | 72 of 72 runs, against the τ = 0.005 ground truth | pass |
| B3 sanity checks | 60 of 60 | pass |
| Deterministic | rerun → all 14 output files byte-identical | pass |
| Ground truth τ = 0.005 | regenerated: byte-identical to the committed files (smoke file too) | pass |
| Ground truth τ = 0.02 | T = 143 / 287 / 431. 0 heavy cells for every encoding and subset. Busiest cell: 71 / 135 / 214 rides for `mosaic_grid`, `geohash` and `morton`; 73 / 137 / 231 for `h3` | as expected: geohash and H3 also give nothing |
| `run_bench.py --tau` | Default dry run: configs byte-identical to the existing ones. `--tau 0.02 --dry-run`: T 143 / 287 / 431, config threshold 0.02. Missing ground truth: clean error, exit 1. `--smoke --tau`: usage error | pass |
| `--combine` with several τ | A fake second system with rows at τ 0.02 and 0.005 (online × 2 and × 3) gives one column per system × τ and ratios 2.00 / 3.00. The parameter check reports consistent; 12 comparison plots | pass |

**Reference path, step 6.** The synthetic reference is in the scratchpad, not in `bench/reference/`.
It uses the A5 format with T = 143 / 287 / 431 and δ = 5:
- **A:** a 3 × 3 block, squares 999–1001 × 999–1001.
- **B:** (930, 1070) and (931, 1071). They touch only at a corner, so they form one area.
- **C:** (500, 500), far from every output. Only in 2/3 and 3/3, so 1/3 has 2 areas and the
  others 3.

The "hand" columns come from an independent script: its own grid formula, brute-force L∞ over
every square, nothing imported from `evaluate.py`.

| Encoding | Subset | Hand: areas found | R | P | F1 | evaluate.py: areas found | R | P | F1 |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `mosaic_grid` | 1/3 | 2 / 2 | 1.000 | 4/7 = 0.571 | 0.727 | 2 / 2 | 1.000 | 0.571 | 0.727 |
| `mosaic_grid` | 2/3 | 2 / 3 | 0.667 | 5/8 = 0.625 | 0.645 | 2 / 3 | 0.667 | 0.625 | 0.645 |
| `mosaic_grid` | 3/3 | 2 / 3 | 0.667 | 5/8 = 0.625 | 0.645 | 2 / 3 | 0.667 | 0.625 | 0.645 |
| `geohash` | 1/3 | 1 / 2 | 0.500 | 6/13 = 0.462 | 0.480 | 1 / 2 | 0.500 | 0.462 | 0.480 |
| `geohash` | 2/3 | 1 / 3 | 0.333 | 4/10 = 0.400 | 0.364 | 1 / 3 | 0.333 | 0.400 | 0.364 |
| `geohash` | 3/3 | 1 / 3 | 0.333 | 4/10 = 0.400 | 0.364 | 1 / 3 | 0.333 | 0.400 | 0.364 |
| `h3` | 1/3 | 2 / 2 | 1.000 | 7/16 = 0.438 | 0.609 | 2 / 2 | 1.000 | 0.438 | 0.609 |
| `h3` | 2/3 | 2 / 3 | 0.667 | 8/14 = 0.571 | 0.615 | 2 / 3 | 0.667 | 0.571 | 0.615 |
| `h3` | 3/3 | 2 / 3 | 0.667 | 9/15 = 0.600 | 0.632 | 2 / 3 | 0.667 | 0.600 | 0.632 |
| `morton` | 1/3 | 2 / 2 | 1.000 | 4/7 = 0.571 | 0.727 | 2 / 2 | 1.000 | 0.571 | 0.727 |
| `morton` | 2/3 | 2 / 3 | 0.667 | 5/8 = 0.625 | 0.645 | 2 / 3 | 0.667 | 0.625 | 0.645 |
| `morton` | 3/3 | 2 / 3 | 0.667 | 5/8 = 0.625 | 0.645 | 2 / 3 | 0.667 | 0.625 | 0.645 |

Two rows worked by hand:
- **`mosaic_grid` 3/3.** Hits:
  - (1003, 994), (994, 1006) and (1000, 998) are within δ of A. The first two are exactly at L∞ 5.
  - (935, 1076) and (936, 1076) are within δ of B, both exactly at L∞ 5 from (931, 1071).

  Misses: (1003, 993), (991, 1004) and (1003, 1007).

  So P = 5/8, R = 2/3 (C missed), F1 = 2·(5/8)·(2/3) / (5/8 + 2/3) = 20/31 = 0.645.
- **`geohash` 1/3.** Six of 13 locations are within δ of A. The airport cells' centres sit at
  lon_grid 1077, which is L∞ 6 from B, so B is missed. P = 6/13, R = 1/2,
  F1 = 2·(6/13)·(1/2) / (6/13 + 1/2) = 12/25 = 0.48.

## Deviations and open questions

1. **Strict A5 reader (please confirm).** The reader rejects extra fields as well as missing ones,
   so the Mosaic harness must write exactly the A5 keys:
   - top level: `dataset`, `subset`, `tau`, `T`, `delta`, `squares`, `areas`;
   - each square: `grid`, `count`, `area`;
   - each area: `id`, `peak`, `peak_count`, `size`.

   If you'd rather allow extras such as `N` or `source`, it is a one-line change.
2. **Consistency checks beyond the format (please confirm).** The reader also stops if the file
   contradicts A5:
   - a count < T;
   - an area's `size` differs from its number of squares;
   - `peak` is not a highest-count square of its area (ties are accepted);
   - the areas are not exactly the 8-connected groups of Q.
3. **τ, δ or T that differ from A5 are notes, not errors.** Notes go to the console and
   `tables.md`. The matching uses the file's δ, and the `delta` summary column shows it. Say if
   you want these to stop the script instead.
4. **Ground truth file names.** τ = 0.005 keeps `day_<subset>.json`, so nothing existing changes.
   Other τ values get `day_<subset>_tau<τ>.json`. The τ = 0.02 files are empty. Commit them as a
   record of the A2 statement, or leave them out; nothing needs them.
5. **Summary columns changed.** `threshold_frac` is column 5, and there are 137 columns. The Mosaic
   harness must copy the new `SUMMARY_COLUMNS`. `--combine` refuses an old summary and names the
   missing `threshold_frac`.
6. **Ratios in `comparison.md` (decision).** With several τ, there is one column per system × τ.
   Ratios are against the first column (Mastic, τ = 0.005). A ratio between rows at different τ
   compares answers to different questions (A2), so it is only indicative. Alternative: show
   ratios only between rows at the same τ.
7. **A9 v3.1 is not in the schema check (question).** `bench_common.validate_result` still
   requires:
   - a numeric `server_to_server`;
   - an integer `count`;
   - numeric `lat`/`lon`;
   - no `extra`.

   Mastic's lines and `--combine` (which reads summaries) don't need the change. `outputs` already
   follows v3.1: outputs without coordinates are not counted. Should I extend the schema check
   too?
8. **Additions beyond the prompt:**
   - `evaluate.py --reference-dir` and `--out-dir`, so that step 6 could run without touching
     `bench/reference/` or `bench/results/`;
   - the `bench/README.md` update.
9. **Note for reading the real reference.** Grid cells and Morton cells are Mosaic squares.
   Geohash and H3 centres can land one square away: at the airport, geohash is at lon_grid 1077
   while the grid is at 1076. Near the δ boundary, the encodings can therefore differ in area
   recall. This follows from A5's "cell centre → grid" rule; it is not a bug.
10. **`bench/.run/` (ignored by Git).** The default dry run rewrote the existing configs with
    identical content. I deleted the four τ = 0.02 configs that my test dry run created.

## What to commit

```
git add bench/scripts/bench_common.py bench/scripts/evaluate.py bench/scripts/ground_truth.py bench/scripts/run_bench.py bench/tests/test_evaluate.py bench/README.md bench/results/summary_mastic.csv bench/results/tables.md bench/results/plots/5_accuracy.png bench/results/plots/5_accuracy.pdf bench/ground_truth/day_1of3_tau0.02.json bench/ground_truth/day_2of3_tau0.02.json bench/ground_truth/day_3of3_tau0.02.json bench/reports/05-spec-v3.md
```

Commit message: `Spec v3: tau per system, A5 area-based accuracy, tau in the summary row key`
