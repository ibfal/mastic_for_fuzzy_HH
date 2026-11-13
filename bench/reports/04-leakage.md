# 04: Leakage

## Summary

I wrote `bench/LEAKAGE_mastic.md` in the structure of A7:
- one table of what is revealed, with to whom, when, how much (formula + measured `day_3of3` value
  per encoding) and category;
- a section on how the encoding changes the leakage.

Sources:
- **Code:** the benchmarked commit `5efbdeb`.
- **Paper:** `bench/reports/mastic.pdf`, the full version with appendices.
- **Counters:** the leakage counters in `summary_mastic.csv`, plus the runner logs.

Every B5 item is confirmed. Three need more precise wording, and I added seven items that were
missing. Every entry is tagged [code], [measured], [tested] or [inferred].

**Issue S1 is not fixed in `5efbdeb`.** `prg.rs:71/77` makes every control bit `true`, so each
correction word carries (¬αᵢ, αᵢ). Each server can read all 21,581 inputs from its own keys. I
confirmed this by running the four control-bit tests from the `security-tests` branch against the
benchmarked source: all four fail, and α is read off a single key in 50 of 50 trials.

Measured for `day_3of3` (grid / geohash / H3 / Morton):
- **Revealed prefix counts:** 746 / 730 / 754 / 720, of which 366 / 356 / 363 / 353 are exact
  sub-threshold counts.
- **Output cells:** 8 / 10 / 15 / 8.
- **Per-client decisions:** 2N = 43,162 in every run.

## Files

Created:
- `bench/LEAKAGE_mastic.md`: the leakage analysis.
- `bench/reports/04-leakage.md`: this report.

Modified: none.

Temporary, removed again:
- `tests/s1_check_tmp_test.rs`: a copy of `security-tests:tests/security_control_bits_test.rs`,
  used to run the S1 tests against the benchmarked code. The working tree was clean afterwards
  (`git status`).

Outside the repo:
- I installed `pypdf` into the scratchpad, not `.venv`, to extract the paper's text, because the
  PDF reader needs poppler, which isn't installed.

Not touched:
- `bench/reports/mastic.pdf`: Git ignores it (`*.pdf`), and I left it that way.

## How to run

No new scripts. To reproduce the numbers and the S1 test:

```powershell
# leakage counters (already in bench\results\summary_mastic.csv)
.\.venv\Scripts\python.exe bench\scripts\evaluate.py
# S1: run the control-bit tests from the security-tests branch against the current source
git show security-tests:tests/security_control_bits_test.rs | Out-File -Encoding utf8 tests\s1_check_tmp_test.rs
cargo test --release --test s1_check_tmp_test      # expected on 5efbdeb: 4 failed
Remove-Item tests\s1_check_tmp_test.rs
```

## Checks

| B5 item | Result | Status |
|---|---|---|
| 1. Intended output: heavy cells and counts | **Confirmed.** 8 / 10 / 15 / 8 cells, equal to the plaintext answer (report 03). Paper: Fig. 2 step 3, §3.1 footnote 10. **More precise:** the counts go to the driver/collector only; the servers know the output paths but not the counts | pass |
| 2a. Count of every evaluated prefix, including pruned ones | **Confirmed.** 746 / 730 / 754 / 720 counts per run (paper: Fig. 2 step 2b.ii, footnote 10, §5.1). Each count is the vector [Σβ, count] (§6). **More precise:** they go to the driver/collector, not to the servers (in the paper, to both aggregators) | pass |
| 2b. Public parameters (N, threshold, `data_bits`) | **Confirmed**, plus the encoding parameters. N = number of stored shares, n = number of correction words per key, T computed by the driver | pass |
| 3a. Per-client FLP accept/reject | **Confirmed.** N = 21,581 decisions. The driver also sees both verifier shares, and the `keep` vector is sent to both servers (Fig. 2 step 1c; Appendix C Fig. 10) | pass |
| 3b. Per-client outcomes of the Merkle search | **Confirmed, but more precise.** In an honest run each level gives one "all clients pass" comparison: 21 / 20 / 23 / 19 per run. Per-client outcomes appear only after a mismatch (to the driver and both servers): 0 in the benchmark. The last level instead compares every client's final proof, at the driver only (§3.2.4, Fig. 2 step 2a.v) | pass |
| 4a. Constant control bits (S1) | **Confirmed and tested; not fixed** in `5efbdeb`. The last change to `prg.rs` on any branch, `c45e1d3`, is an ancestor of `5efbdeb`. 4 of 4 tests fail. Breaks VIDPF privacy (§2.3, Appendix A) | confirmed (leak present) |
| 4b. Driver = client simulator, leader and collector | **Confirmed.** It generates every report from plaintext α (`driver.rs:679-745`), chooses and sends the verification key (`driver.rs:1563-1566`), and receives every verification message | pass |
| 4c. RPC unencrypted and unauthenticated | **Confirmed in code:** plain TCP + bincode (`server.rs:340`, `driver.rs:1502-1507`); no TLS dependency; the server accepts any connection. An observer sees both keys, so it learns α and β even if S1 were fixed. That an outsider can call the RPCs is [inferred] | pass |
| 5. Encoding effect | **Confirmed** from `geo.rs`: aligned blocks for grid, geohash and Morton; for H3 only every third level is a real cell, approximate "snowflake" unions, irregular unions inside a digit. The revealed counts per run are measured per level | pass |
| Measured `day_3of3` values per encoding | in `LEAKAGE_mastic.md`; min = max over the 5 runs for every counter | pass |
| Counter consistency | revealed(k) = 2 · kept(k − 1), Σ = total, n levels: **72 of 72 runs** consistent | pass |
| Logs | no "Removing malicious" / "Detected malicious" line in the 72 `day_3of3` logs; driver log Merkle comparisons = n − 1 | pass |

Items I added to B5's list:

| Row | Item |
|---|---|
| 3 | The servers learn every level's kept/pruned bits, i.e. the heavy-prefix sets. |
| 7 | Per-client final-proof comparisons, at the driver only. They are the second N in `client_validity_bits` = 2N. |
| 11 | The driver gets each level's counts in the same reply as the Merkle roots, i.e. before the level is verified. After a mismatch it gets them again over the reduced report set. The paper verifies first (Fig. 2 step 2a, then 2b) and allows one aggregation per prefix (Appendix C, `Tagg[p]`). Not triggered in the benchmark. |
| 12 | The indices of rejected clients are printed to the server logs. |
| note 2 | Each revealed value is [Σβ, count]. With real weights, every evaluated prefix would also reveal its weight sum [inferred]. |
| row 10 | Message sizes reveal N, n and \|HH_k\| per level, even if the channels were encrypted [inferred]. |
| — | Checked and harmless: `PrgStream`'s debug prints are dead code; the constant server seed is unused. Two robustness issues that are not leakage: unchecked FLP joint randomness; the verification key is known to the client simulator. |

## Deviations and open questions

1. **B5 wording (decision).** B5 is Mastic-only (Part B), so only this file would change. Suggested
   replacements:
   - **2:** "the count of every evaluated prefix … is reconstructed by the driver/collector. The
     servers learn only which prefixes are kept (and the output paths)."
   - **3:** "per-client FLP decision (driver and both servers); per level, one Merkle-root comparison
     for all clients, with per-client outcomes only after a mismatch; at the last level, a per-client
     final-proof comparison (driver only)."
   - **4:** "S1 is **not** fixed in the benchmarked commit `5efbdeb`. Each server reads every α from
     its own key. Independently of S1, the plaintext RPC gives a network observer both keys."
   - Add rows 3, 7, 11 and 12 to the list.
2. **Row 11 differs from Fig. 2 (for information).**
   - Counts are revealed before the level's check, and again after a Merkle mismatch. With a
     malicious server this could expose one honest client's prefix [inferred].
   - It did not occur in the benchmark (`rounds.extra` = 0).
   - Fixing it would change protocol code, which C0.4 rules out. Should it go into the write-up as
     a known deviation?
3. **S1 and the benchmark results (decision).**
   - The measured times, bytes and accuracy should not change with the fix [inferred]: the fix reads
     the bits before masking them, with the same work and message sizes.
   - Every privacy statement about these runs must mention S1.
   - Do you want a timing re-run after S1 is fixed, to confirm?
4. **Paper reference.** `LEAKAGE_mastic.md` cites "§" of `bench/reports/mastic.pdf` (44 pages,
   appendices A–D). Git ignores the PDF, and committing a paper is probably not wanted. If you tell
   me which published version it is (venue or ePrint number), I'll add the reference so the section
   numbers can be checked without the file.
5. **Side note on A2 (for information).** I computed the cell areas. A resolution-10 H3 cell in
   downtown Austin is ≈ 0.017 km², against 0.0107 km² for a grid cell. Resolution 10 is still the
   closest H3 resolution (resolution 11 is ≈ 0.0025 km²), but "area-matched" is approximate (×1.6).

## What to commit

```
git add bench/LEAKAGE_mastic.md bench/reports/04-leakage.md
```

Commit message: `Add LEAKAGE_mastic.md: leakage table, S1 status (not fixed) and encoding effect`
