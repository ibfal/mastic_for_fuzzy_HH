# 02: Instrumentation

## Summary

The driver can now write one A9 result line per run (`--bench-out`, `--bench-meta`). Each line
holds:
- the phase and step timings;
- rounds and bytes per phase;
- the leakage counters;
- the heavy cells.

Without the flags it behaves exactly as before, and `--export` is unchanged. A new binary,
`keygen_bench`, times one client's report generation on one thread (A6). `run_bench.py` drives the
whole matrix: build, configs, servers, the driver, the checks, cleanup and progress.

All checks pass:
- smoke runs exact for all four encodings;
- online rounds 2n + 5;
- key sizes equal to B3's formula;
- every line valid against the schema;
- `--export` and `build_viz.py` unchanged.

One `day_3of3` run takes about 5–6 s with ~0.6 GiB per server, so the full benchmark should take
about **5 minutes**. The protocol code (`vidpf.rs`, `prg.rs`, `collect.rs`, `server.rs`) is
untouched.

## Files

Created:
- `src/bench.rs`: shared by the driver and `keygen_bench`. Holds the `--bench-meta` labels, the A9
  header, message sizes (`wire_size`, `fixint_size`), a UTC timestamp, the JSONL append, and 3 unit
  tests.
- `src/bin/keygen_bench.rs`: the A6 client micro-benchmark. It also checks that the servers' own
  FLP code accepts the generated report.
- `bench/scripts/run_bench.py`: the benchmark runner.
- `bench/reports/02-instrumentation.md`: this report.

Modified:
- `src/bin/driver.rs`: the instrumentation.
  - A `Bench` meter is passed alongside the existing `&mut Option<CrawlExport>`.
  - Every RPC call records its round and message sizes.
  - Stopwatches cover upload and each online step. Time spent measuring is subtracted.
  - `CrawlExport` gains a `regions` switch (outlines only for `--export`) and a `heavy_hitters()`
    method, now shared by `write_export` and the result line.
  - The RPC sequence and the requests are unchanged.
- `src/config.rs`: the `--bench-out` / `--bench-meta` flags (driver only; clap requires both
  together).
- `src/lib.rs`: `pub mod bench;`.
- `Cargo.toml`: `[[bin]] keygen_bench`. No new dependencies.
- `.gitignore`: `bench/.run/`, the runner's configs, logs, smoke and check results.
- `bench/README.md`: sections 4 (runner) and 5 (driver flags, `keygen_bench`), plus layout rows.

Not touched:
- `src/vidpf.rs`, `src/prg.rs`, `src/collect.rs`, `src/bin/server.rs`: measurement did not need
  them;
- `src/configs/`, `bench_common.py`, `scripts/build_viz.py`.

## How to run

```powershell
# smoke runs (all encodings) + client micro-benchmark; results in bench\.run\smoke\
.\.venv\Scripts\python.exe bench\scripts\run_bench.py --smoke
# one day_3of3 run per encoding, kept out of the results
.\.venv\Scripts\python.exe bench\scripts\run_bench.py --only-subset 3of3 --reps 1 --no-warmup --skip-keygen --results-dir bench\.run\check
# the full benchmark (not run yet): 72 runs + 4 client runs into bench\results\raw\
.\.venv\Scripts\python.exe bench\scripts\run_bench.py
```

Options: `--reps`, `--no-warmup`, `--smoke`, `--only-encoding`, `--only-subset`, `--threads`,
`--port-base` (default 18100; 8000 is refused), `--results-dir`, `--skip-keygen`, `--no-build`,
`--dry-run`.

By hand:
```powershell
.\target\release\driver.exe -c <config> -n <rows> --bench-out out.jsonl --bench-meta "@meta.json"
.\target\release\keygen_bench.exe -c bench\.run\configs\keygen-geohash.toml
```

Inline JSON for `--bench-meta` fails in Windows PowerShell 5.1, which strips the quotes, so pass
`"@file"`.

## Checks

**Smoke runs** (`ten_locations.csv`, threshold 0.1, N = 165, T = 16) against
`bench/ground_truth/smoke_ten_locations.json`:

| Encoding | Cells | Counts | P / R / exact | Centres vs ground truth | Online rounds | Status |
|---|---|---|---|---|---|---|
| `mosaic_grid` | 4 | 50/40/30/20 | 1.0 / 1.0 / yes | < 1e-6° | 49 | pass |
| `geohash` | 4 | 50/40/30/20 | 1.0 / 1.0 / yes | < 1e-6° | 47 | pass |
| `h3` | 4 | 50/40/30/20 | 1.0 / 1.0 / yes | < 1e-6° | 53 | pass |
| `morton` | 4 | 50/40/30/20 | 1.0 / 1.0 / yes | < 1e-6° | 45 | pass |

**Online rounds.** Every RPC the driver makes in a normal run (no `--malicious`,
N ≤ `query_flp_batch_size` = 50,000):

| RPC | Phase | Rounds |
|---|---|---|
| `reset` | setup (before upload) | 1, not counted in the schema |
| `add_report_shares` | upload | ⌈N / 1000⌉: 1 / 8 / 15 / 22 (smoke / day subsets) |
| `tree_init` | online | 1 |
| `run_flp_queries` | online | ⌈N / 50,000⌉ = 1 |
| `apply_flp_results` | online | 1 |
| `tree_crawl` | online | n − 1, plus 1 extra per Merkle re-send |
| `tree_prune` | online | n (n − 1 levels + the last level) |
| `tree_crawl_last` | online | 1 |
| `get_proofs` | online | ⌈N / 50,000⌉ = 1 |
| `final_shares` | online | 1 |

That is 1 + 1 + 1 + (n − 1) + n + 1 + 1 + 1 = **2n + 5**. The measured counts are 45 / 47 / 49 / 53
for Morton / geohash / grid / H3 (n = 20 / 21 / 22 / 24), in the smoke runs and the `day_3of3`
runs, with `extra` = 0: **pass**.

**2n + 5 is right.** The 01 report listed the same calls (3 + 2(n − 1) + 4) but summed them wrongly
to 2n + 3. B3 in the spec file says "≈ 2n + 4 (≈ 44 for n = 20)", also one short. In general the
count is 2n + 3 + 2⌈N / 50,000⌉.

**Key sizes (B3)**, per server, from `keygen_bench` and the driver's own `VIDPFKey size` printout:

| Encoding | n | VIDPF key, `bincode::serialize` | B3 formula 33 + n(42 + 24·2) | Key as sent (varint) | FLP proof share | Report share per server (serialize / as sent) |
|---|---|---|---|---|---|---|
| `morton` | 20 | 1,833 | 1,833 | 1,399 | 176 / 120 | 2,061 / 1,568 |
| `geohash` | 21 | 1,923 | 1,923 | 1,468 | 176 / 120 | 2,151 / 1,637 |
| `mosaic_grid` | 22 | 2,013 | 2,013 | 1,537 | 176 / 120 | 2,241 / 1,706 |
| `h3` | 24 | 2,193 | 2,193 | 1,675 | 176 / 120 | 2,421 / 1,844 |

All four equal the formula with m = 1: **pass**. Notes:
- The keys as actually sent are 19 + 69n bytes (see Deviation 3).
- The FLP proof share is 7 field elements.
- The upload bytes confirm the sizes: 165 × 1,637 + 1 = 270,106 B per server (geohash smoke).

**Schema check** (`bench_common.validate_result`): 12 of 12 lines valid. That is 4 smoke lines,
4 `day_3of3` lines and 4 client lines: **pass**. The runner checks every line before appending it.
Smoke lines are checked with their `subset` value `smoke` substituted (Deviation 7).

**Driver unchanged without the flags / `--export`** (original binaries from `HEAD` vs new, all four
smoke configs):

| Check | Result | Status |
|---|---|---|
| Stdout without flags (timings masked) | identical | pass |
| `--export` JSON, timings aside | identical, same key order | pass |
| `--export` together with `--bench-out` | identical | pass |
| Stdout with `--export` | identical except the output file name chosen by the test | pass |
| `build_viz.py` on a new-driver export | accepted: listed in the manifest and inlined in `viz/dist/mastic-crawl.html`; `viz/` restored byte-identical afterwards | pass |
| `cargo test --release` | 39 passed, 0 failed (incl. 3 new) | pass |

**Failure handling:**
- `--port-base 7999` is refused, because it would use 8000.
- A busy port (8088/8089, Splunk) gives a clear error.
- A failing driver and a simulated Ctrl+C both leave no server or driver process behind, and the
  ports are free afterwards.

**Timing run: one `day_3of3` run per encoding** (5 threads per server, N = 21,581, T = 107; self-check
1.0 / 1.0 / exact for all four):

| Encoding | Online s | Upload s | Driver wall s | Run incl. server start/stop s | Peak memory s0 / s1 / driver (MiB) | Cells |
|---|---|---|---|---|---|---|
| `mosaic_grid` | 4.60 | 0.25 | 5.09 | 5.69 | 605 / 605 / 171 | 8 |
| `geohash` | 4.22 | 0.24 | 4.69 | 5.23 | 644 / 643 / 166 | 10 |
| `h3` | 4.43 | 0.28 | 4.96 | 5.57 | 600 / 600 / 179 | 15 |
| `morton` | 4.83 | 0.31 | 5.39 | 6.00 | 604 / 603 / 162 | 8 |

More from these runs:
- **Online steps:** the steps add up to `online_ms` within 3 ms. `flp` takes ~0.12 s,
  `last_level` 0.14–0.21 s, `final` < 1 ms.
- **Measuring overhead:** 31–43 ms per run (< 1 % of online) was subtracted.
- **Rounds:** 22 upload rounds; online 49 / 47 / 53 / 45.
- **Bytes per server:**
  - upload 33.8–39.8 MB;
  - online collector → server 22.4 KB, mostly the FLP keep vector;
  - online server → collector 2.59 MB, mostly FLP verifier shares (~103 B per client) and final
    proofs (16 B per client);
  - server ↔ server 0.
- **Leakage:**
  - revealed counts 746 / 730 / 754 / 720 per run;
  - client validity bits 43,162 = 2N;
  - frontier ends at 8 / 10 / 15 / 8.

**Estimate of the full benchmark.** The work per run is linear in N, because T scales with N and
the frontier stays similar. Each run also has ~0.6 s of fixed cost (server start and stop). That
gives ~47 s per repetition over 4 encodings × 3 subsets, so 6 repetitions take **≈ 4.7 min**.
Adding the client benchmark (~5 s) and the build gives about 5 minutes. The upper bound, if every
run took as long as `day_3of3`, is 6.7 min.

**B6 revisited:**
- Revealed counts per run (720–754) confirm the "700–750 tree nodes" estimate.
- Runs are much faster than estimated: ~4.5 s online, not "tens of seconds to minutes".
- Memory is ~0.6 GiB per server, near the 0.5 GB estimate. All parties together peak at ~1.5 GiB.

**Client micro-benchmark** (A6; 2,000 rows of `day_3of3`, 1 thread, after a warm-up pass; final
binaries):

| Encoding | Mean | Median | p95 (ms) | Report per server | Upload per client |
|---|---|---|---|---|---|
| `mosaic_grid` | 0.0230 | 0.0213 | 0.0249 | 1,706 B | 3,412 B |
| `geohash` | 0.0214 | 0.0205 | 0.0240 | 1,637 B | 3,274 B |
| `h3` | 0.0241 | 0.0227 | 0.0273 | 1,844 B | 3,688 B |
| `morton` | 0.0206 | 0.0200 | 0.0234 | 1,568 B | 3,136 B |

About half the time is `VidpfKey::gen` (~11–13 µs) and half the FLP joint randomness, proof and
split (~12 µs). Encoding takes 0.1–0.6 µs. The servers' code accepts the generated reports.

**B7 checklist:**
- [x] manifest;
- [x] smoke runs;
- [x] schema;
- [x] key sizes and rounds;
- [x] self-check for every run so far (all are run by the runner);
- [x] `day_3of3` timed;
- [ ] encoding parameters equal A2: still pending your A2 update from 01.

## Deviations and open questions

1. **Online rounds: please change B3 to 2n + 5.** Suggested text: "online rounds = 2n + 5 when
   `rounds.extra` = 0 and N ≤ `query_flp_batch_size` (45 Morton, 47 geohash, 49 grid, 53 H3); the
   `reset` call before the upload is setup and not counted." The `reset` round (17 B out, 5 B back
   per server) and the upload "Done" replies (5 B per batch) have no field in A9. They are printed
   in the driver log only.
2. **Upload rounds = ⌈N / 1000⌉ (8 / 15 / 22), not 1 (decision needed).** The driver awaits its
   batches of `add_report_share_batch_size` = 1000 one after another. The futures in the
   `reqs_in_flight` loop only start when awaited. So by A4's definition each batch is a round, and
   I record the measured count. In a real deployment each client uploads once, in parallel (1
   round). This matters for the WAN model: 22 × 50 ms = 1.1 s. Keep the measured count, or record
   1?
3. **Bytes are counted as sent (decision to confirm).**
   - tarpc serializes with `bincode::DefaultOptions` (varint lengths). So I count that encoding,
     which is A4's "application-level size of every serialized message".
   - B3's key formula assumes `bincode::serialize` (8-byte lengths). That gives sizes ~24 % larger:
     1,923 vs 1,468 B for a geohash key.
   - Switching is one line (`wire_size` in `src/bench.rs`).
   - Suggested B3 key bullet: "per-server VIDPF key = 33 + n·(42 + 24·(m+1)) B with
     `bincode::serialize` (1,833 / 1,923 / 2,013 / 2,193 B for n = 20 / 21 / 22 / 24), and
     19 + n·(35 + 17·(m+1)) B as sent (1,399 / 1,468 / 1,537 / 1,675 B); the report share adds the
     nonce, the FLP proof share (7 elements) and the joint-randomness parts: 1,568–1,844 B per
     server as sent."
4. **Leakage counters: choices to confirm.**
   - `revealed_aggregates_per_level` has n entries: the counts reconstructed at each level
     (2 × the previous frontier), times the number of `tree_crawl` calls at that level.
   - Kept and pruned have no fields of their own, because the schema rejects extra fields.
     Kept = `frontier_per_level`; pruned = revealed − frontier.
   - The total does **not** include `final_shares`. That call reconstructs the |output| counts
     (8 / 10 / 15 / 8) a second time, and they were already revealed at level n.
   - `revealed_kind` is `"count"`. Each revealed value is the vector [bucket, counter], equal for a
     constant weight.
   - `client_validity_bits` = 2N: one FLP decision and one final-proof comparison (`get_proofs`)
     per client. The n − 1 Merkle-root comparisons in `tree_crawl` each cover all clients, so they
     are not counted. The driver log prints them.
5. **Online steps.** `tree_init` is counted in `flp`, so the step names are exactly B2's.
   - Observation, unchanged: the servers' `run_flp_queries` evaluates level 1 for every client and
     discards it, and `level_0` evaluates it again. So `flp` contains one VIDPF level.
   - Upload time includes the driver building each batch's shares (cloning the keys), as the
     driver always did.
6. **Client micro-benchmark.**
   - The timing includes encoding the location, because A3 lists it. B2 and your prompt don't, but
     it is < 3 % of the time.
   - The proof split uses a plain iterator instead of `par_iter`: the same work on one thread.
   - Client lines record `n_clients` = 2000 and `env.threads` = 1. `threshold_abs` is 0, because
     the threshold doesn't apply.
7. **Smoke lines** use dataset `synthetic` and subset `smoke`.
   - The schema's subset list only allows `1of3`/`2of3`/`3of3`, so the runner validates them with
     that one field substituted.
   - I didn't change `bench_common.py`.
   - They go to `bench/.run/smoke/`, never to `bench/results/raw/`.
8. **Threads.** Only the servers get `RAYON_NUM_THREADS` = 5 (A8). The driver runs unrestricted.
   Its report generation is not timed, and during the online phase it only combines shares. Say if
   you want it limited too.
9. **Runner choices.**
   - The servers listen on `127.0.0.1`, not `0.0.0.0` as in `src/configs`.
   - Ctrl+C was tested by raising `KeyboardInterrupt` during the driver wait, not with a real key
     press.
10. **Before the full run.**
    - Commit first: every line so far records `dirty: true`.
    - When I checked, only 3.4 GiB of 15.8 GiB RAM were free; the benchmark needs ~1.5 GiB. Close
      heavy applications (A8).
    - A2 still lists geohash 20 bits / H3 resolution 9 (01, Deviation 1).

## What to commit

```
git add .gitignore Cargo.toml src/lib.rs src/config.rs src/bench.rs src/bin/driver.rs src/bin/keygen_bench.rs bench/scripts/run_bench.py bench/README.md bench/reports/02-instrumentation.md
```

Commit message: `Instrument the driver (--bench-out), add keygen_bench and the benchmark runner`
