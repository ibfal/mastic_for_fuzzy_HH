# Leakage of the benchmarked Mastic implementation

This document follows the structure of A7 in [`BENCHMARK_MASTIC.md`](BENCHMARK_MASTIC.md). It covers
the code that produced the benchmark results: commit `5efbdeb` on `benchmarking`. `src/` is
identical at `5efbdeb` and at the current HEAD.

## Sources

| Source | What it is |
|---|---|
| Code | `file:line` references are to that commit. |
| Paper | D. Mouris, C. Patton, H. Davis, P. Sarkar, N. G. Tsoutsos, *Mastic: Private Weighted Heavy-Hitters and Attribute-Based Metrics*: the full version with appendices A–D, 44 pages (`bench/reports/mastic.pdf`, not tracked by Git). "§" refers to that PDF. |
| Measured | `bench/results/summary_mastic.csv`, subset `day_3of3` (N = 21,581, T = 107), medians over 5 runs. Min = max for every leakage counter. Merkle comparisons and log contents are from the runner's driver and server logs in `bench/.run/logs/`. |

Per-encoding values are always given in the order
**`mosaic_grid` / `geohash` / `h3` / `morton`** (n = 22 / 21 / 24 / 20).

## Status tags

| Tag | Meaning |
|---|---|
| **[code]** | verified by reading the code at the cited lines |
| **[measured]** | the number comes from the benchmark results or logs |
| **[tested]** | confirmed by running a test |
| **[inferred]** | follows from the code or the paper, but I did not verify it by test or measurement |

## Parties

| Party | Who it is |
|---|---|
| **S0, S1** | the two servers (`src/bin/server.rs`) |
| **C** | the driver (`src/bin/driver.rs`). It plays the clients, relays every message between the servers, and is the collector. In the paper there is no such third party: S0 and S1 exchange messages directly and both reconstruct the aggregates (§3.1, Fig. 2). Wherever the paper gives something to "the aggregators", this implementation gives it to C (B3). |
| **O** | a network observer on the links between C and the servers |
| **Clients** | they receive nothing back. Their `add_report_shares` reply is the string `"Done"` (`server.rs:69`); in the paper they go offline after uploading (§3.2.1). Clients are left out of the table below. |

## Measured leakage, `day_3of3`

| Counter | Formula | `mosaic_grid` | `geohash` | `h3` | `morton` |
|---|---|---:|---:|---:|---:|
| Revealed prefix counts | 2 · (1 + Σ_{k=1}^{n−1} \|HH_k\|) | 746 | 730 | 754 | 720 |
| … of which kept | Σ_{k=1}^{n} \|HH_k\| | 380 | 374 | 391 | 367 |
| … of which pruned (count < T, revealed exactly) | revealed − kept | 366 | 356 | 363 | 353 |
| Output cells | \|HH_n\| | 8 | 10 | 15 | 8 |
| Widest frontier | max_k \|HH_k\| | 39 | 43 | 39 | 39 |
| FLP decisions (per client) | N | 21,581 | 21,581 | 21,581 | 21,581 |
| Final-proof comparisons (per client) | N | 21,581 | 21,581 | 21,581 | 21,581 |
| `client_validity_bits` | 2N | 43,162 | 43,162 | 43,162 | 43,162 |
| Merkle-root comparisons (each over all clients) | n − 1 | 21 | 20 | 23 | 19 |
| Clients flagged by the Merkle search | — | 0 | 0 | 0 | 0 |
| Inputs α readable by **each** server via S1 | N × n bits | 21,581 × 22 bits | 21,581 × 21 bits | 21,581 × 24 bits | 21,581 × 20 bits |

- HH_k is the set of prefixes kept at level k, with |HH_0| = 1 (the root).
- The formula for the revealed counts matches all four measurements. For example, for
  `mosaic_grid`: 380 − 8 = 372, and 2 · (1 + 372) = 746.
- As decided in report 02, `revealed_aggregates_total` does not count `final_shares`, which reveals
  the output counts a second time.

## The leakage table

| # | What is revealed | To whom | When | How much: formula; measured | Category and source | Status |
|---|---|---|---|---|---|---|
| 1 | **Heavy cells and their counts** | **C**: paths and counts, twice (`tree_crawl_last`, then `final_shares`). **S0, S1**: the paths only, without counts. In the paper, both aggregators get both. | online, `last_level` and `final` | \|HH_n\| cells. Each count is the vector [Σβ, count] (see note 2). **8 / 10 / 15 / 8** | **Intended output**: Fig. 2 step 3, §3.1 footnote 10, §5.1. Code: `driver.rs:1137-1158`, `driver.rs:1222-1242`, `collect.rs:608-643` | [code] [measured]; equals the plaintext answer in all 72 runs (report 03) |
| 2 | **The count of every evaluated prefix**: both children of every kept prefix at every level, including the pruned ones (regions with fewer than T rides) | **C** only. Each server holds just its own share. In the paper, both aggregators get the counts. | online, every level k = 1 … n | 2 · (1 + Σ_{k<n} \|HH_k\|) vectors of m + 1 = 2 field elements. **746 / 730 / 754 / 720**, of which pruned (exact sub-threshold counts) **366 / 356 / 363 / 353** | **Protocol leakage**: Fig. 2 step 2b.ii ("S0 and S1 recover weight_γ"); §3.1 footnote 10 ("histograms at intermediate steps may be considered additional leakage … inherent"); §5.1. The counter element: §6. Code: `collect.rs:360-376`, `collect.rs:549-552`, `driver.rs:1050-1055`, `driver.rs:1153-1158`, counter `vidpf.rs:232-233` | [code] [measured] |
| 3 | **Kept/pruned decision for every evaluated prefix**, i.e. the heavy-prefix set HH_k of every level | **S0, S1** (the `keep` vector of `tree_prune`), **C** | online, after each level | one bit per evaluated prefix: **746 / 730 / 754 / 720** bits. Kept **380 / 374 / 391 / 367**; pruned **366 / 356 / 363 / 353** | **Protocol leakage**: Fig. 2 step 2b.ii (the aggregators build HH_{k+1}). In the paper they learn more: the counts themselves. Code: `driver.rs:1097-1101`, `driver.rs:1202-1206`, `rpc.rs:52`, `collect.rs:564-573` | [code] [measured] |
| 4 | **Public parameters**: N, n, T, the encoding and its parameters, `hist_buckets` and the weight type | S0, S1, C, O | setup and upload | N = **21,581**; n = **22 / 21 / 24 / 20**; T = ⌊0.005 · N⌋ = **107**; encoding parameters as in A2 | **Protocol (public)**: Fig. 2 inputs (T, n, Reports = [N]); §3.2.1 (the weight language L is agreed in advance). Code: N = number of stored shares (`collect.rs:185-187`); n = number of correction words per key (`vidpf.rs:471-473`); T computed by C (`driver.rs:1004-1005`) from the config that both servers also read | [code] |
| 5 | **Each client's weight-check (FLP) result** | **C**, which also sees both verifier shares σ0, σ1 (`driver.rs:941`, `driver.rs:953-964`). **S0, S1** get the decision (`keep`). | online, step `flp` | N decisions: **21,581**, all accepted | **Protocol leakage (verification)**: Fig. 2 step 1c; Appendix C Fig. 10 ("Honest Aggregator Validation" returns `Decide` per client). Zero-knowledge (§2.4, Appendix B): nothing beyond validity. Code: `driver.rs:953-967`, `driver.rs:977-980`, `collect.rs:577-587` | [code] [measured]. That a third party seeing σ0 + σ1 learns nothing more is [inferred] from the ZK property |
| 6 | **Path and one-hot check per level** (Merkle-root comparison) | **C** compares the roots. **S0, S1** learn the outcome only on a mismatch, through the block indices in the next `tree_crawl` request (`rpc.rs:31`). | online, levels 1 … n − 1 | **Honest run:** n − 1 comparisons, each saying "all remaining clients pass": **21 / 20 / 23 / 19**. **On a mismatch:** a binary search reveals the failing clients to C, S0 and S1. Measured: **0** mismatches, `rounds.extra` = 0 | **Protocol leakage (verification)**: Fig. 2 step 2a.v; §3.2.4 (Merkle batching and the search). Per-client outcomes are within the model (§5.1, Appendix C Fig. 10). Code: `collect.rs:431-511`, `collect.rs:345-355`, `driver.rs:1057-1086` | [code] [measured]: no "Detected malicious" in the 72 `day_3of3` logs |
| 7 | **Each client's last-level check** (final VIDPF proofs) | **C** only. Both servers' proofs are compared in the driver; nothing is sent back, and a mismatch aborts the run. | online, `last_level` (`get_proofs`) | N proofs per server, N comparisons: **21,581**, all equal | **Protocol leakage (verification)**: Fig. 2 step 2a.v at level n. Per-client proofs: Appendix C Fig. 10. Equal proofs carry no information (Appendix A: "both parties compute the same proof string"). Code: `collect.rs:532-545`, `collect.rs:555-562`, `driver.rs:1172-1197` | [code] [measured] |
| 8 | **Every client's input α, read from the correction words (issue S1)** | **S0 and S1**, each from its own key. **O**, from either link. | upload: as soon as a report share arrives, before any aggregation | all N inputs, n bits each: **21,581 × 22 / 21 / 24 / 20 bits per server**. With α known, each server can also compute rows 1–3 itself [inferred]. β stays secret-shared [inferred] | **Implementation-only**. `prg.rs:71` clears the two low bits; `prg.rs:77` reads them, so the control bits are always (true, true). Then `vidpf.rs:207-210` gives the correction-word bits (¬αᵢ, αᵢ). Both keys carry the correction words (`vidpf.rs:287-298`; the paper's public share, §2.3 footnote 8). This breaks VIDPF privacy (§2.3, Appendix A). **Not fixed in `5efbdeb`.** | [code] [tested]: 4 of 4 control-bit tests fail; α read off one key in 50 of 50 trials |
| 9 | **All measurements in plaintext, the verification key, and every verification message**: the driver is client simulator, leader and collector at once | **C** | from report generation to the end | all N (α, β) in plaintext; the 16-byte verification key; σ for N clients; all count shares; all proofs | **Implementation-only**. `driver.rs:679-745` generates every report from plaintext α; `driver.rs:1563-1566` and `driver.rs:797-812` choose the verification key and send it to the servers; the servers never contact each other. Paper: clients send shares directly and go offline (§3.2.1); the leader S0 picks the key (§3.2.2); the aggregators exchange σ and R directly (Fig. 2) | [code] |
| 10 | **All traffic in clear and unauthenticated**: both report shares, all count shares, every `keep` vector, the verification key | **O**: anyone on a path between C and the servers. Also anyone who can reach a server port can call any RPC. | all phases | everything in rows 1–9, including every α **and** β (from the two keys together, even if S1 were fixed). Message sizes also reveal N, n and \|HH_k\| per level; those would remain visible even with encryption [inferred] | **Implementation-only**. Plain TCP with bincode: `server.rs:340`, `driver.rs:1502-1507`. No TLS dependency (`Cargo.toml:36`). The server accepts any connection (`server.rs:339-357`). all 29 `src/configs/*.toml` listen on `0.0.0.0:8000/8001`; the benchmark used `127.0.0.1`. The paper assumes secure channels between clients and aggregators (§3.1) | [code]. Active misuse (e.g. calling `final_shares` or `add_report_shares` from outside): [inferred] |
| 11 | **Counts revealed before the level's check completes, and again after a Merkle mismatch** | **C** | online, any level with a Merkle mismatch | Each re-send reveals the level's counts again. The last re-send removes the flagged clients, so the difference between two sets of counts shows the flagged clients' contribution to each prefix. Measured: **0** (no mismatch in any run) | **Implementation-only**. `tree_crawl` returns the counts together with the Merkle roots (`collect.rs:373-376`, `collect.rs:516`) and re-crawls over the reduced report set (`collect.rs:345-355`; `driver.rs:1021-1087`). Fig. 2 checks first (step 2a) and aggregates afterwards (step 2b); Appendix C Fig. 10 allows one aggregation per prefix (`Tagg[p]`) | [code]. Consequence [inferred]: a malicious server could fake a mismatch for one honest client and so expose that client's α at that level. Not exercised |
| 12 | **Indices of rejected clients in the server logs** | whoever reads the server logs | step `flp`; Merkle removals | one log line per rejected client. Measured: **0** lines in the 72 `day_3of3` server logs | **Implementation-only**. `collect.rs:584` and `collect.rs:351` (`println!`). The servers already know this from rows 5 and 6; the logs only make it persistent. | [code] [measured] |

### Notes on the rows

1. **The output, by party (row 1).**
   - C reconstructs each output cell's count once at `last_level` and once more from
     `final_shares`; the second time adds nothing new.
   - The servers know which cells are output, because they keep the frontier, but they never see
     the counts.
2. **What one revealed value is (rows 1–2).**
   - It is the vector [Σβ, count] of m + 1 field elements, with m = `hist_buckets` = 1. The paper's
     evaluation appends a counter the same way (§6: "the last index of the aggregated weight counts
     the number of reports"). Here, `gen_cor_word` pushes the 1 (`vidpf.rs:232-233`).
   - With the benchmark's constant weight (β = [1], `dataset.rs:27-38`) both elements equal the
     count, which is why `revealed_kind` is `"count"`.
   - With real weights, every evaluated prefix would also reveal its weight sum [inferred from
     `keep_values`, which C applies to the full vectors].
3. **Pruned counts are revealed exactly (row 2).** Each pruned prefix's count, between 0 and T − 1
   = 106, is revealed. These are counts of sub-threshold regions next to the heavy ones. The paper
   accepts this (§3.1 footnote 10).
4. **What the servers learn (row 3).** In this implementation the servers learn less than the
   paper's aggregators: only which prefixes are heavy, not their counts. All counts are
   concentrated at C instead.
5. **Validity bits (rows 5–7).** `client_validity_bits` = 2N = row 5 + row 7. The n − 1 Merkle
   comparisons of row 6 each cover all clients, so they are not per-client bits. They are printed in
   the driver log ("Merkle root comparisons 21" etc.).
6. **What S1 does (row 8).**
   - `expand_dir` should return two pseudorandom control bits. It returns (true, true) for every
     seed.
   - In `gen_cor_word` the correction-word bits are (t₀,L ⊕ t₁,L ⊕ αᵢ ⊕ 1, t₀,R ⊕ t₁,R ⊕ αᵢ).
     With constant control bits that is (¬αᵢ, αᵢ).
   - So the second bit of the i-th correction word **is** αᵢ, in both servers' keys.
   - The off-path control bits a server computes during evaluation are also constant (`true`). So
     each server could find α a second way: by evaluating its own key.
7. **Rows 9 and 10 are artefacts of the benchmark harness.** They make the deployment model weaker
   than the paper's (§3.1), but they are not part of Mastic. Row 10 also means that fixing S1 alone
   would not protect the inputs from a network observer.

### Checked and not leakage

| Item | Finding | Status |
|---|---|---|
| `PrgStream::fill_bytes` prints its key stream (`prg.rs:175`, `182`, `193`) | Nothing calls `PrgSeed::to_rng` or `PrgStream` (grep over `src/`), so this code never runs. | [code] |
| Constant server seed `[1u8; 16]` (`server.rs:331`) | Passed to `KeyCollection::new` as `_seed` (`collect.rs:167`) and unused. | [code] |
| FLP joint randomness is not checked (`collect.rs:286-290`, the code's own NOTE) | A robustness problem: a malicious client can choose joint randomness that passes. Not a privacy leak. | [code] |
| The verification key is known to the client simulator (row 9) | A robustness concern: a client that knows the key could craft proofs that pass [inferred]. No privacy effect beyond row 9. | [code] |
| `--export` writes every revealed count to a JSON file (`driver.rs:1091`, `1161`, `1431`) | Not used in the benchmark. The result lines (`bench/results/raw/mastic.jsonl`) hold only the output and the counters above. | [code] |

## Issue S1: is it fixed in the benchmarked commit?

**No.** The benchmarked commit `5efbdeb` contains the constant control bits. The evidence:

1. **The code.** `src/prg.rs:71` clears the two low bits (`key_short[0] &= 0xFC`), and
   `src/prg.rs:77` then reads exactly those bits. The lines are identical in
   `git show 5efbdeb:src/prg.rs` and at HEAD.
2. **No fix exists on any branch.**
   - The last change to `src/prg.rs` on any branch is `c45e1d3`
     (`git log --all -- src/prg.rs`), an ancestor of `5efbdeb`.
   - The branch `security-tests` (commit `4108699`) adds only the tests and states that "prg.rs and
     vidpf.rs are not changed here".
3. **The tests.** I ran the four tests from `security-tests:tests/security_control_bits_test.rs`
   against the benchmarked source. I copied the file in temporarily and deleted it afterwards.
   **All four fail:**

   | Test | Result |
   |---|---|
   | `prg_control_bits_vary_across_seeds` | `t_L = true` in 1,000 of 1,000 seeds |
   | `correction_word_bits_are_independent_of_alpha` | `CW.t_L == αᵢ` in 0 of 3,200 samples, i.e. CW.t_L = ¬αᵢ always |
   | `single_key_does_not_reveal_alpha` | α read off one key's correction words in **50 of 50** trials |
   | `off_path_control_bits_are_not_constant` | off-path bit `true` in 500 of 500 (server 0, level 1) |

**Consequence for the benchmark.** The timings, bytes and accuracy are valid as measurements of
this code. The fix should not change them [inferred]: it only changes which bits are read, so
message sizes and work stay the same. Privacy, however, is broken: in every run, each server could
read all 21,581 inputs.

## How the encoding changes the leakage

The encoding decides two things: what region each revealed count describes, and how many counts
are revealed at each level.

| | `mosaic_grid` | `geohash` | `h3` | `morton` |
|---|---|---|---|---|
| n | 22 | 21 (after the fixed 15-bit prefix `9v6`) | 24 = 8 digits × 3 bits (below the resolution-2 cell `82489ffffffffff`, ≈ 99,000 km²) | 20 |
| Region named by a k-bit prefix | an aligned block of 2^(22−k) cells on Mosaic's 2048 × 2048 grid; longitude and latitude bits alternate, longitude first (`geo.rs:46-47`, `geo.rs:118`) | a geohash box of 2^(21−k) cells; bits alternate from the whole world, longitude first, so the first *sent* bit splits by latitude (`geo.rs:168-169`) | at k = 3j: an H3 cell of resolution 2 + j, as the union of its resolution-10 descendants ("snowflake", only an approximate hexagon). In between: unions of sibling cells, children {0–3} vs {4–6}, then pairs. Digit 7 is invalid (`geo.rs:281-287`, `geo.rs:325-326`) | an aligned block of 2^(20−k) cells in the box lat 29.9005–30.9245, lon −98.0505 to −97.0265; longitude first (`geo.rs:46-47`) |
| Levels with geometric meaning | all | all | every third | all |
| Finest cell (computed) | 0.001°: 111 m × 96 m ≈ 0.0107 km² | 0.00069° × 0.00137°: 76 m × 132 m ≈ 0.0101 km² | resolution 10: ≈ 0.017 km² in downtown Austin (average 0.015 km²) | 0.001°, same as the grid ≈ 0.0107 km² |
| Revealed counts, `day_3of3` | 746 | 730 | 754 | 720 |
| … at N = 7,194 / 14,387 | 760 / 730 | 748 / 740 | 800 / 772 | 750 / 726 |
| Output cells | 8 | 10 | 15 | 8 |

Findings:

1. **The number of revealed counts hardly depends on the encoding or on N.** It is 720–754 at
   N = 21,581 and 726–800 at the smaller N **[measured]**. It equals 2 · (1 + Σ_{k<n} |HH_k|), so
   it follows the number of prefixes that reach T. Because T = 0.005 · N scales with N, that number
   stays similar. A deeper tree (H3, n = 24) does not reveal proportionally more: the levels inside
   a digit keep the frontier narrow.
2. **The shape of what a count describes differs.**
   - **Grid, geohash, Morton:** every revealed count is the number of rides in an axis-aligned
     rectangle that halves at each level, alternately along longitude and latitude **[code]**.
   - **H3:** only every third level is a real H3 cell, and a cell's count covers its resolution-10
     descendants, whose union only approximates the hexagon. The levels in between count unions of
     4 or 2 sibling cells, which are irregular regions with no geographic meaning **[code]**.
   - H3 also reveals counts of branches that are empty by construction (digit 7). These carry no
     information [inferred].
3. **Where the counts are.**
   - **The first levels count large regions.** For example, the grid's level 1 counts the rides
     west and east of longitude −97.719, just east of downtown. Its domain (2.048° square) is much
     larger than Austin, so the early levels have few non-empty prefixes (frontier 2, 4, 4, 4, 4, 4
     for levels 1–6). Morton keeps only one prefix at level 1 **[measured]**.
   - **The deep levels count small blocks around the hotspots.** These include the sub-threshold
     neighbours (row 2): 353–366 exact counts below T per run **[measured]**.
4. **Frontier width.** The widest level keeps 39 / 43 / 39 / 39 prefixes and reveals 78 / 86 / 78 / 78
   counts in that one level **[measured]**. Geohash has the widest single level in these runs.
5. **Output granularity.** The same hotspots are cut differently. H3 reports 15 cells, geohash 10,
   grid and Morton 8 each **[measured]**. The finest cells are similar in area (0.010–0.017 km²), so
   each output count describes a block of about 100 m.
6. **S1 and the plaintext transport do not depend on the encoding.** With rows 8 and 10, the
   servers and the observer learn every ride's finest cell under whichever encoding is used. That
   is about 0.01–0.02 km², i.e. the pickup location to within about 100 m **[code]**. The
   cell sizes were computed from the grid geometry and with the `h3` library.
7. **The encoding parameters are public, and they show where the data is.** The geohash prefix
   `9v6`, the H3 resolution-2 cell and the Morton bounding box each outline the Austin area
   [inferred; these are public by design, A2].

### Per-level counts, `day_3of3` (from `summary_mastic.csv`)

Revealed counts per level, k = 1 … n:

| Encoding | Revealed counts per level |
|---|---|
| `mosaic_grid` | 2, 4, 8, 8, 8, 8, 8, 10, 16, 20, 26, 38, 48, 50, 62, 78, 76, 62, 64, 60, 56, 34 |
| `geohash` | 2, 4, 8, 8, 8, 12, 12, 16, 20, 34, 36, 46, 54, 66, 86, 66, 54, 52, 56, 54, 36 |
| `h3` | 2, 4, 4, 4, 6, 6, 6, 8, 8, 12, 16, 26, 32, 36, 48, 52, 62, 78, 74, 54, 58, 50, 60, 48 |
| `morton` | 2, 2, 4, 8, 8, 10, 14, 22, 24, 34, 44, 50, 72, 78, 74, 60, 56, 66, 58, 34 |

Kept per level (|HH_k|):

| Encoding | Kept per level |
|---|---|
| `mosaic_grid` | 2, 4, 4, 4, 4, 4, 5, 8, 10, 13, 19, 24, 25, 31, 39, 38, 31, 32, 30, 28, 17, 8 |
| `geohash` | 2, 4, 4, 4, 6, 6, 8, 10, 17, 18, 23, 27, 33, 43, 33, 27, 26, 28, 27, 18, 10 |
| `h3` | 2, 2, 2, 3, 3, 3, 4, 4, 6, 8, 13, 16, 18, 24, 26, 31, 39, 37, 27, 29, 25, 30, 24, 15 |
| `morton` | 1, 2, 4, 4, 5, 7, 11, 12, 17, 22, 25, 36, 39, 37, 30, 28, 33, 29, 17, 8 |

The revealed counts at level k are twice the kept prefixes at level k − 1, for every level and
encoding (e.g. `mosaic_grid` level 8: 2 × 5 = 10) **[measured]**.
