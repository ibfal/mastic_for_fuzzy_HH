#!/usr/bin/env python3
"""Plaintext ground truth for ride-sharing heavy hitters.

Encodes pickups exactly as `src/geo.rs` does (Mosaic's grid or geohash), prints every cell whose
count reaches the threshold, and optionally checks a Mastic driver log against it.

    python3 scripts/check_heavy_hitters.py data/mosaic/sample_busiest_day.csv --threshold 0.005 \
        --mastic-log day.log
    python3 scripts/check_heavy_hitters.py data/mosaic/sample_busiest_day.csv --threshold 0.005 \
        --encoding geohash --prefix 9v6 --bits 20 --mastic-log day-geohash.log
"""

import argparse
import csv
import json
import math
import re
from collections import Counter

# Mosaic's grid.
MIN_LAT = 30.267 - 1.0
MIN_LON = -97.743 - 1.0
SCALE = 1000.0
AXIS_BITS = 11
GRID_SIZE = 2000

GEOHASH_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"


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


def geohash_bits(lat, lon, n):
    lat_range, lon_range = [-90.0, 90.0], [-180.0, 180.0]
    out = []
    for i in range(n):
        r, v = (lon_range, lon) if i % 2 == 0 else (lat_range, lat)
        mid = (r[0] + r[1]) / 2
        if v >= mid:
            out.append("1")
            r[0] = mid
        else:
            out.append("0")
            r[1] = mid
    return "".join(out)


def geohash_box(bits):
    lat_range, lon_range = [-90.0, 90.0], [-180.0, 180.0]
    for i, b in enumerate(bits):
        r = lon_range if i % 2 == 0 else lat_range
        mid = (r[0] + r[1]) / 2
        if b == "1":
            r[0] = mid
        else:
            r[1] = mid
    return lat_range, lon_range


def bits_to_base32(bits):
    whole = len(bits) - len(bits) % 5
    s = "".join(GEOHASH_BASE32[int(bits[i:i + 5], 2)] for i in range(0, whole, 5))
    return s + (f"[{bits[whole:]}]" if whole < len(bits) else "")


class MosaicGrid:
    def encode(self, lat, lon):
        return grid_to_bits(*to_grid(lat, lon))

    def describe(self, bits):
        lat_grid, lon_grid = bits_to_grid(bits)
        lat, lon = MIN_LAT + lat_grid / SCALE, MIN_LON + lon_grid / SCALE
        return f"grid ({lat_grid}, {lon_grid})  ({lat:.4f}, {lon:.4f})"


class Geohash:
    def __init__(self, prefix, bits):
        self.prefix = "".join(f"{GEOHASH_BASE32.index(c):05b}" for c in prefix)
        self.bits = bits

    def encode(self, lat, lon):
        full = geohash_bits(lat, lon, len(self.prefix) + self.bits)
        return full[len(self.prefix):] if full.startswith(self.prefix) else None

    def describe(self, bits):
        full = self.prefix + bits
        (lat0, lat1), (lon0, lon1) = geohash_box(full)
        return f"{bits_to_base32(full)}  ({(lat0 + lat1) / 2:.4f}, {(lon0 + lon1) / 2:.4f})"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("csv", help="Mosaic sample_busiest_*.csv")
    parser.add_argument("--threshold", type=float, default=0.005)
    parser.add_argument("--encoding", choices=["mosaic_grid", "geohash"], default="mosaic_grid")
    parser.add_argument("--prefix", default="9v6", help="geohash prefix shared by all rides")
    parser.add_argument("--bits", type=int, default=20, help="geohash bits after the prefix")
    parser.add_argument("--mastic-log", help="driver output to compare against")
    parser.add_argument("--mosaic-results", help="Mosaic results.json (mosaic_grid only)")
    args = parser.parse_args()

    enc = MosaicGrid() if args.encoding == "mosaic_grid" else Geohash(args.prefix, args.bits)
    with open(args.csv, newline="") as f:
        encoded = [
            enc.encode(float(row["start_location_lat"]), float(row["start_location_long"]))
            for row in csv.DictReader(f)
        ]
    cells = [c for c in encoded if c is not None]
    if len(cells) < len(encoded):
        print(f"Skipped {len(encoded) - len(cells)} rides outside the encoded area")
    counts = Counter(cells)
    # Same as the driver: max(1, floor(threshold * num_clients)).
    threshold = max(1, int(args.threshold * len(cells)))
    expected = {bits: n for bits, n in counts.items() if n >= threshold}

    print(f"{len(cells)} rides, {len(counts)} distinct cells, threshold {threshold}")
    print(f"{len(expected)} heavy-hitter cells:")
    for bits, n in sorted(expected.items(), key=lambda kv: -kv[1]):
        print(f"  {bits}  {enc.describe(bits)}  count {n}")

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
        if args.encoding != "mosaic_grid":
            parser.error("--mosaic-results needs --encoding mosaic_grid")
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
