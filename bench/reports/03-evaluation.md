# 03: Evaluation

## Summary

`bench/scripts/evaluate.py` reads `bench/results/raw/*.jsonl`, the ground truth and, if present,
`bench/reference/`. It writes:
- `summary_mastic.csv`: 12 rows × 135 columns. The columns come from one constant,
  `SUMMARY_COLUMNS`.
- `tables.md`;
- the six A10 plots, as PNG and PDF;
- with `--combine`, a side-by-side `comparison.md` and comparison plots.

The network model, the matching, the self-check, the leakage totals and the statistics are small
functions. They have 22 pytest tests, and all pass.

On the full benchmark (72 runs, commit `5efbdeb`, clean):
- **Self-check:** 1.0 / 1.0 / exact for all 72 runs, warm-ups included.
- **B3 sanity checks:** 60 of 60 pass.
- **Accuracy against the reference:** not available yet (`bench/reference/` does not exist). Those
  columns are empty, and the script says so.

Measured online time grows linearly with N, from 1.7–1.8 s (N = 7,194) to 5.7–6.0 s
(N = 21,581). The four encodings are within 7 % of each other.

Modelled network cost:
- **LAN:** +30–47 ms.
- **WAN:** +2.3–2.9 s, almost all of it round trips (45–53 rounds × 50 ms).

One finding needs a decision: about 90 s into the benchmark the laptop slowed down by ~22 % and
stayed there (Deviation 1).

## Files

Created:
- `bench/scripts/evaluate.py`: the evaluation, with `SUMMARY_COLUMNS`, the A4 model, the A5
  matching and self-check, `tables.md`, the plots and `--combine`.
  - It imports `run_bench.self_check`, so the runner and the evaluation share one definition.
- `bench/tests/test_evaluate.py`: 22 tests.
  - The model on hand-computed numbers (including the real `mosaic_grid` 3/3 run).
  - Matching on a hand-made example with P = 3/4, R = 2/3, F1 = 12/17 and count error 0.15.
  - The δ boundary and tie-breaks, the reference formats, the self-check, the leakage totals,
    the median/min/max with warm-ups excluded, and the CSV round trip.
- `bench/results/summary_mastic.csv`, `bench/results/tables.md`, `bench/results/plots/` (12
  files, 708 KB): generated. A rerun reproduces all 14 files byte for byte.
- `bench/reports/03-evaluation.md`: this report.

Modified:
- `.gitignore`: re-includes `bench/results/plots/**/*.png` and `*.pdf`. The repo ignores all
  `*.png`/`*.pdf`, so the plots could not be committed otherwise.
- `bench/requirements.txt`: adds `matplotlib>=3.8` and `pytest>=8`. I installed matplotlib 3.11.2
  and pytest 9.2.0 into `.venv`.
- `bench/README.md`: a new section 6 (evaluation), the setup note and the layout rows.

Not touched:
- the protocol and driver code;
- `run_bench.py`, `bench_common.py`;
- the raw results and the ground truth.

## How to run

```powershell
.\.venv\Scripts\python.exe -m pip install -r scripts\requirements.txt -r bench\requirements.txt
.\.venv\Scripts\python.exe -m pytest bench\tests -p no:cacheprovider
.\.venv\Scripts\python.exe bench\scripts\evaluate.py
# at the end, with Mosaic's summary copied into bench\results\:
.\.venv\Scripts\python.exe bench\scripts\evaluate.py --combine bench\results\summary_mosaic.csv
```

Exit codes:
- **0:** all checks pass.
- **1:** a self-check or schema failure. The outputs are still written, and a banner goes to the
  console and to the top of `tables.md`.
- **2:** the input can't be used.

`--no-plots` skips the plots.

## Checks

| Check | Numbers | Status |
|---|---|---|
| Tests | 22 of 22 pass | pass |
| Input | 72 run lines + 4 client lines, all valid against A9; smoke lines in `raw/`: 0 | pass |
| Repetitions (A2, A8) | 5 measured + 1 warm-up for each of the 12 configurations; warm-ups excluded from every statistic | pass |
| Environment | one commit (`5efbdeb`), dirty false, 5 threads per server, same CPU on every line | pass |
| Self-check (A5, B4) | 72 of 72 runs (60 measured + 12 warm-ups) have P = 1.0, R = 1.0 and exact counts. Centres are on the ground truth's Mosaic-grid cells, and N and T equal the ground truth's | pass |
| Accuracy vs reference (A5) | `bench/reference/` missing → columns empty, stated in console, `tables.md` and plot 5 (which shows the self-check instead) | as specified |
| Online rounds = 2n + 5 (B3) | 45 / 47 / 49 / 53 for Morton / geohash / grid / H3; extra = 0 in all runs | pass (12/12) |
| Upload rounds as implemented = ⌈N/1000⌉ (B3) | 8 / 15 / 22 | pass (12/12) |
| Validity bits = 2N (A7 decision) | 14,388 / 28,774 / 43,162 | pass (12/12) |
| Upload bytes per client (runs) = report size (`keygen_bench`) | 3,136 / 3,274 / 3,412 / 3,688 B. The runs measure 0.006 B per client more: the 3-byte length prefix of each batch | pass (12/12) |
| Server ↔ server bytes = 0 (B3) | 0 in every run | pass |
| Online steps add up to `online_ms` | max difference 2.96 ms, median 1.86 ms over 72 runs | pass |
| Model by hand (A4) | `mosaic_grid` 3/3. LAN: 49 × 0.5 + 2,595,282 B × 8 / 10⁹ = 45.262 ms. WAN: 2,450 + 207.623 = 2,657.623 ms. Same values in the summary | pass |
| Reference path and `--combine` | Tested in the scratchpad, without touching the repo. **Reference:** a synthetic reference (grid cells shifted by 2, counts × 3, plus one far hotspot) gives R 0.875, P 1.000, F1 0.933, count error 0.667 for `mosaic_grid` 1/3, equal to the hand values. **Combine:** a relabelled copy of the summary gives `comparison.md` and 12 comparison plots, with one marker per system | pass |
| Deterministic output | rerun → all 14 output files byte-identical | pass |

Results, in short (medians; full tables in `bench/results/tables.md`):
- **Online, measured:**
  - 1,731–1,838 ms at N = 7,194;
  - 3,623–3,862 ms at N = 14,387;
  - 5,676–6,012 ms at N = 21,581.
- **Online, modelled:**
  - LAN adds 29.6–47.3 ms.
  - WAN adds 2,321–2,858 ms. Of that, 2,250–2,650 ms is rounds × RTT and 71–208 ms is bytes.
  - The busiest link is server → collector: 0.88–2.60 MB per server, mostly FLP verifier shares.
- **Upload per client:** 3,136–3,688 B. The modelled client upload takes 3.0–3.5 ms (LAN) or
  52.5–53.0 ms (WAN).
- **Client report generation:** median 0.021–0.023 ms; p95 ≤ 0.026 ms.
- **Leakage:**
  - 720–800 revealed counts per run, nearly constant in N.
  - Of these, 367–415 are kept and 353–385 pruned.
  - The final frontier has 7–16 cells.

B7 checklist, from these results: self-check for every run [x]; key sizes and rounds [x]. A2
encoding parameters: still pending your A2 update from report 01.

## Deviations and open questions

1. **The machine slowed down during the benchmark (decision needed).**
   - The repetition-drift table in `tables.md` shows a step: every run up to 15:25:21 (warm-up
     rep 0, all of `mosaic_grid` and `geohash` in rep 1, and `h3` 1/3 and 2/3 in rep 1) ran at
     0.72–0.94 of its configuration's median.
   - From then on, every run was at 0.96–1.16. That is about 90 s of load, which is typical of
     a 15 W laptop CPU dropping to its sustained power limit, or of thermal throttling.
   - **Medians are consistent:** every configuration has at least 4 of 5 measured runs in the
     slow state.
   - **The minimums of 8 configurations come from the fast rep 1.** That is why some [min–max]
     ranges are wide, e.g. `mosaic_grid` 3/3 4,544–6,022 ms.
   - Options:
     - (a) Keep the data. Report medians (sustained state) and say so in A8. Make sure Mosaic is
       also measured in the sustained state.
     - (b) Rerun with a longer warm-up, about 2 minutes of load (e.g. 2 warm-up repetitions),
       so that every measured run is in the sustained state. This changes A2 in both files and
       needs a small `run_bench.py` option.
   - I recommend (a), unless Mosaic's runs are short enough to land in the fast state.
2. **Reference file format and meaning of its `count` (decision for A5, both files).**
   - The spec does not define `bench/reference/day_<subset>.json`. The reader accepts:
     - a list, or `{"hotspots": [...]}` (also under `cells` / `points`);
     - items `[lat_grid, lon_grid(, count)]`, `{"grid": [..], "count": c}`,
       `lat_grid`/`lon_grid`, or `lat`/`lon` in degrees.
   - Proposed for the Mosaic harness:
     `{"dataset": "day", "subset": "3of3", "T": 107, "delta": 5, "hotspots": [{"grid": [lat_grid, lon_grid], "count": c}]}`.
     If `T`/`delta` are present and differ from ours, the script prints a note.
   - Please also define what a fuzzy hotspot's `count` is. If it is the number of rides within δ,
     it will be much larger than Mastic's exact cell count, so the count error will be large by
     construction.
3. **Count-error pairing (please confirm).**
   - Only reference hotspots with a reported location within δ are paired. Each is paired with
     the nearest one: L∞ first, then Euclidean, then the larger count.
   - Hotspots without a match within δ add to the recall loss but not to the count error.
4. **Model details (please confirm).**
   - R(online) = `rounds.online` + `rounds.extra`. The Merkle re-sends are sequential online steps;
     extra = 0 here, so the numbers don't change.
   - max_link is taken over five directional links: collector → server and server → collector
     for each server separately, plus server ↔ server.
   - Units are decimal: 1 Gbit = 10⁹ bit.
   - Client report bytes = measured upload bytes of both servers / N, sent through one 10 Mbit/s
     uplink.
5. **Summary layout (please confirm for the Mosaic harness).**
   - The Mosaic harness must copy `SUMMARY_COLUMNS`. `--combine` refuses files whose columns
     differ.
   - Per-step timings and per-level leakage are strings (`flp=120.8;level_0=16.5;…`,
     `2;4;8;…`), because the number of levels differs between encodings and systems.
   - The client metrics are repeated in each subset's row.
   - `delta` is the evaluation δ (5), although Mastic's own `params.delta` is null.
   - Empty cell = not applicable or not available.
6. **Outputs beyond A10 (for information).**
   - I chose the names `comparison.md` and `plots/comparison/` for `--combine`.
   - `tables.md` also has the online steps, the B3 checks and the repetition drift.
   - Until the reference exists, plot 5 shows the self-check recall/precision, and its title says
     so.
7. **`.gitignore`.** I added a narrow exception so that the plots can be committed. If you'd
   rather not change `.gitignore`, revert it and use `git add -f bench/results/plots` instead.

## What to commit

```
git add .gitignore bench/README.md bench/requirements.txt bench/scripts/evaluate.py bench/tests/test_evaluate.py bench/results/summary_mastic.csv bench/results/tables.md bench/results/plots bench/reports/03-evaluation.md
```

Commit message: `Add evaluate.py: summary CSV, A4 network model, self-check, tables and plots`
