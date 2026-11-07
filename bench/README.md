# Mastic benchmark harness

How to run the benchmark described in [`BENCHMARK_MASTIC.md`](BENCHMARK_MASTIC.md), which is the
source of truth. All commands run in PowerShell from the repository root with the repo's `.venv`.

## Setup

```powershell
python -m venv .venv                       # once, if .venv does not exist
.\.venv\Scripts\python.exe -m pip install -r scripts\requirements.txt -r bench\requirements.txt
```

`scripts\requirements.txt` provides `h3` (Uber's reference library) and `bench\requirements.txt`
provides `psutil` (cores and RAM for the environment record).

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
.\.venv\Scripts\python.exe bench\scripts\ground_truth.py --csv data\synthetic\ten_locations.csv --threshold 0.1
.\.venv\Scripts\python.exe bench\scripts\ground_truth.py --check-grid
```

1. **The first command** checks each subset against the manifest, then writes
   `bench\ground_truth\day_<subset>.json`. Each file has, per encoding:
   - N (rides encoded) and T = max(1, ⌊0.005·N⌋);
   - the rides dropped and the number of distinct cells;
   - every heavy cell: its bits, label, centre (lat, lon), Mosaic-grid coordinates of the centre,
     and count.
2. **The second command** does the same for any CSV at any threshold (a smoke test) and writes
   `bench\ground_truth\smoke_<name>.json`. For the 10-landmark file it must find 4 cells with counts
   50/40/30/20 for every encoding.
3. **The third command** checks that Mosaic's grid formula, applied to the day file, gives exactly
   `data\mosaic\start_busiest_day.json`.

The encoders come from `scripts\check_heavy_hitters.py`, so the ground truth and the checker can't
drift apart.

## 3. Common helpers

```powershell
.\.venv\Scripts\python.exe bench\scripts\bench_common.py               # environment + config check
.\.venv\Scripts\python.exe bench\scripts\bench_common.py --self-test   # result schema check
```

`bench\scripts\bench_common.py` holds:
- **the experiment's parameters** in one place: `ENCODINGS` (encoding parameters and n), the
  threshold fraction, the seed and δ;
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
   the encoding parameters from `ENCODINGS`, τ = 0.005 and the servers on `127.0.0.1`.
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

## Parameters

| Encoding | n | Parameters |
|---|---|---|
| `mosaic_grid` | 22 | origin (29.267, −98.743), 0.001° cells, 11 bits per axis |
| `geohash` | 21 | prefix `9v6`, 21 bits |
| `h3` | 24 | prefix `82489ffffffffff`, resolution 10 |
| `morton` | 20 | lat 29.9005–30.9245, lon −98.0505 to −97.0265, 10 bits per axis |

Threshold τ = 0.005 (T = 35 / 71 / 107), δ = 5 grid cells (L∞), seed 20261107.

Geohash and H3 use the area-matched values of `src\configs\ride-austin-day-*.toml`, not A2's
20 bits / resolution 9. A2 and B3 are to be updated in both benchmark files. Change the parameters
only in `ENCODINGS`.

## Layout

| Path | Contents |
|---|---|
| `data\` | subsets and `manifest.json` (generated here, copied to Mosaic) |
| `ground_truth\` | exact heavy cells per encoding and subset |
| `reference\` | reference hotspots from the Mosaic harness (not yet available) |
| `scripts\` | `bench_common.py`, `make_subsets.py`, `ground_truth.py`, `run_bench.py`; `evaluate.py` follows |
| `reports\` | one report per prompt |
| `results\raw\` | `mastic.jsonl` (one line per run) and `mastic_client.jsonl` (one line per encoding) |
| `results\` | summary, tables and plots (later prompts) |
| `.run\` | runner configs, logs, smoke and check results, `runner_log.jsonl` (ignored by Git) |
