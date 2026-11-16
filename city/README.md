# City map: four companies report to the city

A separate program on top of the Mastic code in `src/`. It uses the busiest RideAustin month
(115,174 pickups, `data/mosaic/sample_busiest_month.csv`). Each ride gets a synthetic company:
Uber, Lyft, Waymo or RideAustin. Every company then sends its rides to the city as Mastic
reports:

- **α** is the pickup cell, in one of the four location encodings.
- **β** is a one-hot over the companies: `weight = "company"`, `companies = [...]` (4 buckets).

The city (the collector) learns the cells with at least 0.1% of the rides (115). For each of those
cells it learns one count per company, and nothing else. The map shows exactly that.

## Run

```bash
city/run.sh                 # all four encodings, about a minute each
city/run.sh morton h3       # only some
open city/dist/city-map.html
```

`run.sh`:

1. Creates `city/data/rides_month_companies.csv` if it is missing.
2. Runs two servers and the driver for each `city/configs/city-month-<encoding>.toml`.
3. Checks each result against the plaintext answer (`check_companies.py` prints `MATCH` per
   encoding).
4. Builds the single-file map `city/dist/city-map.html`.

## Files

| File | What it does |
|---|---|
| `make_company_rides.py` | Adds a seeded random `company` column to the month data |
| `configs/city-month-*.toml` | One config per encoding: Mosaic grid, geohash, H3, Morton |
| `run.sh` | Runs Mastic for each encoding, then checks the results and builds the map |
| `check_companies.py` | Plaintext check: the same heavy cells and the same per-company counts |
| `map.html` → `build_map.py` | Map template; the builder inlines the final heavy cells |

## The synthetic companies

The split is deliberately uneven. Citywide it comes out at about Uber 45%, Lyft 32%,
RideAustin 12% and Waymo 11%.

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

- The dropdown switches between the four encodings.
- **Company mix**: one pie per heavy cell, sized by its number of rides.
- **Leading company**: each cell's real outline, coloured by the company with the most rides
  there.
- Clicking a company in the legend shades every cell by that company's share.

## Privacy

The colours are computed only from what Mastic reveals to the city: per-company counts for cells
above the threshold. The city learns nothing about individual rides, or about cells below the
threshold.

Two things are revealed by design:

- **Local market share.** Each company's share in every heavy cell is revealed, and a small
  company's count in a cell can be small. That is the aggregate the city asked for.
- **The threshold applies to the cell total, not to each company.** A cell with 115 rides can
  still show that one company had 3 of them.

The repository-wide prototype warning in the top-level README (the PRG's constant control bits)
applies here too.
