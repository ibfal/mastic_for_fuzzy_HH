#!/usr/bin/env python3
"""Plaintext ground truth for ride-sharing heavy hitters on Mosaic's grid.

Counts pickups per 0.001-degree cell exactly as `src/geo.rs` (MosaicGrid) does, prints every cell
whose count reaches the threshold, and optionally checks a Mastic driver log against it.

    python3 scripts/check_heavy_hitters.py data/mosaic/sample_busiest_day.csv --threshold 0.02 \
        --mastic-log day.log --mosaic-results data/mosaic/results.json
"""

import argparse
import csv
import json
import math
import re
from collections import Counter

MIN_LAT = 30.267 - 1.0
MIN_LON = -97.743 - 1.0
SCALE = 1000.0
AXIS_BITS = 11
GRID_SIZE = 2000


def to_cell(v):
    # Rust's f64::round rounds half away from zero; Python's round() does not.
    return min(max(math.floor(v + 0.5), 0), GRID_SIZE - 1)


def to_grid(lat, lon):
    return to_cell((lat - MIN_LAT) * SCALE), to_cell((lon - MIN_LON) * SCALE)


def grid_to_bits(lat_grid, lon_grid):
    return "".join(
        f"{(lon_grid >> i) & 1}{(lat_grid >> i) & 1}" for i in reversed(range(AXIS_BITS))
    )


def bits_to_grid(bits):
    lat = int(bits[1::2], 2)
    lon = int(bits[0::2], 2)
    return lat, lon


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("csv", help="Mosaic sample_busiest_*.csv")
    parser.add_argument("--threshold", type=float, default=0.02)
    parser.add_argument("--mastic-log", help="driver output to compare against")
    parser.add_argument("--mosaic-results", help="Mosaic results.json to compare against")
    args = parser.parse_args()

    with open(args.csv, newline="") as f:
        cells = [
            to_grid(float(row["start_location_lat"]), float(row["start_location_long"]))
            for row in csv.DictReader(f)
        ]
    counts = Counter(cells)
    # Same as the driver: max(1, floor(threshold * num_clients)).
    threshold = max(1, int(args.threshold * len(cells)))
    expected = {grid_to_bits(*c): n for c, n in counts.items() if n >= threshold}

    print(f"{len(cells)} rides, {len(counts)} distinct cells, threshold {threshold}")
    print(f"{len(expected)} heavy-hitter cells:")
    for bits, n in sorted(expected.items(), key=lambda kv: -kv[1]):
        lat_grid, lon_grid = bits_to_grid(bits)
        lat, lon = MIN_LAT + lat_grid / SCALE, MIN_LON + lon_grid / SCALE
        print(f"  {bits}  grid ({lat_grid}, {lon_grid})  ({lat:.3f}, {lon:.3f})  count {n}")

    ok = True
    if args.mastic_log:
        with open(args.mastic_log) as f:
            found = {
                m.group(1): int(m.group(2))
                for m in re.finditer(r"Value \(([01]+)\).*?Count: \[(\d+)", f.read())
            }
        missing = expected.keys() - found.keys()
        extra = found.keys() - expected.keys()
        wrong = {b for b in expected.keys() & found.keys() if expected[b] != found[b]}
        ok = not (missing or extra or wrong)
        print(f"\nMastic: {len(found)} cells; missing {len(missing)}, extra {len(extra)}, "
              f"wrong count {len(wrong)} -> {'MATCH' if ok else 'MISMATCH'}")

    if args.mosaic_results:
        with open(args.mosaic_results) as f:
            mosaic = json.load(f)
        mosaic_cells = {(int(a), int(b)) for a, b in mosaic["heavy_hitters"]}
        ours = {bits_to_grid(b) for b in expected}
        print(f"\nMosaic results ({mosaic['config']}): {len(mosaic_cells)} points; "
              f"{len(ours & mosaic_cells)} of our {len(ours)} cells are among them. "
              "Mosaic counts points within delta of a point, so the sets are not expected to match.")

    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
