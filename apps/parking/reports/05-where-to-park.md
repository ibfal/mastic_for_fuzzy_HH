# P6b: "Where to park" view

This report follows P6 of [`PARKING_PLAN.md`](../PARKING_PLAN.md) (§8) and [`04-frontend.md`](04-frontend.md). It covers branch `parking`.
- Nothing is committed.
- No Rust code and no result file changed.

The design follows the apple-design skill (`.agents/skills/apple-design/SKILL.md`; it is not under `.claude/skills/`). The colours were checked with the dataviz palette validator.

## Summary

- **The page has a new tab, "Where to park".** It sits next to "Occupancy map" and shares the hour slider, the play button and the encoding switch. The page shows the following, per cell of the selected encoding at the selected hour:
  - **Red, sequential (light → dark = fuller):** the heavy cells, shaded by % full = cars ÷ 2019 sensor bays, from the Mastic export.
  - **Green, with white dots:** non-heavy cells evaluated at the last level of the crawl that have space at that hour, meaning % full < 85 % and at least 1 free sensor bay. The cutoff is a single constant, `GREEN_MAX_PCT = 85`, in `index.html`.
  - **Transparent grey, "Parking exists here":** every other cell with any 2025 bay or 2019 sensor bay. This includes last-level cells that are full at that hour, and cells without hourly data.
  - **A legend.** It says that green uses counts the Mastic collector learned during the search, which Mastic's design reveals anyway, and that grey can also mean "no hourly data". A tooltip on the red row says that red marks hotspots even when they have space.
  - **A "Best bets at hh:30" list.** It shows the 5 green cells or hotspots with the most free sensor bays. Clicking an entry zooms to the cell and opens its panel.
- **The view never reads the ground truth.** The test replaces every true occupancy in `data.js` with 99999, and the view stays identical: all 3 encodings, at 08:30 and 17:30.
- **Hourly data is sparse.** Only 3–7 % of the cells with parking have hourly data (heavy cells + last-level candidates), or 19–32 % of the cells with 2019 sensor bays (table in Checks §3). So most grey cells mean "unknown", not "full".
- **Colour-blind safety: colour alone is not enough, so there is a second cue.**
  - Under deuteranopia, the green and the middle red step are only ΔE 4.3 apart, below the floor of 6.
  - The second cue is a white-dot pattern on every green cell. The test checks it on every green cell, for every hour and encoding.
  - Text labels ("Hotspot" / "Space likely") back it up in the tooltips and the best-bets badges.
- **Restyle (apple-design).** The page uses:
  - the system font with size-specific tracking;
  - a translucent sticky toolbar, plus a legend in a thicker material with a deeper shadow;
  - iOS-style segmented controls;
  - press feedback on pointer-down;
  - support for reduced motion, reduced transparency and higher contrast.

  All existing IDs and selectors are kept. All existing features still work, and all existing checks still pass.
- **Checks: all pass.**
  - The build is deterministic.
  - `--check` passes, including the new sections (g)–(i).
  - The headless test passes: all the old checks plus the new ones, with no console errors, failed requests or HTTP errors. It saves 13 screenshots.

## Files

| File | Change | Why |
|---|---|---|
| `apps/parking/scripts/build_frontend.py` | modified | `SCHEMA_VERSION` 2. Per encoding it adds `parking[]` and `last_level`. `--check` gains (g), (h) and (i) |
| `apps/parking/frontend/data.js` | regenerated (571 KB → 1,616 KB) | adds the two new keys. Existing keys are unchanged |
| `apps/parking/frontend/index.html` | modified (37 → 57 KB) | adds the tab, the Where-to-park layer, legend and best bets, the candidate panel and the restyle |
| `apps/parking/scripts/test_frontend.py` | modified | keeps all old checks and adds the Where-to-park checks, the CVD report, the ground-truth independence test and 7 screenshots |
| `apps/parking/reports/screenshots/park_{morton,geohash,h3}_{0830,1730}.png` | new (6 files, about 0.95–1.0 MB each) | the new view per encoding |
| `apps/parking/reports/screenshots/phone_park_morton_0830.png` | new (516 KB) | the new view at 390 px |
| `apps/parking/reports/screenshots/{morton,geohash,h3}_1130.png`, `morton_1130_cell_truth.png`, `morton_crawl_level6.png`, `phone_morton_1130.png` | regenerated | the restyle changes them slightly |
| `apps/parking/PARKING_PLAN.md` | modified | §8 P6 row: one clause on the new view |
| `apps/parking/reports/05-where-to-park.md` | new | this report |

### New keys in `data.js`, per encoding

- **`parking[]`**: every row of `capacity_<enc>.csv` with `markers_2019 > 0` or `bays_2025 > 0`.
  - Fields: `{bits, label, capacity, bays_2025, center, outline}`, where `capacity` = 2019 sensor bays or null.
  - The outline comes from the plaintext encoder, the same function as the truth layer.
  - **The records hold capacity data only.** `--check` rejects any other key, so occupancy cannot creep in.
- **`last_level`**: `{level, candidates: [...]}`. It holds the `heavy = false` candidates of `levels[-1]` in the export.
  - Fields: `{bits, label, count, cars, capacity, bays_2025, free, pct, center, outline | null}`, with `cars = value[:24]`. The export's `value` has 25 entries; the 25th is the total.
  - The outline is the export's region. H3 has 1 candidate without a region, a non-cell; it is kept but not drawn.

### The view

- **Map.** It shows one polygon per cell with parking, in a separate Leaflet pane, `park`.
  - The colour at hour h is decided as follows: red if the cell is heavy; green if it is a last-level candidate with `capacity − cars[h] ≥ 1` and `100·cars[h]/capacity < GREEN_MAX_PCT`; grey otherwise.
  - Each path carries `data-park` (`red` / `green` / `grey`) and `data-bits`.
  - Restyling on an hour change only touches the polygons whose colour changed. A 250 ms fill transition makes the play button read as continuous; it is off under reduced motion.
  - Green and red are brought to the front, so their outlines are not covered by grey neighbours.
- **Colours.** These were validated with the dataviz validator.
  - Red ramp: `#f0958a #e26a5e #c9463b #a12d25 #6e1a15`, in the same 5 bins as the occupancy map. Ordinal check: monotone lightness, steps ≥ 0.06, light end 2.18:1.
  - Green: `#0ca30c`, the palette's "good" green, with a white-dot pattern (`#parkFree`).
  - Grey: `#75746e` at 24 % fill.
  - The map colours are the same in light and dark mode, because the OSM tiles stay light.
- **Tooltips.**
  - Red: cars, bays, % full and free bays, plus "Red marks hotspots even when they have space at this hour".
  - Green: free bays and % full, plus "Counts the Mastic collector learned during the search".
  - Grey: the bays, and why the cell is grey: "Full at hh:30: x %", "No hourly data: below the threshold and not evaluated at the last level", or "no 2019 sensor bays".
- **Cell panel.** Clicking a red or green cell, or a grey last-level cell, opens the panel. For a last-level candidate it has a note: "Below the threshold: not output, but its hourly counts were revealed during the search (last level L)", followed by whether the cell is green at that hour. The chart, capacity, free spaces, top 3 hours and table are the same as for heavy cells.
- **Best bets.** The list takes green cells and heavy cells with ≥ 1 free sensor bay, and sorts them by free bays (descending), then % full (ascending), then label. It shows the top 5 (`BEST_BETS_N`). Each entry is a button with a "Hotspot" or "Space likely" badge (the green badge is dotted), the label, the free bays, the % full and the capacity. A click calls `fitBounds` (maxZoom 17, with no animation under reduced motion) and opens the panel. On a phone it also scrolls the map into view.
- **Isolation from the occupancy-map overlays.** In the Where-to-park view:
  - the ground-truth and "What the collector saw" toggles are disabled, with the tooltip "Available in the Occupancy map view";
  - their layers, the banner, the hatch and the heavy-cell layer are removed.

  Their state is kept and comes back on switching tabs. All reads of `state.truth` / `state.crawl` go through `truthOn()` / `crawlOn()`, which are false in this view, so the cell panel does not show the truth ticks there either. Switching tabs does not move the map.
- **URL state.** `view=park` is added only in this view, so older links open as before. `cell=` restores a heavy cell, or a last-level candidate in this view. Tabs follow the ARIA tab pattern: `role=tab`, `aria-selected`, and the arrow keys move between tabs.

### Restyle (apple-design), with all features kept

- The system font stack (`-apple-system`, `SF Pro Text`, `system-ui`, `Segoe UI`), with font sizes in rem.
  - h1: 1.25 rem / −0.02 em / line-height 1.15.
  - Small labels: +0.01 em.
- The toolbar is a sticky translucent material (`backdrop-filter: blur(20px) saturate(180%)`). On a phone, the panel content scrolls under it, so the hour slider stays reachable. `#mapwrap` gets `isolation: isolate`, so Leaflet's z-indices stay inside the map.
- The legend is the larger floating surface, so it gets a stronger blur and a deeper shadow than the chips.
- Segmented controls (view and encoding): a grey track with a raised thumb on the selected item.
- Press feedback on pointer-down (`:active { transform: scale(.97) }`, 100 ms) for buttons, segments and best-bet cards.
- `prefers-reduced-motion` turns off transitions and fly animations. `prefers-reduced-transparency` makes the surfaces solid. `prefers-contrast: more` makes the surfaces solid with ink borders.
- Dark-mode tokens are defined for every new colour.

## How to run

PowerShell, from the repo root:

```powershell
.\.venv\Scripts\python.exe -I apps\parking\scripts\build_frontend.py           # write apps\parking\frontend\data.js
.\.venv\Scripts\python.exe -I apps\parking\scripts\build_frontend.py --check   # exit 1 on any mismatch
start "apps\parking\frontend\index.html#view=park&hour=8"                      # open the Where-to-park view
.\.venv\Scripts\python.exe -I apps\parking\scripts\test_frontend.py            # headless test + 13 screenshots (--headed to watch)
```

## Checks

### 1. Deterministic build: pass

I built twice into the scratchpad and once into the repo. All three files have the same SHA-256: `5b202b338a27c5ca583047d0f9fc2326489ed0e1e70a102fcb50da1eb4525c1d` (1,616,419 bytes).

### 2. `build_frontend.py --check`: pass

Sections (a)–(f) are unchanged from P6. The new sections are:
- **(g) Parking cells.** The set equals the capacity-CSV rows with any bay. Capacity, 2025 bays and label match. The outline equals the plaintext encoder's cell within the rounding, and the keys are exactly the allowed capacity fields.
- **(h) Last-level candidates.**
  - The set equals the export's `heavy = false` candidates of the last level.
  - **`cars == value[:24]`**, and `sum(cars) == count == value[24]`.
  - Capacity and 2025 bays match the CSV, and free / pct are consistent.
  - The outline equals the export region.
  - The heavy last-level candidates equal `heavy_hitters`: the same bits, and `value[:24] == buckets`. Every heavy cell still matches the results, as in (b).
- **(i) Summary.** Coverage, and red / green / grey per hour, computed in Python.

```
(a) apps/parking/frontend/data.js equals a fresh build: yes
    morton   last-level candidates: 35/35 with hourly counts = export value; hourly data: 21 heavy + 35 last-level = 56 cells (56 with parking) of 1347 cells with parking (234 with 2019 sensor bays)
             04:30 red 21 / green 21 / grey 1305 | 08:30 red 21 / green 19 / grey 1307 | 12:30 red 21 / green 15 / grey 1311 | 17:30 red 21 / green 20 / grey 1306
    morton   pass: 21 heavy cells (export 21), 234 truth cells, heavy cells with truth = Mastic hours: 21/21, 12 levels, 460 regions revealed (250 kept, 210 pruned, 0 without region)
    geohash  last-level candidates: 39/39 with hourly counts = export value; hourly data: 17 heavy + 39 last-level = 56 cells (55 with parking) of 1685 cells with parking (237 with 2019 sensor bays)
             04:30 red 17 / green 27 / grey 1641 | 08:30 red 17 / green 25 / grey 1643 | 12:30 red 17 / green 19 / grey 1649 | 17:30 red 17 / green 24 / grey 1644
    geohash  pass: 17 heavy cells (export 17), 237 truth cells, heavy cells with truth = Mastic hours: 17/17, 21 levels, 470 regions revealed (251 kept, 219 pruned, 0 without region)
    h3       last-level candidates: 48/48 with hourly counts = export value; hourly data: 28 heavy + 48 last-level = 76 cells (72 with parking) of 1028 cells with parking (193 with 2019 sensor bays)
             04:30 red 28 / green 34 / grey 966 | 08:30 red 28 / green 34 / grey 966 | 12:30 red 28 / green 29 / grey 971 | 17:30 red 28 / green 33 / grey 967
    h3       pass: 28 heavy cells (export 28), 193 truth cells, heavy cells with truth = Mastic hours: 28/28, 15 levels, 460 regions revealed (257 kept, 203 pruned, 10 without region)
all checks pass
```

**Negative test.** In a copy of `data.js` I changed the first hour count of the first last-level candidate. `--check` then reported "candidate hourly counts differ from the export's value" for Morton `001110101110`, and exited with 1.

### 3. Hourly data vs cells with parking

| Encoding | Heavy | Last-level, not heavy | With hourly data (and parking) | Cells with parking | Share | Cells with 2019 sensor bays | Of those with hourly data |
|---|---|---|---|---|---|---|---|
| Morton | 21 | 35 | 56 (56) | 1,347 | 4.2 % | 234 | 42 (17.9 %) |
| geohash | 17 | 39 | 56 (55) | 1,685 | 3.3 % | 237 | 44 (18.6 %) |
| H3 | 28 | 48 | 76 (72) | 1,028 | 7.0 % | 193 | 62 (32.1 %) |

- Last-level candidates without 2019 sensor bays (Morton 14, geohash 12, H3 14) all have count 0. Every candidate with cars lies in a cell with sensor bays.
- Candidates outside any parking cell (geohash 1, H3 4, including the non-cell) are not drawn.
- Most cells with parking (1,113 / 1,448 / 835) have only 2025 bays and no 2019 sensors, so no hourly data could exist for them.

### 4. Headless page test (`test_frontend.py`, Chrome 157): pass

```
  browser: chrome 157.0.8103.52
  morton: 21 cell polygons, privacy 460 revealed / 21 heavy
  morton: collector layer checked on 12 levels (460 regions, 0 without outline)
  geohash: 17 cell polygons, privacy 470 revealed / 17 heavy
  geohash: collector layer checked on 21 levels (470 regions, 0 without outline)
  h3: 28 cell polygons, privacy 460 revealed / 28 heavy
  h3: collector layer checked on 15 levels (460 regions, 10 without outline)
  morton: Where to park checked for 24 hours; red/green/grey at 04:30 21/21/1305, 08:30 21/19/1307, 12:30 21/15/1311, 17:30 21/20/1306; green 04:30 vs 12:30: 21 vs 15 cells, 6 differ
  morton: best bets 08:30 morton (35, 25) (mastic), morton (31, 27) (mastic), morton (35, 27) (mastic); 17:30 morton (35, 25) (mastic), morton (31, 27) (mastic), morton (40, 22) (mastic)
  geohash: Where to park checked for 24 hours; red/green/grey at 04:30 17/27/1641, 08:30 17/25/1643, 12:30 17/19/1649, 17:30 17/24/1644; green 04:30 vs 12:30: 27 vs 19 cells, 8 differ
  geohash: best bets 08:30 r1r0fsf[1] (mastic), r1r0fs7[0] (mastic), r1r0g5k[1] (mastic); 17:30 r1r0fsf[1] (mastic), r1r0fs7[0] (mastic), r1r0g5k[1] (mastic)
  h3: Where to park checked for 24 hours; red/green/grey at 04:30 28/34/966, 08:30 28/34/966, 12:30 28/29/971, 17:30 28/33/967; green 04:30 vs 12:30: 34 vs 29 cells, 5 differ
  h3: best bets 08:30 8abe63563667fff (mastic), 8abe6354599ffff (candidate), 8abe6356362ffff (mastic); 17:30 8abe63563667fff (mastic), 8abe6356360ffff (mastic), 8abe63563397fff (mastic)
  CVD (Machado 2009, OKLab ΔE×100) green #0ca30c vs red ramp #f0958a,#e26a5e,#c9463b,#a12d25,#6e1a15: normal vision min 30.8; protan min 10.8 (#e26a5e), deutan min 4.3 (#c9463b), tritan min 28.8 (#f0958a) → colour alone is below the 6 floor for deutan; the dotted pattern (checked on every green cell, every hour) and the text labels in tooltips and best bets carry the distinction
  Where to park with every true occupancy replaced by 99999: identical cells and best bets (3 encodings × 08:30, 17:30)
  screenshots (13): geohash_1130.png, h3_1130.png, morton_1130.png, morton_1130_cell_truth.png, morton_crawl_level6.png, park_geohash_0830.png, park_geohash_1730.png, park_h3_0830.png, park_h3_1730.png, park_morton_0830.png, park_morton_1730.png, phone_morton_1130.png, phone_park_morton_0830.png
frontend test: pass
```

As before, the test fails on any console error, page error, failed request or HTTP status ≥ 400. None occurred.

All P6 checks still pass after the restyle. They cover the cell polygons, hatch, encoding switch, hour label, privacy figures, cell panel, top 3 hours, chart, ground-truth toggle and banner, every crawl level, the play button, and the phone layout. The test also confirms that the occupancy map draws nothing in the `park` pane.

New checks, per encoding:
- **`view=park`.** It opens the "Where to park" tab, and `GREEN_MAX_PCT == 85`. The truth and collector toggles are disabled. The cells, truth, crawl and hatch panes are empty.
- **For each of the 24 hours.** The red, green and grey `data-bits` sets drawn equal the Python computation from `data.js`, with no untagged path. The computed `fill` of every green path is `url("#parkFree")`, and no red or grey path uses the pattern.
- **The green set changes between 04:30 and 12:30**:
  - Morton: 21 → 15 cells, 6 differ;
  - geohash: 27 → 19 cells, 8 differ;
  - H3: 34 → 29 cells, 5 differ.
- **Legend.** It contains "Hotspot", "Space likely", "85 %", "Parking exists here", "learned during the search" and "no hourly data". The `title` of the red row contains "even when they have space".
- **Best bets at 08:30 and 17:30.**
  - The entries and their order (bits and kind) equal the Python top 5, each with "n free".
  - The heading reads "Best bets at 08:30" / "Best bets at 17:30".
  - Clicking the first entry sets the selected cell and kind. The map is then at zoom ≥ 16 and contains the cell's centre. The panel title holds the label, and the panel has the 24-bar chart.
- **Switching back to "Occupancy map".** It draws the heavy cells again, empties the `park` pane, re-enables the toggles and drops `view=` from the URL.
- **Ground truth independence.** A second page replaces every `truth[].occupied` value with 99999 before the page reads `data.js`. The red / green / grey sets and the best bets are identical at 08:30 and 17:30 for all 3 encodings.
- **Phone (390 px).** In the Where-to-park view there is no horizontal scroll, the cell sets are right, and the 5 best bets are visible.

I looked at the screenshots myself:
- The red shades follow % full, and the dots are visible on every green cell.
- The grey reads as "parking exists" without competing with red and green.
- The legend does not cover any red or green cell at 08:30 or 17:30. It does cover some grey cells in the bottom-left corner.

### 5. No protocol or result changes: pass

`git status --short -- src tests Cargo.toml apps/parking/results` is empty.

## Decisions needed

1. **Green is built on revealed-but-not-output counts.** The green cells use the hourly vectors of the pruned last-level candidates. The page states this, and it is true of Mastic as run. But it turns a leak into a feature. Under a design that hides pruned totals, or with 1 bucket instead of 24, the green layer would be empty. Do you want the view framed this way in the thesis, or labelled more prominently as "uses leakage"?
2. **The 85 % cutoff.** It is one constant (`GREEN_MAX_PCT`). Raising it to 90 % adds only a few green cells. At 04:30, 08:30, 12:30 and 17:30:
   - Morton: 21/19/15/20 → 21/20/16/20;
   - geohash: 27/25/19/24 → 27/25/22/26;
   - H3: 34/34/29/33 → 34/34/33/34.

   Keep 85 %?
3. **Best bets rank by absolute free bays.** So large hotspots dominate. Examples: Morton (35, 25) at 10 % full with 91 free; at 17:30 all top 5 are hotspots in Morton and geohash. Ranking by % free, or showing hotspots and green cells in separate lists, would surface more green cells. Keep the ranking as specified?
4. **Grey mostly means "unknown".** It covers 1,300+ cells per encoding, most with 2025 bays only. One option is to draw grey only for cells with 2019 sensor bays, or to add a toggle for it.
5. **Size of `data.js`.** It grew from 571 KB to 1.6 MB, mostly the outlines of about 4,000 parking cells. Morton and geohash outlines could be computed in JS from the bits, which would save about 0.6 MB; H3 would still need stored outlines or a library. I kept stored outlines, so that one checked source serves every encoding.
6. **Screenshots.** There are now 13 PNGs, about 11.5 MB. The 7 new ones are about 6.4 MB, and the 6 old ones were regenerated by the restyle. The same question as in report 04, decision 3: commit all of them, a subset (for example `park_morton_0830.png` and `phone_park_morton_0830.png`), or none?
7. **The legend is collapsed on a phone.** The explanation of green and grey sits behind "Legend ▸", as in P6. The best-bet badges and the tooltips still carry the labels. Open it by default in the Where-to-park view?

## What to commit

Not included: `.agents/` (untracked; it holds the apple-design skill, not part of this step) and `__pycache__/` (git-ignored). The screenshots follow the `.gitignore` exception from P6.

```powershell
git add apps/parking/PARKING_PLAN.md `
        apps/parking/scripts/build_frontend.py `
        apps/parking/scripts/test_frontend.py `
        apps/parking/frontend/index.html `
        apps/parking/frontend/data.js `
        apps/parking/reports/05-where-to-park.md `
        apps/parking/reports/screenshots/morton_1130.png `
        apps/parking/reports/screenshots/geohash_1130.png `
        apps/parking/reports/screenshots/h3_1130.png `
        apps/parking/reports/screenshots/morton_1130_cell_truth.png `
        apps/parking/reports/screenshots/morton_crawl_level6.png `
        apps/parking/reports/screenshots/phone_morton_1130.png `
        apps/parking/reports/screenshots/park_morton_0830.png `
        apps/parking/reports/screenshots/park_morton_1730.png `
        apps/parking/reports/screenshots/park_geohash_0830.png `
        apps/parking/reports/screenshots/park_geohash_1730.png `
        apps/parking/reports/screenshots/park_h3_0830.png `
        apps/parking/reports/screenshots/park_h3_1730.png `
        apps/parking/reports/screenshots/phone_park_morton_0830.png
```

Commit message: `P6b: "Where to park" view (hotspots, last-level cells with space, parking cells; best bets) and apple-design restyle`
