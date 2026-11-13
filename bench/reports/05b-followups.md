# 05b: Follow-ups to spec v3 (reference reader, reference setting, comparison baseline, A9 v3.1)

## Summary

I implemented your four decisions on report 05's open questions.

- **Reference:** the reader now ignores extra fields. A reference whose τ, δ or T is not A5's stops
  `evaluate.py` with exit code 2 before anything is written.
- **comparison.md:** every ratio is divided by Mastic at τ = 0.005, and every ratio label names
  both τ values. A same-τ section is added for each τ that two systems share.
- **A9 v3.1:** `bench_common.py` and `evaluate.py` accept and handle the additions.
- **One dependency:** `evaluate.py` now imports only `bench_common.py`. `self_check`,
  `BASE_CONFIG` and the smoke names moved there.

On the existing 72 runs, all 1,644 values that the old and new summaries share are identical. The
summary gains three `outputs_no_coords` columns, which are 0 for Mastic, and all 12 plots are
byte-identical.

I also fixed a crash that was already in HEAD: with a reference present, `evaluate.py` failed
whenever its output was redirected on Windows.

## Files

Modified:
- `bench/scripts/bench_common.py`:
  - **Moved from `run_bench.py`, values unchanged:** `BASE_CONFIG`, `SMOKE_DATASET` and
    `SMOKE_SUBSET`.
  - **`self_check`**, moved from `run_bench.py` and extended for v3.1:
    - outputs are matched by label, and labels must be unique and non-null;
    - if every count is null, `exact` is `None` and only the locations are compared; counts that
      are only partly null fail;
    - centres are compared only where both sides have coordinates; coordinates on one side only
      fail;
    - it returns the old keys plus `problems`.
  - **Schema:**
    - `server_to_server` is a number, `[s0, s1]` or null;
    - `count` may be null;
    - `lat`/`lon` may be null, but both or neither;
    - run lines may carry an optional `extra` object.
  - **Self-test:** 9 new cases, 18 in total.
- `bench/scripts/evaluate.py`:
  - **Decision 1:** `_keys()` checks for missing fields only and ignores extras at every level.
  - **Decision 2:** the new `check_reference_setting()` checks each reference as it loads. τ must
    be 0.02, δ must be 5, and T must equal max(1, ⌊0.02·N⌋) for every N of that subset's runs.
    Otherwise it raises an error naming every difference and "wrong file?"; the script prints a
    banner, writes nothing and exits with code 2. The old notes are gone; `reference_notes` became
    `reference_descriptions`.
  - **Decision 3:** in `comparison_md()`:
    - the baseline is Mastic at τ = 0.005 and comes first, whichever system runs `--combine`;
    - ratio headers read e.g. `Online ratio mosaic τ=0.02 / mastic τ=0.005`;
    - for each τ that two systems share, a `## Same τ = …` section repeats the tables for that τ
      only;
    - without a shared τ, the intro says there is no same-τ table.
  - **Decision 4:**
    - `server_to_server` pair: each direction is its own link, so the busiest link uses the larger
      direction. Totals and the `bytes_online_server_to_server_*` columns use the sum.
    - New summary columns `outputs_no_coords_median/min/max`, giving 140 columns.
    - `selfcheck_exact` is empty when a run reveals no counts.
    - `extra` values go into `tables.md` section 9 (median [min–max] for numbers), which appears
      only if some run has `extra`.
  - **Imports:** no longer imports `run_bench`.
  - **Additions beyond the prompt:**
    - `safe_console()`, the cp1252 fix (deviation 1);
    - `banner()` looks up stderr when it is called, so tests can capture it.
- `bench/scripts/run_bench.py`: imports only. It uses `bc.BASE_CONFIG`, `bc.SMOKE_*` and
  `bc.self_check`, and the three definitions are deleted. Behaviour is unchanged; see the dry-run
  check.
- `bench/tests/test_evaluate.py`: 50 tests, 33 before.
  - **Decision 1:** extras ignored at all three levels, missing fields still rejected.
  - **Decision 2:**
    - wrong τ, δ 4, T 430, and T 287 (2/3's T) are rejected, while float noise in τ is accepted;
    - T is checked against the N of the subset's runs;
    - `main()` exits with 2 and writes nothing.
  - **Decision 3:**
    - the baseline is Mastic 0.005 even with the systems in the order `["mosaic", "mastic"]`;
    - the same-τ section appears only when a τ is shared, with the right ratios.
  - **Decision 4:**
    - the schema accepts the v3.1 additions and rejects malformed ones;
    - the pair semantics are checked on hand-computed numbers;
    - all 72 real lines give identical metrics with `server_to_server` 0 or [0, 0];
    - self-check without counts, and with coordinate-less outputs;
    - `outputs_no_coords` takes no part in the accuracy;
    - `selfcheck_exact` stays empty through the CSV and shows "—" in `tables.md`;
    - `extra` appears in `tables.md` and not in the summary;
    - an end-to-end `main()` on Mosaic-style lines;
    - `evaluate` has no `run_bench`.
  - **Plus** a test of the cp1252 fix.
  - The "unexpected field" reject case was removed (decision 1).
- `bench/README.md`: sections 3 and 6 describe the above.
- `bench/results/summary_mastic.csv`, `bench/results/tables.md`: regenerated.

Created:
- `bench/reports/05b-followups.md`: this report.

Not touched:
- `BENCHMARK_MASTIC.md`, the protocol and driver code, the raw results, the ground truth and
  `.gitignore`.
- The plots: regenerated, byte-identical.

`bench/.run/configs/` is ignored by Git; the dry runs rewrote it with identical content. All test
inputs for the command-line checks are in the session scratchpad. `bench/reference/` was not
created.

## How to run

```powershell
.\.venv\Scripts\python.exe -m pytest bench\tests -p no:cacheprovider
.\.venv\Scripts\python.exe bench\scripts\bench_common.py --self-test
.\.venv\Scripts\python.exe bench\scripts\evaluate.py
# once bench\reference\day_<subset>.json exists (A5 format and setting; extra fields are ignored):
.\.venv\Scripts\python.exe bench\scripts\evaluate.py
.\.venv\Scripts\python.exe bench\scripts\evaluate.py --combine bench\results\summary_mosaic.csv
.\.venv\Scripts\python.exe bench\scripts\run_bench.py --dry-run   # configs unchanged by the move
```

## Checks

| Check | Numbers | Status |
|---|---|---|
| Tests | 50 of 50 pass (33 before, 17 new) | pass |
| Schema self-test | 18 of 18. The 9 new cases accept a pair, a null count, null lat/lon and an `extra` object. They reject `server_to_server` with 3 values, lat null with a numeric lon, `extra` as a list, `extra` null, and `extra` on a client line | pass |
| Existing results stay valid | All 72 lines pass the A9 check; the same 72 lines with `server_to_server` [0, 0] give identical `run_metrics` | pass |
| Summary unchanged | 12 rows with the same keys. Columns: 137 → 140, 0 removed, shared columns in the same order. Shared values compared: 396 timing, 432 bytes, 108 rounds, 216 leakage, 492 other, 1,644 in all: **0 differ**. Added: `outputs_no_coords_median/min/max`, 0 in every row | pass |
| Self-check (A5) | 72 of 72 runs | pass |
| Old vs new `self_check` | The old function, taken from HEAD, and the new one compared on 76 real lines (72 runs and 4 smoke runs): identical results; the new one only adds `problems: []` | pass |
| B3 sanity checks | 60 of 60 | pass |
| Plots | 12 of 12 files byte-identical to before | pass |
| `tables.md` | Only the intended changes: one sentence in section 3 (pairs), one in section 5 (outputs without coordinates, and "—" in Exact), and a column "Outputs without coordinates" (0) | pass |
| Deterministic | Rerun: summary, `tables.md` and plots byte-identical | pass |
| `run_bench.py --dry-run` and `--smoke --dry-run` | 20 of 20 configs byte-identical to before the `BASE_CONFIG`/`SMOKE_*` move | pass |
| Decision 1 on the real runs | Synthetic references for all three subsets, with extra fields at the top level (`N`, `source`), in squares and in areas: exit 0, accuracy computed. Hand check, `mosaic_grid` 3/3: (1003, 993), (1003, 994) and (1000, 998) are within L∞ 5 of area 0; (935, 1076) and (936, 1076) are within L∞ 5 of area 1. So P = 5/8 = 0.625, R = 2/2, F1 = 0.769, the same as `evaluate.py` | pass |
| Decision 2 on the real runs | The same references at τ = 0.005 (T = 35): exit 2. The message names `day_1of3.json`, "tau = 0.005, A5 requires 0.02; T = 35, … = 143 for N = 7194" and "wrong file?". The output folder was never created | pass |
| Decision 3 on the real runs | `--combine` with a fake Mosaic summary (τ 0.02 with online × 2, τ 0.005 with online × 3). Series: mastic 0.005, mosaic 0.005, mosaic 0.02. Ratios 3.00 / 2.00, labelled `mosaic τ=0.005 / mastic τ=0.005` and `mosaic τ=0.02 / mastic τ=0.005`. One `## Same τ = 0.005` section with only the 0.005 columns (ratio 3.00). Parameter check: consistent | pass |
| Decision 4, end to end | Mosaic-style lines through `main()`: `server_to_server` [3,000,000, 1,000,000], null counts, one output with null coordinates, and `extra`. Exit 0; self-check passes with Exact "—"; `outputs` 1, `outputs_no_coords` 1; server↔server 4,000,000; busiest link 3,000,000; `extra` in section 9 | pass |
| Branch, git | On `benchmarking`; nothing committed or staged | pass |

## Deviations and open questions

1. **Crash fixed that was already in HEAD (addition, please confirm).**
   - **The crash:** with a reference present, `evaluate.py` prints "…for runs of every τ". When
     stdout is redirected or piped on Windows, Python encodes it as cp1252, which has no τ. The
     script crashed there, before writing anything.
   - **The fix:** `main()` now calls `safe_console()`, so such characters are written as `τ`
     escapes. The console output is unchanged, and a test covers it. Say if you'd rather I revert
     it.
2. **Schema choices.**
   - `extra: null` is rejected, because A9 says "an object".
   - `extra` is allowed on run lines only, not on client lines; A9 lists it with the run-line
     additions.
   - `lat`/`lon` must both be null or both be numbers.
   - `label` stays nullable in the schema, but the self-check requires unique, non-null labels:
     outputs without coordinates can only be matched by label.
3. **Self-check edge cases.**
   - An empty output counts as exact, since there is nothing to compare. So a Mosaic row with no
     outputs would show Exact "yes" rather than "—".
   - A row's `selfcheck_exact` is empty if any measured run reveals no counts.
   - The ground truth must list outputs without coordinates with null `lat`, `lon` and `grid`.
4. **Where N comes from for the T check.** N is taken from the subset's runs (`n_clients`), not
   from `manifest.json`. The self-check separately checks N against the ground truth. τ is
   compared with a relative tolerance of 10⁻⁹, so float noise such as 0.1 × 0.2 passes.
5. **comparison.md layout.** The main tables keep every series, including Mosaic 0.005 if it
   exists, with ratios against Mastic 0.005. The same-τ section then repeats the 0.005 columns on
   their own. The client-cost table doesn't depend on τ, so it appears only once.
6. **tables.md section 9.** It is placed at the end and appears only with `extra`. A Mosaic-only
   `tables.md` numbers its sections 1–6, 8, 9, because section 7 (the B3 checks) is Mastic-only;
   that gap existed before.
7. **Reusing `evaluate.py` in the Mosaic harness (question).** It now needs only `bench_common.py`,
   but that file still has Mastic-specific parts:
   - `SYSTEM`, `ENCODINGS`, the ground-truth location and `BASE_CONFIG`;
   - `checker_module()`, which imports `scripts/check_heavy_hitters.py` from this repo for
     `to_grid`.

   Some `tables.md` sentences also name Mastic, e.g. "Server offline is 0 for Mastic" and "bincode
   varint for Mastic". So the Mosaic harness needs:
   - `evaluate.py`, unchanged;
   - `bench_common.py`, with `SYSTEM = "mosaic"` and its own ground truth;
   - a `to_grid`.

   Should I make that split cleaner? For example: an inline `to_grid` from A1's formula, checked
   against `check_heavy_hitters.py`, and wording that doesn't name a system.
8. **Runner log.** `bench/.run/runner_log.jsonl` (ignored by Git) will have a `problems` list in
   each `self_check` record from now on.
9. **`__pycache__`.** The existing folders are older than this session and ignored by Git. pytest
   rewrites `bench/tests/__pycache__` on every run, as before.

## What to commit

```
git add bench/scripts/bench_common.py bench/scripts/evaluate.py bench/scripts/run_bench.py bench/tests/test_evaluate.py bench/README.md bench/results/summary_mastic.csv bench/results/tables.md bench/reports/05b-followups.md
```

Commit message: `Spec v3 follow-ups: lenient A5 reader, A5 setting check, Mastic-baseline comparison, A9 v3.1`
