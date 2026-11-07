<h1 align="center">Mastic: Private Weighted Heavy-Hitters and Attribute-Based Metrics
  <a href="https://github.com/TrustworthyComputing/mastic/actions/workflows/ci-build.yml"><img src="https://github.com/TrustworthyComputing/mastic/workflows/ci-build/badge.svg"></a>
  <a href="https://github.com/TrustworthyComputing/mastic/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg"></a>
</h1>

A pickup location is encoded as a bit string `alpha` whose prefixes are nested regions. Mastic's
two-server tree crawl then finds every cell whose number of pickups reaches a threshold, without
either server seeing individual locations.

> **Security status: research prototype, do not rely on its privacy.** The PRG in `src/prg.rs`
> currently returns constant control bits, so every VIDPF correction word contains `alpha` in the
> clear: **either aggregator can read every client's location from its key**. Tests that show this
> are on the `security-tests` branch (`tests/security_control_bits_test.rs`); the one-line fix is
> described under [Known limitations](#known-limitations). Results about correctness, cost and which
> cells are heavy are unaffected.

## Repository layout

| Path | Purpose |
|---|---|
| `src/vidpf.rs`, `src/prg.rs` | VIDPF key generation and evaluation; the AES-based PRG |
| `src/collect.rs` | One aggregator's state: tree crawl, checks, aggregation, pruning |
| `src/rpc.rs` | The RPC interface between the driver and the aggregators (tarpc) |
| `src/config.rs` | TOML config and command-line flags |
| `src/geo.rs` | Location encoders: `MosaicGrid`, `Geohash`, `H3`, `Morton` |
| `src/dataset.rs` | Loads a ride CSV and encodes each ride as `(alpha, bucket)` |
| `src/bin/server.rs` | An aggregator (run two) |
| `src/bin/driver.rs` | Simulates the clients and acts as leader/collector |
| `src/configs/` | Configs for every dataset and encoding (see below) |
| `tests/` | Unit and integration tests |
| `scripts/` | Plaintext checker, synthetic data generator, viewer scripts |
| `data/mosaic/` | RideAustin samples and Mosaic's results, from the Mosaic artifact |
| `data/synthetic/` | The 10-landmark dataset |
| `viz/` | The crawl viewer |
| `artifact/` | The original Mastic paper's artifact (configs, plots) |

## Requirements and build

- **Rust**: tested with rustc 1.99.1. Install with [rustup](https://www.rust-lang.org/tools/install);
  on Windows the MSVC toolchain needs the Visual Studio Build Tools.
- **Python 3**: for the checker and the viewer scripts. The H3 checks need Uber's `h3` package:

  ```bash
  python3 -m venv .venv
  .venv/bin/pip install -r scripts/requirements.txt      # Windows: .venv\Scripts\pip
  ```

Build and test:

```bash
cargo build --release
cargo test --release
```

Always use `--release` for runs: debug builds are much slower.

## Quick start: a dataset with a known answer

`data/synthetic/ten_locations.csv` has 165 rides at 10 Austin landmarks. At threshold 0.1
(16 rides) exactly four are heavy: the airport (50), the Capitol (40), UT Tower (30) and the
Domain (20).

```bash
# two aggregators, in two shells
target/release/server --config src/configs/synthetic-ten-locations-morton.toml --server_id 0
target/release/server --config src/configs/synthetic-ten-locations-morton.toml --server_id 1

# the driver, in a third shell
target/release/driver --config src/configs/synthetic-ten-locations-morton.toml -n 1000 > morton.log

# compare with the plaintext answer: prints MATCH
python3 scripts/check_heavy_hitters.py data/synthetic/ten_locations.csv --threshold 0.1 \
    --encoding morton --bbox 29.9005,30.9245,-98.0505,-97.0265 --axis-bits 10 --mastic-log morton.log
```

Stop the servers with Ctrl+C. Replace `morton` with `mosaic-grid`, `geohash` or `h3` to try
the other encodings (see [Checking results](#checking-results-against-the-plaintext-ground-truth)
for their checker flags).

## Running on the RideAustin data

The same three commands work with any `src/configs/ride-austin-{day,week,month}-*.toml`. Run
from the repository root, because dataset paths are relative.

- **`-n` caps the number of rides**, taken from the start of the file. The files are sorted by
  pickup time, so `-n 1000` means the first hour or so of the day, not a random sample. To use a
  whole file pass at least its size: 21,581 (day), 59,040 (week), 115,174 (month).
- **Dropped rides.** After loading and again after the results, the driver prints

  ```
  *** DROPPED RIDES: 0 of 21581 outside the area of Morton { ... }; 21581 rides used ***
  ```

  Rides outside the encoded area (outside a geohash or H3 prefix, or a Morton box) are not sent.
  The Mosaic grid clamps them to its edge instead. None of the provided configs drops any ride.
- **Output.** Each heavy cell is printed as

  ```
  Value (0011…) 	 Cell: morton (374, 310) 	 Location: (30.2750, -97.7400) 	 Count: [40, 40]
  ```

  `Count` is `[bucket counts…, number of clients]`; with `weight = "constant"` both are the
  number of rides.
- The "busiest month" file covers 2017-03-31 to 2017-04-13 (14 days), as sampled by Mosaic.

## Location encodings

| Encoding | `alpha` | Cell | Outside the area | Prefixes |
|---|---|---|---|---|
| `mosaic_grid` | 22 bits: 11-bit grid indices, interleaved lon/lat | Mosaic's 0.001° grid (≈111 × 96 m) | clamped to the edge | aligned blocks of cells |
| `geohash` | `bits` after a fixed `prefix` (`9v6` covers Austin) | geohash cell; 20 bits ≈ 152 × 132 m | dropped | exact nested boxes |
| `h3` | 3 bits per resolution below a fixed `prefix` cell | H3 hexagon; res 9 ≈ 0.1 km² | dropped | hexagons nest only approximately; levels inside a 3-bit digit are groups of children |
| `morton` | `2 * axis_bits` bits, interleaved lon/lat | `(max - min) / 2^axis_bits` per axis | dropped | aligned blocks of cells |

**Morton (Z-order).** The Mosaic grid and geohash are both Morton codes, but with fixed boxes.
`morton` takes any bounding box `[min_lat, max_lat) x [min_lon, max_lon)` and splits each axis into
`2^axis_bits` equal cells. To choose its parameters:

1. Choose the cell size `c`.
2. Set `axis_bits = ceil(log2(data extent / c))` and make each side `2^axis_bits * c`.
3. The RideAustin pickups span about 0.75° x 0.77°, so 0.001° cells need `axis_bits = 10`
   (a 1.024° box).
4. Put the edges half a data step off the 3-decimal coordinates (`x.xxx5`), so no ride sits on a
   cell boundary.

With the box `29.9005..30.9245, -98.0505..-97.0265` and `axis_bits = 10` the cells are **exactly
Mosaic's grid cells** with 20 tree levels instead of 22.

Every prefix is an aligned block, and a block's count is the sum of its children's, so pruning never
loses a heavy cell. The box only decides which neighbouring cells are added together at intermediate
levels.

## Configuration

A dataset config looks like this:

```toml
mode.weighted_heavy_hitters.threshold = 0.005   # fraction of the rides used

data_bits = 20                                  # must equal the encoding's length
hist_buckets = 1                                # must be 1 for weight = "constant"

server_0 = "0.0.0.0:8000"
server_1 = "0.0.0.0:8001"

add_report_share_batch_size = 1000
query_flp_batch_size = 50000                    # larger batches exceed tarpc's 8 MiB frame limit
zipf_unique_buckets = 1000                      # still required; only used without a dataset
zipf_exponent = 1.03

[dataset]
path = "data/mosaic/sample_busiest_day.csv"
encoding.morton = { min_lat = 29.9005, max_lat = 30.9245, min_lon = -98.0505, max_lon = -97.0265, axis_bits = 10 }
weight = "constant"
```

Other encodings:

```toml
encoding = "mosaic_grid"                                          # data_bits = 22
encoding.geohash = { prefix = "9v6", bits = 20 }                  # data_bits = bits
encoding.h3 = { prefix = "82489ffffffffff", resolution = 9 }      # data_bits = 3 * (9 - 2) = 21
```

- **Threshold.** A prefix is kept when its count is at least `max(1, floor(threshold * N))`, where
  `N` is the number of rides used, after dropping and `-n`.
- **Top-level keys go above `[dataset]`.** Any key after that header belongs to the dataset table,
  and unknown keys are silently ignored.
- `data_bits` and `hist_buckets` are checked against the encoding and weight when the dataset is
  loaded. The test `every_dataset_config_matches_its_encoder` checks all provided configs.

## Provided configs and expected results

`src/configs/<dataset>-<encoding>.toml`, with `<dataset>` one of `ride-austin-day`,
`ride-austin-week`, `ride-austin-month`, `synthetic-ten-locations`. The heavy cells below use
threshold 0.005 on the ride data and 0.1 on the synthetic data, which gives 4 cells for every
encoding.

| Config suffix | n | Cell | Heavy cells (day / week / month) | Key size |
|---|---|---|---|---|
| `mosaic-grid` | 22 | Mosaic's 0.001° grid | 8 / 5 / 8 | 2,013 B |
| `geohash` | 20 | geohash `9v6` + 20 bits | 18 / 13 / 16 | 1,833 B |
| `h3` | 21 | H3 resolution 9 | 26 / 27 / 29 | 1,923 B |
| `morton` | 20 | Mosaic's cells, 1.024° box | 8 / 5 / 8 (same cells as `mosaic-grid`) | 1,833 B |
| `morton-geohash21-area` | 20 | square, area of a geohash 9v6 + 21-bit cell (0.0101 km²) | 8 / 5 / 8 | 1,833 B |
| `morton-h3res10-area` | 20 | square, area of an Austin H3 res-10 cell (0.0172 km²) | 12 / 13 / 14 | 1,833 B |
| `morton-shifted` | 20 | the `morton` box shifted by half a cell | 19 / 13 / 17, see the warning below | 1,833 B |
| `morton-delta5` (week only) | 14 | 0.011° blocks, the size of Mosaic's δ-neighbourhood | 10 (at threshold 0.02) | — |

Key size is one aggregator's VIDPF key; each client also sends a 176-byte FLP proof share per aggregator.


## Checking results against the plaintext ground truth

`scripts/check_heavy_hitters.py` encodes the rides exactly as `src/geo.rs` does and prints every
cell at or above the threshold (and the dropped-ride count). With `--mastic-log` it compares a
driver log cell by cell and prints `MATCH` or `MISMATCH`. H3 uses Uber's reference library,
independently of the Rust code.

```bash
python3 scripts/check_heavy_hitters.py data/mosaic/sample_busiest_day.csv --threshold 0.005 --mastic-log day.log
python3 scripts/check_heavy_hitters.py data/mosaic/sample_busiest_day.csv --threshold 0.005 \
    --encoding geohash --prefix 9v6 --bits 20 --mastic-log day-geohash.log
python3 scripts/check_heavy_hitters.py data/mosaic/sample_busiest_day.csv --threshold 0.005 \
    --encoding h3 --prefix 82489ffffffffff --resolution 9 --mastic-log day-h3.log
python3 scripts/check_heavy_hitters.py data/mosaic/sample_busiest_day.csv --threshold 0.005 \
    --encoding morton --bbox 29.9005,30.9245,-98.0505,-97.0265 --axis-bits 10 --mastic-log day-morton.log
```

For Morton, copy `--bbox` and `--axis-bits` verbatim from the config, so both sides parse the same
numbers. `--mosaic-results data/mosaic/results.json` (grid only) reports the overlap with Mosaic's
heavy points.

`scripts/make_synthetic_rides.py <out.csv>` regenerates the synthetic dataset and checks that the 10
landmarks fall in 10 distinct cells under every encoding.

## Crawl viewer

`viz/index.html` shows four synced maps (grid, geohash, H3, Morton) and a slider over the crawl
levels:

- Every candidate prefix at the selected level is drawn as its region: filled and coloured by count
  if the crawl keeps it, dashed if it is pruned. This is what the collector learns, including the
  counts of pruned prefixes.
- Each pane's menu can show any saved run of the selected dataset.
- Arrow keys step through the levels and space plays them.

**Save runs.** The driver's `--export <file>` writes every crawl level and the heavy hitters as JSON
(weighted heavy hitters only). `scripts/run_encodings.sh` does this for each encoding of a dataset:

```bash
scripts/run_encodings.sh synthetic-ten-locations          # mosaic-grid, geohash, h3, morton
scripts/run_encodings.sh ride-austin-day
scripts/run_encodings.sh ride-austin-week morton-shifted morton-h3res10-area
```

It writes `viz/results/<dataset>-<encoding>.json` and the logs in `viz/logs/`. Then it runs
`scripts/build_viz.py`, which writes `viz/results/manifest.json` and the single-file
`viz/dist/mastic-crawl.html`.

The script needs `nc`, a `python3` command, and ports 8000 and 8001 to be free. On Windows (Git Bash)
these are often missing or taken: the Microsoft Store `python3` stub is not Python. In that case
run the servers and the driver with `--export` by hand, then `python scripts/build_viz.py`.

**View.**

```bash
python3 -m http.server 8080 --bind 127.0.0.1 --directory viz
```

Then open http://localhost:8080. The page reads `viz/results/` on every load.


## Disclaimer

This is software for a research prototype and not production-ready code. This repository builds upon
[plasma](https://github.com/TrustworthyComputing/plasma),
[heavy-hitters](https://github.com/henrycg/heavyhitters), and
[libprio-rs](https://github.com/divviup/libprio-rs/tree/main).

This is a Rust implementation of the ideas presented in Mastic Verifiable
Distributed Aggregation Function (VDAF) individual Internet-Draft. You can read
the draft on the [Datatracker Page](https://datatracker.ietf.org/doc/draft-mouris-cfrg-mastic/).

## Acknowledgments
This work was partially supported by the National Science Foundation (Award #2239334).
