# Mastic benchmark harness

How to run the benchmark described in [`BENCHMARK_MASTIC.md`](BENCHMARK_MASTIC.md), which is the
source of truth. All commands run in PowerShell from the repository root with the repo's `.venv`.

## Setup

```powershell
python -m venv .venv                       # once, if .venv does not exist
.\.venv\Scripts\python.exe -m pip install -r scripts\requirements.txt -r bench\requirements.txt
```

`scripts\requirements.txt` provides `h3` (Uber's reference library). `bench\requirements.txt`
provides `psutil` (cores and RAM for the environment record), `matplotlib` (plots) and `pytest`
(tests of `evaluate.py`).

## 1. Data subsets (A1), done once

```powershell
.\.venv\Scripts\python.exe bench\scripts\make_subsets.py --source data\mosaic\sample_busiest_day.csv --dataset day --seed 20261107
```

This writes `bench\data\day_1of3.csv`, `day_2of3.csv` and `day_3of3.csv` (7,194 / 14,387 / 21,581
rides, nested, seeded shuffle) and `bench\data\manifest.json` with the SHA-256 of every file.

- The rows are copied unchanged, and the script checks this.
- The files have LF line endings. `bench\data\.gitattributes` stops Git from converting them, so the
  hashes hold on every checkout.
- The manifest records the source's hash twice: `sha256` of the bytes on disk, and `sha256_lf`. On
  Windows Git checks the source out with CRLF.
- Copy the three CSVs and `manifest.json` to the Mosaic codebase unchanged. Don't regenerate them.

## 2. Ground truth (A5, B4)

```powershell
.\.venv\Scripts\python.exe bench\scripts\ground_truth.py
.\.venv\Scripts\python.exe bench\scripts\ground_truth.py --tau 0.02
.\.venv\Scripts\python.exe bench\scripts\ground_truth.py --csv data\synthetic\ten_locations.csv --threshold 0.1
.\.venv\Scripts\python.exe bench\scripts\ground_truth.py --check-grid
```

1. **The first command** checks each subset against the manifest, then writes
   `bench\ground_truth\day_<subset>.json` at Mastic's τ = 0.005. Each file has, per encoding:
   - N (rides encoded) and T = max(1, ⌊τ·N⌋);
   - the rides dropped and the number of distinct cells;
   - every heavy cell: its bits, label, centre (lat, lon), Mosaic-grid coordinates of the centre,
     and count.
2. **`--tau`** does the same at another τ and writes `bench\ground_truth\day_<subset>_tau<τ>.json`
   (the 0.005 files keep their names). The console table also shows the busiest cell's count. At
   τ = 0.02 (T = 143 / 287 / 431) every encoding has 0 heavy cells: the busiest cell holds 214 rides
   (231 for H3).
3. **`--csv`** does the same for any CSV at any threshold (a smoke test) and writes
   `bench\ground_truth\smoke_<name>.json`. For the 10-landmark file it must find 4 cells with counts
   50/40/30/20 for every encoding.
4. **`--check-grid`** checks that Mosaic's grid formula, applied to the day file, gives exactly
   `data\mosaic\start_busiest_day.json`.

The encoders come from `scripts\check_heavy_hitters.py`, so the ground truth and the checker can't
drift apart.

## 3. Common helpers

```powershell
.\.venv\Scripts\python.exe bench\scripts\bench_common.py               # environment + config check
.\.venv\Scripts\python.exe bench\scripts\bench_common.py --self-test   # result schema check
```

`bench\scripts\bench_common.py` holds:
- **the experiment's parameters** in one place: `ENCODINGS` (encoding parameters and n), τ per
  system (`THRESHOLDS`: Mastic 0.005, Mosaic 0.02; `THRESHOLD_FRAC` is Mastic's), the reference's
  τ and δ, the seed and δ. `threshold_abs(n, tau)` always takes τ explicitly, and
  `ground_truth_path(dataset, subset, tau)` names the ground-truth file for a τ;
- **the environment record (A8)**: CPU, physical/logical cores, RAM, OS, threads per server
  (⌊physical / 2⌋), rustc version, git commit and dirty flag;
- **the A9 schema check** (`validate_result`), and `append_jsonl`, which only writes lines that pass
  it.

Without options it also reports whether `src\configs\ride-austin-day-*.toml` match `ENCODINGS`.

## 4. Running the benchmark (A2, A8, B7)

```powershell
.\.venv\Scripts\python.exe bench\scripts\run_bench.py --smoke      # 4 smoke runs + client micro-benchmark
.\.venv\Scripts\python.exe bench\scripts\run_bench.py --dry-run    # print the plan, write the configs
.\.venv\Scripts\python.exe bench\scripts\run_bench.py              # the full benchmark: 72 runs, about 5 min
```

`bench\scripts\run_bench.py`:
1. builds `server`, `driver` and `keygen_bench` in release mode, once;
2. writes one driver config per encoding × subset to `bench\.run\configs\` (ignored by Git), with
   the encoding parameters from `ENCODINGS`, τ = 0.005 (or `--tau`) and the servers on `127.0.0.1`.
   `src\configs\` is not touched;
3. runs `keygen_bench` once per encoding (A6) and appends to `bench\results\raw\mastic_client.jsonl`;
4. repetition loop outermost (rep 0 = warm-up, reps 1–5 measured), for every encoding and subset:
   - checks that both ports are free;
   - starts both servers with `RAYON_NUM_THREADS` = ⌊physical cores / 2⌋ and waits for their ports;
   - runs the driver with `-n` = the subset's rows and `--bench-out`;
   - checks the line against the A9 schema and the ground truth (self-check), then appends it to
     `bench\results\raw\mastic.jsonl`;
   - stops the servers, also when the driver fails or on Ctrl+C.

It stops at the first failure. It prints run *i* of *N*, the elapsed time and an estimate of the time
left. Logs and a per-run record (wall-clock time, peak memory, self-check) go to `bench\.run\`.
`--malicious` is never used.

| Option | Effect |
|---|---|
| `--smoke` | `data\synthetic\ten_locations.csv`, threshold 0.1, 1 run per encoding; results in `bench\.run\smoke\` |
| `--tau X` | threshold fraction (default 0.005, Mastic's τ); the driver records it in `params.threshold_frac`. Needs `ground_truth.py --tau X` first; configs and logs get a `-tau<X>` suffix. Mastic is not meant to run at 0.02 (A2) |
| `--reps R` | R measured repetitions (default 5), plus the warm-up unless `--no-warmup` |
| `--only-encoding E`, `--only-subset S` | restrict the matrix (repeatable) |
| `--threads T` | threads per server (default ⌊physical cores / 2⌋ = 5 on the benchmark laptop) |
| `--port-base P` | server 0 on P, server 1 on P + 1 (default 18100; 8000 is refused) |
| `--results-dir D` | write `mastic.jsonl` and `mastic_client.jsonl` to D instead of `bench\results\raw` |
| `--skip-keygen`, `--no-build`, `--dry-run` | skip the client micro-benchmark / the build / everything but the plan |

Timing run of one `day_3of3` run per encoding, without touching the results:

```powershell
.\.venv\Scripts\python.exe bench\scripts\run_bench.py --only-subset 3of3 --reps 1 --no-warmup --skip-keygen --results-dir bench\.run\check
```

## 5. The instrumented driver and the client micro-benchmark

The runner calls these. To call them by hand:

```powershell
.\target\release\driver.exe -c bench\.run\configs\geohash-3of3.toml -n 21581 --bench-out out.jsonl --bench-meta "@meta.json"
.\target\release\keygen_bench.exe -c bench\.run\configs\keygen-geohash.toml
```

- **`--bench-out FILE --bench-meta JSON`** (always together). The driver appends one A9 result
  line to `FILE`.
  - The meta object holds `dataset`, `subset`, `rep`, `warmup` and optionally `timestamp` and `env`.
    Without `env` the driver fills in what Rust knows.
  - Pass the meta as `"@file"`. Windows PowerShell 5.1 strips the quotes from inline JSON.
  - Without these flags the driver behaves exactly as before, and `--export` is unchanged.
- **`keygen_bench`** times one client's report generation on one thread (A6). It uses the first
  2,000 rows of `bench\data\day_3of3.csv` (`--csv`, `-k`) after one warm-up pass. It writes
  mean/median/p95 and the report size per server to `bench\results\raw\mastic_client.jsonl`
  (`--out`).
- **Bytes** are the bincode size of each message as tarpc sends it (`bincode::DefaultOptions`, i.e.
  varint integers), without tarpc's envelope and frame length. `keygen_bench` also prints the
  `bincode::serialize` sizes, which B3's key-size formula uses.

## 6. Evaluation (A4, A5, A7, A10)

```powershell
.\.venv\Scripts\python.exe -m pytest bench\tests -p no:cacheprovider     # tests of evaluate.py
.\.venv\Scripts\python.exe bench\scripts\evaluate.py                     # summary, tables, plots
.\.venv\Scripts\python.exe bench\scripts\evaluate.py --combine bench\results\summary_mosaic.csv
.\.venv\Scripts\python.exe bench\scripts\evaluate.py --reference-dir D --out-dir E   # e.g. a test reference, outputs elsewhere
```

`bench\scripts\evaluate.py` reads only:
- `bench\results\raw\*.jsonl`;
- `bench\ground_truth\`, at each run's τ;
- `bench\reference\` (or `--reference-dir`), if it exists.

Smoke lines (subset `smoke`) are skipped. Warm-ups are self-checked but left out of every
statistic. It writes:

| Output | Contents |
|---|---|
| `bench\results\summary_mastic.csv` | one row per encoding × subset × τ (`threshold_frac` is a column and part of the row key): median/min/max over the measured runs of every metric, the modelled LAN/WAN times, client metrics, leakage counters, self-check and accuracy. The columns are `SUMMARY_COLUMNS` in `evaluate.py`; the Mosaic harness must write the same columns. |
| `bench\results\tables.md` | timings and online steps, modelled communication, communication as measured, client cost, accuracy, leakage, B3 sanity checks, repetition drift |
| `bench\results\plots\` | the six A10 plots, PNG + PDF, one colour per encoding |
| `bench\results\comparison.md`, `bench\results\plots\comparison\` | only with `--combine`: side by side, one marker per system |

- **Network model (A4).**
  - t_comm = R × RTT + busiest link / bandwidth, with R = online + extra rounds.
  - t_online_est = measured online + t_comm. LAN: 0.5 ms, 1 Gbit/s. WAN: 50 ms, 100 Mbit/s.
  - Client upload = 1 × RTT + report bytes / 10 Mbit/s.
  - `rounds.upload` is reported as implemented, but the model doesn't use it.
- **Self-check (A5).** Every run is compared with the ground truth at its own τ: cells by label,
  counts, centres and their Mosaic-grid coordinates, N and T. A failure prints a banner, goes at
  the top of `tables.md` and makes the exit code 1.
- **Reference (A5).** `bench\reference\day_<subset>.json` holds Mosaic's qualifying squares Q at
  τ = 0.02, δ = 5, grouped into areas (format in A5). Every run of every τ is measured against it on
  the Mosaic grid: `area_recall`, `precision`, `f1`, `areas_found` / `reference_areas`, `outputs` /
  `reference_squares`. Counts are not compared. The reader accepts only the A5 format and checks it
  against A5 (counts ≥ T, area sizes and peaks, areas = squares touching on a side or a corner); any
  other file stops the script with exit code 2 and a message naming the problem. A τ, δ or T that
  differs from A5 is reported as a note. Without the folder these columns stay empty, and the
  script says so.
- **Several τ.** Tables have a τ column; plots tell τ values apart by line style and hollow markers,
  and plot 5 has one row of panels per τ. `--combine` puts one column per system × τ side by side,
  checks that N and δ agree, that T agrees wherever two systems ran the same τ, and that
  T = max(1, ⌊τ·N⌋) in every row.
- **Leakage (A7).** Kept = Σ `frontier_per_level`; pruned = revealed − kept.

## Parameters

| Encoding | n | Parameters |
|---|---|---|
| `mosaic_grid` | 22 | origin (29.267, −98.743), 0.001° cells, 11 bits per axis |
| `geohash` | 21 | prefix `9v6`, 21 bits |
| `h3` | 24 | prefix `82489ffffffffff`, resolution 10 |
| `morton` | 20 | lat 29.9005–30.9245, lon −98.0505 to −97.0265, 10 bits per axis |

Threshold τ per system (A2): Mastic 0.005 (T = 35 / 71 / 107); Mosaic 0.02 (T = 143 / 287 / 431),
optionally also 0.005. Reference: Mosaic's squares at τ = 0.02, δ = 5 grid cells (L∞). Seed
20261107.

Geohash and H3 use the area-matched values of `src\configs\ride-austin-day-*.toml`, not A2's
20 bits / resolution 9. A2 and B3 are to be updated in both benchmark files. Change the parameters
only in `ENCODINGS`.

## Layout

| Path | Contents |
|---|---|
| `data\` | subsets and `manifest.json` (generated here, copied to Mosaic) |
| `ground_truth\` | exact heavy cells per encoding and subset: `day_<subset>.json` (τ = 0.005), `day_<subset>_tau0.02.json` (τ = 0.02, all empty) |
| `reference\` | Mosaic's qualifying squares and areas, A5 format, from the Mosaic harness (not yet available) |
| `scripts\` | `bench_common.py`, `make_subsets.py`, `ground_truth.py`, `run_bench.py`, `evaluate.py` |
| `tests\` | `test_evaluate.py` (pytest) |
| `reports\` | one report per prompt |
| `results\raw\` | `mastic.jsonl` (one line per run) and `mastic_client.jsonl` (one line per encoding) |
| `results\` | `summary_mastic.csv`, `tables.md`, `plots\` (written by `evaluate.py`) |
| `.run\` | runner configs, logs, smoke and check results, `runner_log.jsonl` (ignored by Git) |
