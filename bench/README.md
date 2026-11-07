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
| `scripts\` | `bench_common.py`, `make_subsets.py`, `ground_truth.py`; `run_bench.py` and `evaluate.py` follow |
| `reports\` | one report per prompt |
| `results\` | raw result lines, summary, tables and plots (later prompts) |
