# Mosaic RideAustin data

Cleaned RideAustin pickup data from the Mosaic artifact:

- Record: "Mosaic: A Modular Framework for Private Fuzzy Heavy Hitters"
  (Garimella, Miao, Nolan, Pham Van Long, Sitaraman), https://zenodo.org/records/20768775,
  DOI `10.5281/zenodo.20768775`, MIT license.
- Source file: `fuzzyheavyhitters.zip`
  (sha256 `05119ce92cfd22a48c399f5565e51617ce26ed63a20488b17f73fe2464bd2e9e`), folder `data/` plus
  `results.json`.

| File | Contents |
| --- | --- |
| `sample_busiest_day.csv` | 21,581 rides |
| `sample_busiest_week.csv` | 59,040 rides |
| `sample_busiest_month.csv` | 115,174 rides |
| `start_busiest_{day,week,month}.json` | Pickups on Mosaic's grid as `[lat_grid, lon_grid]` |
| `results.json` | Mosaic's experiment results |

CSV columns of interest: `start_location_lat` (16), `start_location_long` (15), `hour_of_day` (21),
`requested_car_category` (12). Coordinates are rounded to 0.001 degrees.

Mosaic's grid: `lat_grid = round((lat - 29.267) * 1000)`, `lon_grid = round((lon + 98.743) * 1000)`,
11 bits per axis.

The underlying ride data comes from RideAustin; check its terms before redistributing.
