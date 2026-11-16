# City map: four companies report to the city

A separate program on top of the Mastic code in `src/`. It uses the busiest RideAustin day
(March 17, 2017: 21,581 pickups, `data/mosaic/sample_busiest_day.csv`). Each ride gets a synthetic company:
Uber, Lyft, Waymo or RideAustin. Every company then sends its rides to the city as Mastic
reports:

- **α** is a location cell, in one of the four location encodings. There are two kinds of
  query, one per tab of the map:
  - **Pickups:** α is the pickup point.
  - **Drop-offs:** α is the drop-off point (`alpha = "dropoff"` in the config), so the heavy cells
    are popular destinations.
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
city/run.sh                            # both tabs x four encodings, about 2 minutes each
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

## Privacy

The two tabs are two queries over the same rides. Together the city learns the heavy pickup cells
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

The repository-wide prototype warning in the top-level README (the PRG's constant control bits)
applies here too.
