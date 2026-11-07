# 01: Benchmark harness

## Summary

This prompt adds the shared benchmark harness in `bench/`.

- **Subsets:** the day file is split into three nested subsets (7,194 / 14,387 / 21,581 rides) with a
  seeded shuffle. A manifest records their SHA-256 hashes, so the Mosaic codebase can check it has
  the same rides.
- **Ground truth:** the exact heavy cells are computed for each encoding and subset, using the
  encoders from `scripts/check_heavy_hitters.py`.
- **Common helpers:** a module holds the experiment's parameters, records the machine environment,
  checks result lines against the A9 schema, and appends them to JSONL files.

Every check below passes. Rust code and the protocol are unchanged.

**Main deviation from the spec (your decision):** geohash and H3 use the area-matched parameters of
`src/configs` (21 bits, resolution 10), not A2's 20 bits and resolution 9. A2 and B3 need updating
in both benchmark files.

## Files

Created:
- `bench/scripts/bench_common.py`: the experiment's parameters (`ENCODINGS`, threshold, seed, δ) in
  one place; the environment record (A8); the A9 schema check; JSONL append; the config comparison.
- `bench/scripts/make_subsets.py`: the A1 subsets and `manifest.json`.
- `bench/scripts/ground_truth.py`: exact heavy cells per encoding, for the subsets or any smoke-test
  CSV; also the grid sanity check.
- `bench/requirements.txt`: `psutil`, for physical cores and RAM.
- `bench/README.md`: the exact commands.
- `bench/reports/01-harness.md`: this report.
- `bench/data/day_1of3.csv`, `day_2of3.csv`, `day_3of3.csv`, `manifest.json`: the subsets and their
  manifest.
- `bench/data/.gitattributes`: `*.csv -text`, so Git never changes the subsets' line endings and the
  hashes stay valid.
- `bench/ground_truth/day_1of3.json`, `day_2of3.json`, `day_3of3.json`, `smoke_ten_locations.json`:
  the ground truth.

Modified:
- `scripts/check_heavy_hitters.py`: the four encoder classes gain `label(bits)` and `center(bits)`,
  and `describe()` is built from them. This lets the ground truth import centres and labels instead
  of re-implementing them. The checker's output is byte-identical before and after (12 of 12 runs).
- `.gitignore`: adds `!bench/**/*.json`. Without it, the existing `**/*.json` rule hides the manifest
  and the ground truth from `git add`.

Not touched: `src/`, `src/configs/`, and your uncommitted change to `README.md`. `psutil` was
installed into `.venv`, which isn't a repo file.

## How to run

```powershell
.\.venv\Scripts\python.exe -m pip install -r scripts\requirements.txt -r bench\requirements.txt
.\.venv\Scripts\python.exe bench\scripts\make_subsets.py --source data\mosaic\sample_busiest_day.csv --dataset day --seed 20261107
.\.venv\Scripts\python.exe bench\scripts\ground_truth.py
.\.venv\Scripts\python.exe bench\scripts\ground_truth.py --csv data\synthetic\ten_locations.csv --threshold 0.1
.\.venv\Scripts\python.exe bench\scripts\ground_truth.py --check-grid
.\.venv\Scripts\python.exe bench\scripts\bench_common.py --self-test
.\.venv\Scripts\python.exe bench\scripts\bench_common.py
```

## Checks

**Orientation (step 1)**

| Check | Result | Status |
|---|---|---|
| Branch | `benchmarking`, at `1e61df7`, equal to origin; no git writes | pass |
| Rows in `data/mosaic/sample_busiest_day.csv` | 21,581; 0 with missing or non-numeric coordinates | pass |
| δ in `data/mosaic/results.json` | 5, L∞, 11-bit grid. That file is Mosaic's week run (59,040 points, threshold 1180) | pass (see Deviation 3) |
| Threshold in `src/configs/ride-austin-day-*.toml` | τ = 0.005 in all of them, so T = 35 / 71 / 107 | pass |
| Encoding parameters vs A2 | grid and Morton equal A2. Geohash is 21 bits and H3 res 10 in the configs, vs 20 / 9 in A2 | **deviation**: the harness follows the configs (your decision) |
| `ENCODINGS` vs `src/configs/ride-austin-day-*.toml` | all four equal | pass |
| `CLAUDE.md`, `.claude/` | no `CLAUDE.md`; `.claude/settings.local.json` only holds permissions for rustc/cargo | n/a |

**Harness**

| Check | Result | Status |
|---|---|---|
| Subset rows (manifest) | 7,194 / 14,387 / 21,581; 0 dropped | pass |
| Subsets nested; rows unchanged | each contains the smaller ones; re-serialising all rows reproduces the LF-normalised source byte for byte | pass |
| Subset files | LF only; SHA-256 matches the manifest; line counts match | pass |
| Smoke ground truth, `ten_locations.csv`, threshold 0.1 | 4 cells with counts 50/40/30/20 for every encoding; T = 16 | pass |
| `day_3of3` vs the unshuffled day file | identical heavy cells and counts for all four encodings | pass |
| Morton vs grid | identical cells (grid coordinates) and counts in every subset | pass |
| Grid sanity check | 21,581 of 21,581 points equal `start_busiest_day.json`, in file order | pass |
| Schema self-test | both A9 examples valid; all 7 broken copies rejected | pass |
| Environment record | i7-1255U, 10 physical / 12 logical cores, 15.8 GB, Windows 11, rustc 1.99.1, threads 5, commit `1e61df7`, `dirty: true` | pass (see Deviation 8) |
| Checker refactor | output identical, 12 of 12 runs | pass |

**Heavy cells per subset** (threshold 0.005, no rides dropped):

| Encoding | n | `day_1of3` (T = 35) | `day_2of3` (T = 71) | `day_3of3` (T = 107) |
|---|---|---|---|---|
| `mosaic_grid` | 22 | 7 | 8 | 8 |
| `geohash` (21 bits) | 21 | 13 | 10 | 10 |
| `h3` (res 10) | 24 | 16 | 14 | 15 |
| `morton` | 20 | 7 | 8 | 8 |

**B7 checklist status:**
- done: manifest; ground-truth side of the smoke test; schema check ready.
- prompt 2: Mastic smoke runs, key sizes and rounds, self-check, timing run.
- pending your A2 update: encoding parameters vs A2.

## Deviations and open questions

1. **Geohash and H3 parameters (your decision).** The harness uses geohash 21 bits (n = 21) and H3
   resolution 10 (n = 24). Part A has to change in both files at once, so I didn't edit the spec.
   Replacement text for **both** `BENCHMARK_MASTIC.md` and `BENCHMARK_MOSAIC.md`:
   - A2, `geohash` row: "prefix `9v6`, 21 bits, n = 21"
   - A2, `h3` row: "prefix `82489ffffffffff`, resolution 10, n = 24"
   - B3, key-size bullet: "per-server VIDPF key ≈ 33 + n·(42 + 24·(m+1)) bytes: ≈ 1,833 B (Morton,
     n = 20), 1,923 B (geohash, n = 21), 2,013 B (grid, n = 22), 2,193 B (H3, n = 24), plus the FLP
     proof share."
2. **B3's round count is off by one.** Counting the RPCs gives 2n + 3 online rounds when no Merkle
   search is needed and N ≤ `query_flp_batch_size`:
   - tree_init, flp, apply;
   - (n − 1) × (crawl + prune);
   - crawl_last, get_proofs, prune, final.

   That's 43 / 45 / 47 / 51 for Morton / geohash / grid / H3. Suggested B3 text: "online rounds = 2n + 3
   when `rounds.extra` = 0 (43 for Morton, n = 20)". Prompt 2 will measure it.
3. **`results.json` is from the week run.** δ = 5 applies to the day file. Its threshold of 1180
   (2% of 59,040) does not; the benchmark uses τ = 0.005.
4. **The source file's hash depends on line endings.** Git stores
   `data/mosaic/sample_busiest_day.csv` with LF, but this checkout has CRLF (`core.autocrlf=true`).
   The manifest records both: `sha256` (the bytes on disk, `c53204d8…`) and `sha256_lf`
   (`24614c0a…`). The subsets themselves are LF and protected by `.gitattributes`, so the Mosaic
   side should verify those.
5. **Two additions the spec doesn't mention:** `bench/data/.gitattributes` and the `.gitignore`
   exception. Both are needed for the manifest and subsets to be committed and to stay byte-identical.
6. **Issue S1 isn't fixed on `benchmarking`.** The constant PRG control bits are still in
   `src/prg.rs`, so each server can read every client's α. Record this in B5 and
   `LEAKAGE_mastic.md`.
7. **`src/configs/synthetic-ten-locations-*` still use geohash 20 / H3 res 9.** That's harmless:
   `run_bench.py` will write its own configs from `ENCODINGS` (your decision), leaving `src/configs`
   untouched.
8. **`dirty: true`** comes from your `README.md` change and this prompt's two edits. Commit before the
   timing runs so the results record a clean commit.
9. **Geohash labels.** With 21 bits after the 15-bit prefix, labels end in a bracketed bit, e.g.
   `9v6ec91[1]`, because 36 bits isn't a multiple of 5. The Rust driver prints the same format, and
   matching uses the bits.

## What to commit

```
git add .gitignore scripts/check_heavy_hitters.py bench/README.md bench/requirements.txt bench/scripts/bench_common.py bench/scripts/make_subsets.py bench/scripts/ground_truth.py bench/data/.gitattributes bench/data/manifest.json bench/data/day_1of3.csv bench/data/day_2of3.csv bench/data/day_3of3.csv bench/ground_truth/day_1of3.json bench/ground_truth/day_2of3.json bench/ground_truth/day_3of3.json bench/ground_truth/smoke_ten_locations.json bench/reports/01-harness.md
```

Commit message: `Add benchmark harness: seeded day subsets, per-encoding ground truth, common helpers`
