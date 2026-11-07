# Benchmarking Mastic (location heavy hitters)

This file is the source of truth for benchmarking the **Mastic** codebase (`mastic_for_fuzzy_HH`). It lives at `mastic_for_fuzzy_HH/bench/BENCHMARK_MASTIC.md`. The Mosaic codebase has its own file, `BENCHMARK_MOSAIC.md`.

The two benchmarks run **separately**, each inside its own codebase. They can only be compared at the end if the **common definitions** (Part A) are identical in both files. Change Part A in both files at the same time, or not at all.

Only two things cross between the codebases:

| What | Direction | Why |
|---|---|---|
| The data subsets (`bench/data/`) | Mastic → Mosaic | both systems must see exactly the same rides |
| The reference hotspots (`bench/reference/`) | Mosaic → Mastic | "accuracy" is measured against Mosaic's plaintext fuzzy definition |

All parameters have defaults. Change one only after agreeing with the professors, and then in **both** files.

---

# Part A: Common definitions (identical in both files)

## A1. Data

**Where it comes from.** The rides are RideAustin trips (Austin, Texas, 2016–2017), as cleaned and sampled by the Mosaic authors in their published artifact (Zenodo record 20768775, `fuzzyheavyhitters.zip`, folder `data/`). Our fork copied them into `mastic_for_fuzzy_HH/data/mosaic/` (see the `README.md` there). That folder holds **data only**; the Mosaic codebase is a separate repository.

| File in `data/mosaic/` | Contents | Use |
|---|---|---|
| `sample_busiest_day.csv` | 21,581 rides | **the benchmark data** |
| `sample_busiest_week.csv` | 59,040 rides | not used (Mosaic's own `results.json` run used this file with threshold 2%) |
| `sample_busiest_month.csv` | 115,174 rides (actually 14 days, 2017-03-31 to 2017-04-13) | not used |
| `start_busiest_day.json` | the day's pickups on Mosaic's grid as `[lat_grid, lon_grid]` | sanity check of the grid conversion |
| `results.json` | Mosaic's own experiment results | source for δ and Mosaic's parameters |

Only the columns `start_location_lat` and `start_location_long` are used. Coordinates are rounded to 0.001°. Mosaic's grid: `lat_grid = round((lat − 29.267)·1000)`, `lon_grid = round((lon + 98.743)·1000)`, 11 bits per axis. The day file is also the one referenced by `src/configs/ride-austin-day-*.toml` (`[dataset].path`).

The RideAustin terms apply to the subsets too (the data README says to check them before redistributing). Treat `bench/data/` like `data/mosaic/`.

**Subsets.** The benchmark measures each system at one third, two thirds and all of the clients:

1. Load `data/mosaic/sample_busiest_day.csv` from the Mastic repo.
2. Drop rows with missing or non-numeric coordinates (expected: none). Record the number dropped.
3. Shuffle all rows with `random.Random(20261107).shuffle`.
4. Take the first round(N/3), round(2N/3) and N rows of the shuffled order: **7,194 / 14,387 / 21,581** rides. The subsets are nested (`1of3` ⊂ `2of3` ⊂ `3of3`), and the spatial distribution is the same in all three, so only N changes.
5. Write `bench/data/day_1of3.csv`, `day_2of3.csv`, `day_3of3.csv` with the original header and columns.
6. Write `bench/data/manifest.json`: per file the path, row count, SHA-256, seed, and the source file with its SHA-256.

The subsets are generated **once, in the Mastic codebase**, and copied to the Mosaic codebase, which checks the SHA-256 values against the manifest.

Never subset with the driver's `-n`: it keeps the earliest rides, and the files are sorted by time. Always pass `-n` equal to the subset's row count.

## A2. Experiment matrix

| Factor | Values |
|---|---|
| Encoding | `mosaic_grid`, `geohash`, `h3`, `morton` |
| Subset | `day_1of3`, `day_2of3`, `day_3of3` |
| Repetitions | 1 warm-up (discarded) + 5 measured |
| Malicious clients | none |

Per system: 4 encodings × 3 subsets × 6 runs = **72 runs**.

| Parameter | Value (same in both systems) |
|---|---|
| Threshold | fraction τ = **0.005** of the subset size (the value in the existing day configs; confirm); T = max(1, ⌊τ·N⌋), i.e. 35 / 71 / 107 for the three day subsets |
| Weight | constant: every ride is one vote |
| Mosaic radius δ | **5** grid cells, L∞ (from Mosaic's `results.json`; confirm) |
| `mosaic_grid` | origin (29.267, −98.743), 0.001° cells, 11 bits per axis, n = 22 |
| `geohash` | prefix `9v6`, 21 bits, n = 21 (area-matched to the grid) |
| `h3` | prefix `82489ffffffffff`, resolution 10, n = 24 (area-matched to the grid) |
| `morton` | lat 29.9005–30.9245, lon −98.0505 to −97.0265, 10 bits per axis, n = 20 |

If an encoding uses different parameters in either codebase, align them before benchmarking.

## A3. Phases

| Metric | Definition |
|---|---|
| **Client report generation** | Time for one client to turn its location into the reports for all servers, on one thread (encoding, key generation, proofs, share splitting). Measured with a micro-benchmark (A6). |
| **Server offline** | Server work that does not depend on client data and could be done before the reports arrive. |
| **Upload** | From the first report sent until the last report is stored on all servers. Reported separately, not part of online. |
| **Online** | From the start of aggregation (all reports stored) until the collector holds the final output. Wall-clock on localhost. |
| **Online steps** | The online phase broken into its natural steps (per level, per protocol step). |

"Offline" in the report means client report generation **and** server offline work, shown as two separate numbers. Per-client time is never added to server time.

## A4. Communication

**Rounds.** A round is one sequential step in which a party sends messages and must wait for the replies before continuing. Messages sent concurrently to several parties in the same step count as one round. Report `upload`, `online` and `extra` rounds separately.

**Bytes.** Application-level size of every message **as actually sent** (i.e. with the encoding the RPC layer really uses), summed per link and per phase. Framing overhead (TCP, RPC headers) is not counted.

| Link | Meaning |
|---|---|
| `client_to_server` | report shares, per server |
| `server_to_server` | direct traffic between the servers |
| `collector_to_server` | requests from the collector, per server |
| `server_to_collector` | responses to the collector, per server |

**Communication time (modelled).** On localhost the network costs almost nothing, so communication time is modelled:

    t_comm(phase) = R_phase × RTT + max_link(B_link,phase) / bandwidth
    t_online_est  = online_ms (measured)  +  t_comm(online)

| Profile | RTT | Bandwidth |
|---|---|---|
| LAN | 0.5 ms | 1 Gbit/s |
| WAN | 50 ms | 100 Mbit/s |
| Client uplink (upload only) | — | 10 Mbit/s |

**Upload in the model.** `rounds.upload` records the upload rounds **as implemented** (the simulator sends batches one after another). The deployment model treats upload per client: each client uploads once, so client upload time = 1 × RTT + report bytes / client uplink. Upload is never added to online time.

## A5. Accuracy

All reported locations are converted to the Mosaic grid (cell centre → grid coordinates) before comparison.

**Self-check (correctness).** Compare each run's output with the plaintext answer of the same system, encoding and subset: precision, recall, and whether every count is exact. Expected 1.0 / 1.0 / exact; anything else is a bug and blocks the benchmark.

**Accuracy against the reference.** The reference hotspots are Mosaic's plaintext fuzzy hotspots, computed on the Mosaic grid with the same T and δ, by the Mosaic harness (`bench/reference/day_<subset>.json`). For each run:

| Metric | Definition |
|---|---|
| recall | share of reference hotspots with a reported location within L∞ ≤ δ grid cells |
| precision | share of reported locations within L∞ ≤ δ of some reference hotspot |
| F1 | 2·P·R / (P + R) |
| outputs | number of reported cells or points |
| count error | median \|reported − reference\| / reference over matched pairs (each reference hotspot matched to its nearest reported location) |

## A6. Client micro-benchmark

One thread; the first K = **2,000** rows of `day_3of3.csv`; one warm-up pass. Time each client's report generation individually and report the mean, median and p95 (ms); also report the serialized report size per server and the total upload per client (bytes). Run once per encoding.

## A7. Leakage

Measured per run (`leakage` in the result line):

| Field | Meaning |
|---|---|
| `revealed_aggregates_total` | number of aggregate values the collector reconstructs during the run |
| `revealed_aggregates_per_level` | the same, per level or step |
| `revealed_kind` | what one revealed value is: `"count"`, `"bit"`, … |
| `client_validity_bits` | number of per-client accept/reject results revealed |
| `frontier_per_level` | surviving candidates per level |

Written analysis (`bench/LEAKAGE_<system>.md`): one table. Rows are what is revealed. Columns:

- to whom (each server, collector, network observer, clients);
- when (phase, level);
- how much (formula + measured value for `day_3of3` per encoding);
- category: **intended output**, **protocol leakage** accepted in the paper (cite the section), or **implementation-only** leakage (cite `file:line`).

Then a section on how the encoding changes the leakage.

## A8. Environment

- Same laptop for all runs: plugged in, high-performance power mode, heavy applications closed. All parties on localhost.
- Release builds only.
- Threads: each server process uses ⌊physical cores / 2⌋ threads (at least 1). The runner sets this and records it, and it stays fixed for the whole benchmark.
- Loop order: repetition outermost, so configurations interleave and thermal throttling spreads evenly.
- Report the median of the 5 measured runs, plus min and max.
- Record per run: CPU model, physical/logical cores, RAM, OS, compiler version, git commit and dirty flag.

## A9. Result schema

One JSON object per line in `bench/results/raw/<system>.jsonl`, where `system` is `mastic` or `mosaic`. Per-server values are `[server0, server1]`. Fields that don't apply to a system are 0 or `null`, never left out.

```json
{
  "schema_version": 1,
  "system": "mastic",
  "encoding": "geohash",
  "dataset": "day",
  "subset": "1of3",
  "n_clients": 7194,
  "rep": 2,
  "warmup": false,
  "timestamp": "2026-11-10T14:03:11+01:00",
  "params": {"data_bits": 21, "threshold_frac": 0.005, "threshold_abs": 35, "delta": null,
             "encoding_params": {"prefix": "9v6", "bits": 21}},
  "env": {"cpu": "...", "cores_physical": 8, "cores_logical": 16, "ram_gb": 16, "os": "...",
          "threads": 4, "compiler": "rustc 1.xx", "commit": "abc123", "dirty": false},
  "phases": {"server_offline_ms": 0, "upload_ms": 812.4, "online_ms": 4210.7,
             "online_steps": [{"name": "flp", "ms": 640.2}, {"name": "level_0", "ms": 151.3}]},
  "rounds": {"upload": 1, "online": 47, "extra": 0},
  "bytes": {"upload": {"client_to_server": [0, 0]},
            "online": {"collector_to_server": [0, 0], "server_to_collector": [0, 0],
                       "server_to_server": 0}},
  "leakage": {"revealed_aggregates_total": 694, "revealed_aggregates_per_level": [2, 4],
              "revealed_kind": "count", "client_validity_bits": 7194,
              "frontier_per_level": [1, 2]},
  "output": [{"label": "9v6s0j9", "lat": 30.265, "lon": -97.732, "count": 120}]
}
```

The client micro-benchmark writes the same header fields plus the following to `bench/results/raw/<system>_client.jsonl`:

```json
"client": {"k": 2000, "keygen_ms_mean": 0.41, "keygen_ms_median": 0.39, "keygen_ms_p95": 0.52,
           "report_bytes_per_server": [2210, 2210], "upload_bytes_per_client": 4420}
```

## A10. Outputs

- `bench/results/summary_<system>.csv`: one row per encoding × subset, with the median, min and max of every metric, modelled LAN/WAN times, client metrics, leakage counters and accuracy. **The columns are identical for both systems**, so the two files can be concatenated.
- `bench/results/tables.md`: timings, communication, client cost, accuracy, leakage.
- `bench/results/plots/` (PNG + PDF, one colour per encoding):
  1. online time vs N (LAN and WAN panels);
  2. bytes vs N;
  3. rounds per encoding;
  4. client time and report size per encoding;
  5. recall/precision per encoding;
  6. revealed aggregates vs N.
- **Comparison (at the end):** copy the other system's `summary_<system>.csv` into `bench/results/` and run `evaluate.py --combine <file>`. This produces the side-by-side tables and plots with one marker per system.

---

# Part B: Mastic specifics

## B1. Folder layout

```
mastic_for_fuzzy_HH/                 (branch `benchmarking`)
├── bench/
│   ├── BENCHMARK_MASTIC.md          ← this file
│   ├── README.md                    ← exact commands (written by Claude Code)
│   ├── data/                        ← subsets + manifest.json (generated here)
│   ├── ground_truth/                ← exact heavy cells per encoding and subset
│   ├── reference/                   ← reference hotspots, copied from the Mosaic harness
│   ├── scripts/
│   │   ├── bench_common.py          ← schema check, environment info, JSONL helpers
│   │   ├── make_subsets.py
│   │   ├── ground_truth.py
│   │   ├── run_bench.py
│   │   └── evaluate.py
│   ├── tests/test_evaluate.py
│   ├── requirements.txt             ← Python packages for the bench scripts
│   ├── reports/                     ← one report per Claude Code prompt (C0)
│   ├── results/raw/  results/plots/  summary_mastic.csv  tables.md
│   └── LEAKAGE_mastic.md
└── src/
    ├── bin/driver.rs                ← instrumented (--bench-out)
    └── bin/keygen_bench.rs          ← client micro-benchmark (new)
```

## B2. How the phases map onto the Mastic code

| Metric | Mastic code |
|---|---|
| Client report generation | `VidpfKey::gen(α, β)` + FLP `prove` + proof sharing (as in `generate_reports`, `driver.rs`) |
| Server offline | **0**: Mastic has no input-independent server preprocessing. Record 0 and say so in the report. |
| Upload | `add_reports` (all `add_report_shares` batches) |
| Online | `tree_init` → `run_flp_queries` + `apply_flp_results` → `data_bits − 1` × `run_level` (incl. Merkle search rounds) → `run_level_last` (incl. `get_proofs`) → `final_shares` + reconstruction |
| Online steps | `flp`, `level_0` … `level_{n−2}`, `last_level`, `final` |

## B3. Communication in Mastic

- **Topology as implemented:** the two servers never talk to each other; the driver relays everything and acts as client simulator, leader and collector. So `server_to_server` = 0. All traffic is `collector_to_server` and `server_to_collector`. State this in the report.
- **Rounds:** every driver call sent to both servers at once (`try_join!`) is one round. Merkle binary-search re-sends go into `rounds.extra`.
- **Bytes:** length of every request and response as tarpc actually sends it (bincode with varint encoding), per server.
- Sanity checks:
  - online rounds when `rounds.extra` = 0 and N ≤ `query_flp_batch_size`: tree_init + FLP query + apply FLP results + (n − 1) crawls + n prunes + crawl_last + get_proofs + final shares = **2n + 5** (45 Morton, 47 geohash, 49 grid, 53 H3). Confirmed by measurement in prompt 2;
  - upload rounds as implemented: ⌈N / 1000⌉ batches (8 / 15 / 22 for the three subsets). The model uses 1 round per client (A4);
  - per-server VIDPF key with fixed-length bincode ≈ 33 + n·(42 + 24·(m+1)) bytes: 1,833 B (Morton, n = 20), 1,923 B (geohash, n = 21), 2,013 B (grid, n = 22), 2,193 B (H3, n = 24). As actually sent (varint): 1,399 / 1,468 / 1,537 / 1,675 B. The FLP proof share (7 field elements) comes on top. Confirmed in prompt 2.

## B4. Accuracy in Mastic

- **Self-check:** exact heavy cells per encoding, using the same logic as `scripts/check_heavy_hitters.py`. Import its encoders; don't re-implement them.
- **Accuracy against the reference:** read `bench/reference/day_<subset>.json` once the Mosaic harness has produced it. Until then, `evaluate.py` skips these columns and says so.

## B5. Leakage in Mastic (starting list for `LEAKAGE_mastic.md`)

Verify each item against the code and the paper:

1. **Intended output:** the heavy cells and their counts.
2. **Protocol leakage:**
   - the count of every evaluated prefix (both children of every surviving node at every level, including pruned ones), i.e. counts of regions;
   - public parameters (N, threshold, `data_bits`).
3. **Revealed for verification:**
   - per-client FLP accept/reject;
   - per-client outcomes of the Merkle search (which clients failed the checks).
4. **Implementation only:**
   - the constant control bits in `prg.rs` `expand_dir` (issue S1). If not fixed in the benchmarked commit, each server can read every client's α directly from the correction words. Record whether the benchmarked commit has the fix.
   - the driver acts as client simulator, leader and collector;
   - RPC is unencrypted and unauthenticated (a network observer sees both servers' keys).
5. **Encoding effect:** intermediate counts are aligned blocks (grid, geohash, Morton) or approximate "snowflake" regions (H3). The number of revealed counts per run comes from the leakage counters.

## B6. Rough cost on a laptop (estimate from reading the code; measure it)

- Each client is evaluated at roughly 700–750 tree nodes over the whole crawl, per server. For `day_3of3` that is about 16 million VIDPF evaluations per server. Expect tens of seconds to a few minutes per run, depending on cores.
- Memory peaks at roughly 0.5 GB per server for `day_3of3` (frontier + children + the `prev_frontier` copy). That's fine on 8 GB.
- The month sample would need several GB per server; that's why it isn't used.
- The timing run in prompt 2 replaces these estimates with real numbers.

## B7. Validation checklist (before the full run)

- [ ] `bench/data/manifest.json` exists; the subset row counts are 7,194 / 14,387 / 21,581.
- [ ] Smoke run on `data/synthetic/ten_locations.csv` (threshold 0.1): 4 cells with counts 50/40/30/20 for every encoding.
- [ ] Every result line passes the schema check.
- [ ] Key sizes and round counts match B3.
- [ ] Self-check is 1.0 / 1.0 / exact for every run.
- [ ] One `day_3of3` run per encoding has been timed, so the total duration can be estimated.
- [ ] Encoding parameters equal A2.

---

# Part C: Running it

## C0. Rules for Claude Code (apply to every prompt)

1. **No git operations that change anything.** Do not run `git commit`, `git push`, `git checkout`, `git switch`, `git branch`, `git reset`, `git stash`, `git merge`, `git rebase`, `git restore` or `git clean`. Read-only git (`status`, `log`, `diff`, `show`, `rev-parse`) is fine. Leave all changes uncommitted. I commit and push myself.
2. **Stay on the current branch** (`benchmarking`). If it looks wrong, stop and tell me.
3. **This file is the source of truth.** Ask me before deviating from it. If something in the code doesn't fit it, explain the mismatch and propose a resolution instead of silently choosing.
4. **Don't change protocol behaviour.** Measurement code only. List every existing file you modify and why.
5. **Don't start the full benchmark.** Run only the smoke runs and single timing runs each prompt asks for.
6. **Windows.** Scripts must run in PowerShell with the repo's `.venv` Python. No bash, no shell redirection. Write output files directly from Python or Rust.
7. **Report at the end of every prompt.** Write `bench/reports/NN-<name>.md` and show the same text in the chat, with these sections:
   - **Summary:** what was done, in plain words; 3–6 sentences.
   - **Files:** created and modified, one line each on why.
   - **How to run:** exact commands.
   - **Checks:** each check from this file, with the numbers and pass/fail.
   - **Deviations and open questions:** anything that differs from this file or needs my decision.
   - **What to commit:** the exact `git add …` command (specific paths, never `git add .`) and a one-line commit message. Leave out `.venv/`, `__pycache__/`, `target/` and temporary files.