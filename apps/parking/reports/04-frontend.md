# P6: frontend for the private parking map

Prompt P6 of [`PARKING_PLAN.md`](../PARKING_PLAN.md) (§8). Branch `parking`. Nothing is committed. No Rust code and no result file changed.

## Summary

- **The page only shows what the Mastic run revealed.** `apps/parking/frontend/index.html` reads the saved exports through `data.js` and never starts a run. It opens by double-clicking, with no web server, build step or npm. The only external resources are Leaflet 1.9.4 from cdnjs (with SRI hashes) and OpenStreetMap tiles.
- **`build_frontend.py` writes `data.js`.** For each encoding it contains:
  - every heavy cell with its real outline (from the export's region), centre, label and 24 hour counts (cars present at h:30);
  - capacity (2019 sensor bays, `markers_2019`), free spaces and % full per hour;
  - the run parameters (τ, T, N, day);
  - the privacy figures;
  - the crawl candidates of every level;
  - the true occupancy of every sensor cell, used only by the comparison layer.
- **The page.** It has:
  - a short header;
  - an encoding switch (Morton / geohash / H3);
  - an hour slider 0–23 with a play button;
  - cells coloured by % full, with a legend: > 100 % and "no capacity" each have their own colour and a "!" / "?" marker;
  - a cell panel with a 24-hour bar chart, capacity, free spaces at the hour and the top 3 hours;
  - a "What's private" panel;
  - a ground-truth toggle with the banner "not available in a real deployment";
  - the "What the collector saw" layer (level slider; kept regions in violet, pruned in grey, tooltip with the count and % of N).

  Areas below the threshold are hatched as "not revealed — *not* empty".
- **Privacy figures.** Region totals revealed during the crawl vs heavy cells output:
  - Morton: **460** vs **21**;
  - geohash: **470** vs **17**;
  - H3: **460** vs **28**.
- **Checks:** all pass.
  - The build is deterministic.
  - `--check` matches every heavy cell, hour count, capacity, free-space value and crawl level against the results JSON and the CSVs.
  - The headless Playwright test in Chrome passes with no console errors, failed requests or HTTP errors.
  - It saved 6 screenshots.
- **PARKING_PLAN.md:**
  - §7 experiments 2 and 3 are marked as dropped (no benchmarking in this application).
  - The §8 P6 row points to the frontend.

## Files

| File | Change | Why |
|---|---|---|
| `apps/parking/scripts/build_frontend.py` | new | builds `frontend/data.js` from the 3 exports, the 3 capacity CSVs and the 3 occupancy CSVs; `--check` verifies it against the sources; `--out` writes elsewhere |
| `apps/parking/frontend/data.js` | new (generated, 571 KB) | `window.PARKING_DATA = {…};`, one record per line, so diffs stay readable |
| `apps/parking/frontend/index.html` | new (37 KB) | the page; inline CSS/JS, no build |
| `apps/parking/scripts/test_frontend.py` | new | headless Playwright test + screenshots |
| `apps/parking/reports/screenshots/*.png` | new (6 files, 5.6 MB) | `morton_1130`, `geohash_1130`, `h3_1130`, `morton_1130_cell_truth`, `morton_crawl_level6`, `phone_morton_1130` |
| `.gitignore` | modified | `!apps/parking/reports/screenshots/*.png` (the repo ignores `*.png`) |
| `apps/parking/PARKING_PLAN.md` | modified | §7 items 2, 3 dropped; §8 P6 row |
| `apps/parking/reports/04-frontend.md` | new | this report |

Environment only, not committed: `playwright` 1.65.2 installed into `.venv` with pip. It drives the installed Chrome (`channel="chrome"`, falling back to Edge), so no browser was downloaded.

### data.js, per encoding

- **`cells[]`**: `bits, label, center, count, capacity, bays_2025, cars[24], free[24], pct[24], outline`. `outline` is the export's region for that cell, taken from the last level's candidate with the same bits (`heavy_hitters` has no region of its own). It is a list of rings of `[lat, lon]`.
- **`params`**: `tau, T, N, day, data_bits, hist_buckets, encoding_params, dataset`.
- **`privacy`**: `revealed / kept / pruned` (all candidates of all levels), `heavy_cells`, `no_region`, and per level `{candidates, kept, pruned, no_region}`.
- **`crawl[]`**: per level, every candidate `{bits, count, heavy, outline | null}`. H3 has 10 candidates without a region (`null`): non-cells with H3 digit 7. They are counted, but not drawn.
- **`truth[]`**: every cell in the occupancy CSV (Morton 234, geohash 237, H3 193), with `occupied[24]`, capacity and a `heavy` flag.
  - Their outlines come from the plaintext encoders: the Morton box, `geohash_box`, and `h3.cell_to_boundary`.
  - For the heavy cells, the check confirms that these encoder outlines equal the export's regions within 1e-9.

Top-level keys: `day`, `snapshot_minute`, `tau`, `study_area` (the Morton box = the day's extent, used for the hatch), and the source paths.

### The page

- **Colour = % full** at the selected hour (cars ÷ 2019 sensor bays). It uses a one-hue blue ramp in 5 bins: 0–25, 25–50, 50–75, 75–90, 90–100 %.
  - Over 100 % is drawn in the status red with a thick outline and a "!" marker.
  - A cell without capacity is grey with a dashed outline and a "?" marker.
  - No cell in the current data is in either case: the smallest heavy-cell capacity is 28, and D1 caps cars at the bays.
- **"Not revealed".** The study area minus the revealed cells is hatched. Its tooltip and the legend say "below the threshold, cars may still be parked here".
- **Cell panel.**
  - An SVG bar chart of % full per hour, with the selected hour highlighted, a dashed 100 % line and a hover readout. Clicking a bar selects that hour.
  - Capacity, free spaces and % full at the hour.
  - The 3 hours with the most free spaces (ties go to the earlier hour).
  - A collapsible table view.
  - With ground truth on, the chart adds the true % as orange ticks.
- **Ground truth toggle.** It draws the below-threshold sensor cells with dashed orange outlines, under the banner "Ground truth from the sensors: comparison only, not available in a real deployment". Clicking one opens its panel, marked "Mastic did not reveal this area". For heavy cells the truth is identical to Mastic's counts (21/21, 17/17, 28/28), so they stay as they are.
- **What the collector saw.**
  - The level slider follows the export's level numbers: Morton 1–12, geohash 1–21, H3 1–15.
  - The counter reads, for example, "Regions revealed at this level: 28 (kept 20 / pruned 8) · total over all 12 levels: 460". For H3 it adds "+ n not drawable".
  - A "Zoom to level" button fits the map to the level's regions; the large regions of the early levels reach well beyond the CBD.
  - While the layer is on, the heavy cells are dimmed and the hatch is hidden.
- **State in the URL** (`#enc=h3&hour=11&truth=1&crawl=6&cell=<bits>`), so a view can be shared or bookmarked.
- **Layout.** The side panel sits next to the map on a laptop. Under 760 px it stacks below the map, the legend folds into a "Legend ▸" button, and the page has no horizontal scroll at 390 px.
- **Colours** follow the dataviz palette. The bar and truth colours (#2a78d6 / #eb6834 light, #3987e5 / #d95926 dark) pass the palette validator in both modes. Light and dark mode tokens are defined; the OSM tiles stay light.

## How to run

PowerShell, from the repo root:

```powershell
.\.venv\Scripts\python.exe -I apps\parking\scripts\build_frontend.py           # write apps\parking\frontend\data.js
.\.venv\Scripts\python.exe -I apps\parking\scripts\build_frontend.py --check   # exit 1 on any mismatch
start apps\parking\frontend\index.html                                         # open in the default browser

# headless test + screenshots (needs playwright in .venv once; uses the installed Chrome)
.\.venv\Scripts\python.exe -m pip install playwright
.\.venv\Scripts\python.exe -I apps\parking\scripts\test_frontend.py            # add --headed to watch
```

After a new `run_app.py` run, rebuild `data.js`; the page itself needs no change. The page needs internet access for Leaflet and the map tiles.

## Checks

### 1. Deterministic build: pass

I built twice into the scratchpad and once into the repo. All three files have the same SHA-256: `fb5e2e7bab910d64491de0fb0c8179ad8d503bf4d57527bb03d65cfa8fdb4bd4`. The file has no timestamps, its keys are in a fixed order, its coordinates are rounded to 7 decimals and it uses `\n` line endings.

### 2. `build_frontend.py --check`: pass

```
(a) apps/parking/frontend/data.js equals a fresh build: yes
    morton   pass: 21 heavy cells (export 21), 234 truth cells, heavy cells with truth = Mastic hours: 21/21, 12 levels, 460 regions revealed (250 kept, 210 pruned, 0 without region)
    geohash  pass: 17 heavy cells (export 17), 237 truth cells, heavy cells with truth = Mastic hours: 17/17, 21 levels, 470 regions revealed (251 kept, 219 pruned, 0 without region)
    h3       pass: 28 heavy cells (export 28), 193 truth cells, heavy cells with truth = Mastic hours: 28/28, 15 levels, 460 regions revealed (257 kept, 203 pruned, 10 without region)
all checks pass
```

The check parses `data.js` back and compares it with the source files it re-reads itself:
- **(b) Heavy cells.** Exactly the export's heavy cells. `cars` = `buckets` = `value[:24]`, the count matches, the hours sum to the count, and the labels match.
- **(c) Capacity and free spaces.** Capacity = `markers_2019` from the capacity CSV, `free = capacity − cars`, and `pct = 100·cars/capacity`. Free spaces also equal `free_markers` in the occupancy CSV for every heavy cell and hour.
- **(d) Truth layer.** It equals the occupancy CSV: cells, hours, capacity and the heavy flag.
- **(e) Outlines.** Each outline equals the export region within the rounding (5·10⁻⁸). The export region equals the plaintext encoder's cell within 1e-9.
- **(f) Crawl.** Per encoding and level:
  - the number of candidates equals the export's;
  - drawable + no region = the number of candidates;
  - bits, count and the heavy flag equal the export's;
  - the totals equal `privacy.revealed/kept/pruned`.

Negative test: in a copy of `data.js` I changed one hour count. `--check` then reported 13 problems and exited with 1.

### 3. Headless page test (`test_frontend.py`, Chrome 157): pass

```
  browser: chrome 157.0.8103.52
  morton: 21 cell polygons, privacy 460 revealed / 21 heavy
  morton: collector layer checked on 12 levels (460 regions, 0 without outline)
  geohash: 17 cell polygons, privacy 470 revealed / 17 heavy
  geohash: collector layer checked on 21 levels (470 regions, 0 without outline)
  h3: 28 cell polygons, privacy 460 revealed / 28 heavy
  h3: collector layer checked on 15 levels (460 regions, 10 without outline)
  screenshots (6): geohash_1130.png, h3_1130.png, morton_1130.png, morton_1130_cell_truth.png, morton_crawl_level6.png, phone_morton_1130.png
frontend test: pass
```

The test fails on any console error, page error, failed request or HTTP status ≥ 400. None occurred: Leaflet passed its SRI check, and the OSM tiles loaded from `file://`. For each encoding it checks:
- the number of cell polygons equals the heavy cells in `data.js`, the hatch is present, and the encoding switch and hour label are right;
- the privacy figures in the panel are right;
- clicking a cell shows its label, capacity, free spaces and % full at 11:30, the top 3 hours and 24 chart bars;
- the ground-truth toggle draws exactly the below-threshold sensor cells and shows the banner, and removes them again;
- for **every level**, the drawn crawl polygons equal the drawable candidates (and the kept ones the kept ones), the counter shows the level's and the total revealed count, and for H3 it shows the "not drawable" count.

It also checks that the play button advances the hour, and that at phone width (390 px) the page has no horizontal scroll and all cells are drawn.

Two problems found by the test were fixed in the page:
- The legend covered some cells after the map fitted to them. The fit now leaves room for the legend.
- On a phone, the two-line OSM attribution covered the legend button. The bottom-left controls now sit above it.

### 4. No protocol or result changes: pass

`git status -- src tests Cargo.toml apps/parking/results` is empty. The only modified tracked files are `.gitignore` and `PARKING_PLAN.md`.

## Decisions needed

1. **Pruned cells at the finest level.** The "What's private" panel says that cells below the threshold are not output. That is true, but the crawl does reveal the totals of the candidates it evaluates and then prunes, including the finest-level cells just below T whose parent region was heavy: Morton 35, geohash 39 and H3 48. The panel says this in a note and links to the collector layer. Do you want the wording stronger or weaker? The prompt's "any cell below the threshold stayed hidden" is literally true only for cells inside regions pruned earlier.
2. **Each revealed region carries 24 hourly counts, not one total.** With `Weight::HourOfDay`, every candidate's revealed value is its full hour vector, not just a total. The page says so ("each with its 24 hourly counts"). This is more leakage than a 1-bucket crawl, which is worth stating in the thesis.
3. **Commit the screenshots?** They total 5.6 MB of PNG (about 1 MB each, because of the map tiles). Alternatives: commit only `morton_1130.png` and `morton_crawl_level6.png`, or regenerate them on demand with `test_frontend.py` and leave the `.gitignore` exception out.
4. **Playwright stays a local dev dependency.** The repo has no Python requirements file, so it isn't recorded anywhere except in this report and the docstring of `test_frontend.py`.
5. **OSM tiles from `file://`.** They loaded fine today. OSM's tile policy asks for a Referer, which a page opened from disk doesn't send, so tile access could be blocked in future. For a public demo, serve the folder over HTTP (for example `python -m http.server`), or switch to a tile provider with a key.

## What to commit

Both new scripts set `sys.dont_write_bytecode`. `__pycache__/` is git-ignored anyway, and nothing in it is in the list.

```powershell
git add .gitignore `
        apps/parking/PARKING_PLAN.md `
        apps/parking/scripts/build_frontend.py `
        apps/parking/scripts/test_frontend.py `
        apps/parking/frontend/index.html `
        apps/parking/frontend/data.js `
        apps/parking/reports/04-frontend.md `
        apps/parking/reports/screenshots/morton_1130.png `
        apps/parking/reports/screenshots/geohash_1130.png `
        apps/parking/reports/screenshots/h3_1130.png `
        apps/parking/reports/screenshots/morton_1130_cell_truth.png `
        apps/parking/reports/screenshots/morton_crawl_level6.png `
        apps/parking/reports/screenshots/phone_morton_1130.png
```

Commit message: `P6: parking frontend (map, hour slider, free spaces, privacy and collector views) built from the saved Mastic results`
