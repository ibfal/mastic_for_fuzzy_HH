#!/usr/bin/env python3
"""Plaintext ground truth for ride-sharing heavy hitters.

Encodes pickups exactly as `src/geo.rs` does (Mosaic's grid, geohash, H3 or Morton), prints every
cell whose count reaches the threshold, and optionally checks a Mastic driver log against it. H3
uses Uber's reference library (`pip install -r scripts/requirements.txt`), independent of the Rust
code.

    python3 scripts/check_heavy_hitters.py data/mosaic/sample_busiest_day.csv --threshold 0.005 \
        --mastic-log day.log
    python3 scripts/check_heavy_hitters.py data/mosaic/sample_busiest_day.csv --threshold 0.005 \
        --encoding geohash --prefix 9v6 --bits 20 --mastic-log day-geohash.log
    python3 scripts/check_heavy_hitters.py data/mosaic/sample_busiest_day.csv --threshold 0.005 \
        --encoding h3 --prefix 82489ffffffffff --resolution 9 --mastic-log day-h3.log
    python3 scripts/check_heavy_hitters.py data/mosaic/sample_busiest_day.csv --threshold 0.005 \
        --encoding morton --bbox 29.9005,30.9245,-98.0505,-97.0265 --axis-bits 10 \
        --mastic-log day-morton.log
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


def interleave(lat_idx, lon_idx, axis_bits):
    """Morton code: lon, lat, lon, ... bits, most significant first, as geo.rs `interleave`."""
    return "".join(
        f"{(lon_idx >> i) & 1}{(lat_idx >> i) & 1}" for i in reversed(range(axis_bits))
    )


def grid_to_bits(lat_grid, lon_grid):
    return interleave(lat_grid, lon_grid, AXIS_BITS)


def bits_to_grid(bits):
    # Also the inverse of `interleave` for any axis_bits, as geo.rs `deinterleave`.
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


class H3:
    def __init__(self, prefix, resolution):
        import h3  # Only needed for this encoding.

        self.h3 = h3
        self.prefix = prefix
        self.prefix_res = h3.get_resolution(prefix)
        self.resolution = resolution

    def _resolutions(self):
        return range(self.prefix_res + 1, self.resolution + 1)

    def encode(self, lat, lon):
        cell = self.h3.latlng_to_cell(lat, lon, self.resolution)
        if self.h3.cell_to_parent(cell, self.prefix_res) != self.prefix:
            return None
        index = self.h3.str_to_int(cell)
        return "".join(f"{(index >> 3 * (15 - r)) & 7:03b}" for r in self._resolutions())

    def cell(self, bits):
        index = self.h3.str_to_int(self.prefix)
        index = (index & ~(0xF << 52)) | (self.resolution << 52)
        for r, i in zip(self._resolutions(), range(0, len(bits), 3)):
            index = (index & ~(7 << 3 * (15 - r))) | (int(bits[i:i + 3], 2) << 3 * (15 - r))
        return self.h3.int_to_str(index)

    def describe(self, bits):
        cell = self.cell(bits)
        lat, lon = self.h3.cell_to_latlng(cell)
        return f"{cell}  ({lat:.4f}, {lon:.4f})"


# 1.024 degrees square, edges half a 0.001-degree step off the data: with axis_bits = 10 its cells
# are exactly Mosaic's grid cells.
MORTON_BBOX = (29.9005, 30.9245, -98.0505, -97.0265)


class Morton:
    """Morton code over a bounding box, as geo.rs `Morton`. The quantisation evaluates the same
    expression in the same order, so both produce identical bits even for points on a boundary."""

    def __init__(self, bbox, axis_bits):
        self.min_lat, self.max_lat, self.min_lon, self.max_lon = bbox
        self.axis_bits = axis_bits

    def _axis_cell(self, v, lo, hi):
        if not (lo <= v < hi):
            return None
        cells = float(1 << self.axis_bits)
        return min(math.floor((v - lo) / (hi - lo) * cells), (1 << self.axis_bits) - 1)

    def encode(self, lat, lon):
        lat_idx = self._axis_cell(lat, self.min_lat, self.max_lat)
        lon_idx = self._axis_cell(lon, self.min_lon, self.max_lon)
        if lat_idx is None or lon_idx is None:
            return None
        return interleave(lat_idx, lon_idx, self.axis_bits)

    def describe(self, bits):
        lat_idx, lon_idx = bits_to_grid(bits)
        cells = float(1 << self.axis_bits)

        def center(lo, hi, i):
            return (lo + (hi - lo) * i / cells + lo + (hi - lo) * (i + 1) / cells) / 2

        lat = center(self.min_lat, self.max_lat, lat_idx)
        lon = center(self.min_lon, self.max_lon, lon_idx)
        return f"morton ({lat_idx}, {lon_idx})  ({lat:.4f}, {lon:.4f})"


def make_encoder(encoding, prefix=None, bits=20, resolution=9, bbox=None, axis_bits=10):
    if encoding == "mosaic_grid":
        return MosaicGrid()
    if encoding == "geohash":
        return Geohash(prefix or "9v6", bits)
    if encoding == "morton":
        return Morton(bbox or MORTON_BBOX, axis_bits)
    return H3(prefix or "82489ffffffffff", resolution)


def parse_bbox(text):
    values = tuple(float(v) for v in text.split(","))
    if len(values) != 4:
        raise argparse.ArgumentTypeError("expected min_lat,max_lat,min_lon,max_lon")
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("csv", help="Mosaic sample_busiest_*.csv")
    parser.add_argument("--threshold", type=float, default=0.005)
    parser.add_argument("--encoding", choices=["mosaic_grid", "geohash", "h3", "morton"],
                        default="mosaic_grid")
    parser.add_argument("--prefix", help="cell shared by all rides (default: 9v6 for geohash, "
                        "82489ffffffffff for h3)")
    parser.add_argument("--bits", type=int, default=20, help="geohash bits after the prefix")
    parser.add_argument("--resolution", type=int, default=9, help="H3 resolution")
    parser.add_argument("--bbox", type=parse_bbox,
                        help="Morton box min_lat,max_lat,min_lon,max_lon, copied verbatim from the "
                        "config (default: %s)" % ",".join(map(str, MORTON_BBOX)))
    parser.add_argument("--axis-bits", type=int, default=10, help="Morton bits per axis")
    parser.add_argument("--mastic-log", help="driver output to compare against")
    parser.add_argument("--mosaic-results", help="Mosaic results.json (mosaic_grid only)")
    args = parser.parse_args()

    enc = make_encoder(args.encoding, args.prefix, args.bits, args.resolution, args.bbox,
                       args.axis_bits)
    with open(args.csv, newline="") as f:
        encoded = [
            enc.encode(float(row["start_location_lat"]), float(row["start_location_long"]))
            for row in csv.DictReader(f)
        ]
    cells = [c for c in encoded if c is not None]
    print(f"*** DROPPED RIDES: {len(encoded) - len(cells)} of {len(encoded)} outside the encoded "
          "area ***")
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
