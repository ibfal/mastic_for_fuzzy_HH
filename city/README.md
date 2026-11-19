# City map: four companies report to the city

A separate program on top of the Mastic code in `src/`. It uses the busiest RideAustin day
(March 17, 2017: 21,581 pickups, `data/mosaic/sample_busiest_day.csv`). Each ride gets a synthetic company:
Uber, Lyft, Waymo or RideAustin. Every company then sends its rides to the city as Mastic
reports:

- **α** is a location cell, in one of the four location encodings. There are two kinds of
  query, one per tab of the map:
  - **Pickups:** α is the pickup point.
  - **Drop-offs:** α is the drop-off point (`alpha = "dropoff"` in the config), so the heavy cells
    are popular destinations. The hour block is the drop-off hour (`dropoff_hour`, from
    `completed_on`) instead of the pickup hour.
  - **Neighborhoods:** α is the pickup point and β is just the count (`weight = "constant"`, 1
    bucket). The heavy cells are then grouped into neighborhoods (below).
- **β** has five one-hot blocks:

  | Block | Buckets |
  |---|---|
  | company | 4 |
  | pickup hour | 24 |
  | car category | 4 |
  | surge | 2 |
  | zone (airport, downtown, UT campus, elsewhere): the drop-off zone on the Pickups tab, the pickup zone (`pickup_zone`) on the Drop-offs tab | 4 |

  That is 38 buckets in total.

The city (the collector) learns the cells with at least 0.1% of the rides (21). For each of those
cells it learns the totals of each block (rides per company, per hour, and so on), and nothing
else. The map shows exactly that.

### Why the day and not the month

The servers keep every client's full β vector for every candidate cell of a crawl level. Memory
therefore grows with rides × candidates × β length.

- The month (115,174 rides) worked with the company block alone (4 buckets).
- With all 38 buckets at 0.1% the month needs very roughly 70 GB per server, and both servers
  run on one machine. The day needs about a fifth of that.
- Shrinking the stored vectors would weaken the crawl's consistency check, which uses every entry
  of β, so the code was left as is.

## Run

```bash
city/run.sh                            # three tabs x four encodings: ~2 min each with weights, ~15 s plain
city/run.sh dropoff-morton pickup-h3   # only some runs (<tab>-<encoding>)
open city/dist/city-map.html
```

`run.sh`:

1. Creates `city/data/rides_day_companies.csv` if it is missing.
2. Runs two servers and the driver for each `city/configs/city-day-<tab>-<encoding>.toml`,
   writing `city/results/<tab>-<encoding>.json`.
3. Checks each result against the plaintext answer (`check_companies.py` prints `MATCH` per
   encoding).
4. Builds the single-file map `city/dist/city-map.html`.

## Files

| File | What it does |
|---|---|
| `make_company_rides.py` | Adds a seeded random `company` column to the day data |
| `configs/city-day-{pickup,dropoff}-*.toml` | One config per tab and encoding: Mosaic grid, geohash, H3, Morton |
| `run.sh` | Runs Mastic for each tab and encoding, then checks the results and builds the map |
| `neighborhoods.py` | Groups the heavy cells of the plain runs into neighborhoods (used by `build_map.py`) |
| `check_companies.py` | Plaintext check: the same heavy cells and the same value in every block |
| `map.html` → `build_map.py` | Map template; the builder inlines the final heavy cells |

## The synthetic companies

The split is deliberately uneven. Citywide it comes out at about Uber 46%, Lyft 30%,
RideAustin 12% and Waymo 12%.

Each company is also stronger around one area (`HOTSPOTS` in `make_company_rides.py`):

| Company | Stronger around |
|---|---|
| Waymo | downtown |
| Lyft | UT campus |
| RideAustin | north Austin |
| Uber | the airport |

Without the hotspots every cell would show the same mix, apart from noise, and the map would be
all Uber. `--uniform` turns them off.

## The map

- Two tabs, **Pickups** and **Drop-offs**: the same rides, with cells at where they start or where
  they end. On the Drop-offs tab the zone block is the pickup zone, so a destination cell shows
  e.g. how many of its rides came from the airport.
- The dropdown switches between the four encodings.
  - Morton drops 41 drop-offs that fall outside its box; the page says so.
  - The Mosaic grid clamps them to its edge.
- By default each heavy cell's real outline is coloured by the company with the most rides there.
  A clearer lead gives a darker fill.
- Clicking a company in the legend shows only that company: cells shaded by its number of rides.
  Clicking it again, or "Show all companies", goes back.
- Clicking a cell opens a pane at the bottom with that cell's full split: rides per company,
  pickup hour, car category, surge and drop-off zone (pickup zone on the Drop-offs tab).
  - Those last four are totals over all companies. The blocks are revealed separately, not per
    company.
  - Joining them (e.g. company × hour) would need one bucket per pair.
- **Filter cells** keeps only the heavy cells with at least N rides in one bucket: a company, a
  4-hour pickup period (00:00–04:00, …, 20:00–24:00), a car category, surge or a zone. N defaults to the threshold (21).
  - It only reads what the city already learned, so it reveals nothing new.
  - At N ≥ 21 the answer is complete. A cell with 21 rides between 20:00 and 24:00 has at least 21 rides in total,
    so it is already a heavy cell.
  - Below 21 the answer is only partial: cells under the overall threshold never reached the city.
    The page says so.
  - Getting a complete answer for a lower threshold (e.g. 0.1% of the evening rides) needs a new
    Mastic query, for example with the companies sending only their evening rides.

## Neighborhoods

The third tab groups the heavy pickup cells of the plain-count runs into neighborhoods. The rules
are in `neighborhoods.py`:

1. **Touching cells connect.** Heavy cells that share a side or a corner (for H3, a side) are
   connected, however far the chain goes.
2. **One-cell gaps are bridged.** Connected areas only one cell apart are joined, and the cell in
   the gap becomes part of the neighborhood (`GAP = 1`).
3. **Holes are filled.** Cells enclosed by a neighborhood belong to it. A heavy cell, or a smaller
   neighborhood, inside a hole joins the surrounding neighborhood.
4. **Minimum size.** A neighborhood needs at least 2 heavy cells (`MIN_CELLS = 2`), so a lone
   heavy cell isn't one.

**Cells added by rules 2 and 3** are below the threshold. Some of their counts are known anyway:
at each level the crawl reveals the count of both children of every heavy prefix, heavy or not.
So the last level already gave the city the exact count of every cell next to a heavy cell.

- **Count revealed by the crawl:** the cell is shaded like a heavy cell and its pickups are added
  to the neighborhood. This reveals nothing new.
- **Count unknown:** the cell was never revealed. It is tinted red and adds nothing.

Each neighborhood shows heavy cells + revealed cells + unknown cells. For example, Morton's
biggest neighborhood is 5,246 pickups in 107 heavy cells, plus 234 in 17 cells with revealed
counts, plus 11 cells with unknown counts: 5,480 pickups in total.

Across all neighborhoods, the crawl had already revealed 22 of the 37 added cells on Morton and
the Mosaic grid, 19 of 33 on H3, and 51 of 73 on geohash (many of them 0).

Counting the unknown cells would need a new query, which leaks more. Asking per cell reveals
small counts (a count of 1 is one person's pickup location). Asking for one total per
neighborhood lets the city subtract the known counts. Either way the leakage adds to the first
query's.

**Borders** follow the outer edge of all of a neighborhood's cells. Each neighborhood has exactly
one border, with no inner rings.

Results on the busiest day:

| Encoding | Neighborhoods | Heavy cells in a neighborhood | Biggest neighborhood |
|---|---|---|---|
| Mosaic grid, Morton | 8 | 138 of 158 | 107 heavy cells + 28 added, 74% of the pickups in heavy cells |
| Geohash | 10 | 135 of 164 | 71% of the pickups |
| H3 | 10 | 136 of 157 | 71% of the pickups |

Downtown's connected areas are mostly one cell apart, so bridging the gaps joins them into one
large neighborhood.

Two other rules were tried and rejected:

- **Chaining at 5 steps:** linked the whole centre into one blob (126 of 158 cells).
- **Every pair within 5 steps:** split connected areas apart and grouped cells with gaps between
  them.

The grouping runs in `build_map.py` on the cells the city already learned, so it reveals nothing
new.

## Privacy

The Pickups and Drop-offs tabs are two queries over the same rides, and the Neighborhoods tab is a
third (pickups, count only). Together the city learns the heavy pickup cells
and the heavy drop-off cells, each with its block totals. It still does not learn which pickup
goes with which drop-off, apart from the three named zones.

The colours and the cell pane are computed only from what Mastic reveals to the city: the block
totals of the cells above the threshold. The city learns nothing about individual rides, or about cells below the
threshold.

Two things are revealed by design:

- **Local market share.** Each company's share in every heavy cell is revealed, and a small
  company's count in a cell can be small. That is the aggregate the city asked for.
- **The threshold applies to the cell total, not to each company.** A cell with 21 rides can
  still show that one company had 1 of them.

The [security notes](../README.md#security-notes) in the top-level README apply here too. The
control-bit leak is fixed. The driver still sees every input, and traffic is still unencrypted.
