#!/usr/bin/env python3
"""Write a tiny ride dataset with a known answer: 10 Austin landmarks, each repeated a fixed
number of times. With threshold 0.1 (16 of 165 rides) the heavy hitters are exactly the first
four landmarks, with counts 50, 40, 30 and 20.

    python3 scripts/make_synthetic_rides.py data/synthetic/ten_locations.csv
"""

import csv
import os
import sys

from check_heavy_hitters import make_encoder

# (name, lat, lon, rides). Coordinates use 3 decimals like the RideAustin data. Every landmark must
# fall in a different cell, or their counts merge; `check_distinct_cells` below verifies this.
LANDMARKS = [
    ("Airport", 30.197, -97.666, 50),
    ("Texas Capitol", 30.275, -97.740, 40),
    ("UT Tower", 30.286, -97.739, 30),
    ("The Domain", 30.402, -97.725, 20),
    ("Zilker Park", 30.267, -97.773, 10),
    ("South Congress", 30.249, -97.750, 5),
    ("Mueller", 30.299, -97.706, 4),
    ("Circuit of the Americas", 30.135, -97.636, 3),
    ("Round Rock", 30.508, -97.679, 2),
    ("Lady Bird Lake", 30.262, -97.745, 1),
]


# Encodings used by the synthetic-ten-locations-*.toml configs.
ENCODINGS = [
    ("mosaic_grid", {}),
    ("geohash", {"prefix": "9v6", "bits": 20}),
    ("h3", {"prefix": "82489ffffffffff", "resolution": 9}),
]


def check_distinct_cells():
    """Warn if two landmarks share a cell, which would change the expected answer."""
    for encoding, params in ENCODINGS:
        try:
            enc = make_encoder(encoding, **params)
        except ImportError:
            print(f"  {encoding}: skipped (pip install -r scripts/requirements.txt)")
            continue
        cells = {}
        for name, lat, lon, _ in LANDMARKS:
            cells.setdefault(enc.encode(lat, lon), []).append(name)
        shared = [names for cell, names in cells.items() if len(names) > 1 or cell is None]
        status = "OK" if not shared else f"WARNING, shared or outside: {shared}"
        print(f"  {encoding}: {len(LANDMARKS)} landmarks in {len(cells)} cells, {status}")


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "data/synthetic/ten_locations.csv"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["name", "start_location_lat", "start_location_long"])
        for name, lat, lon, rides in LANDMARKS:
            writer.writerows([name, f"{lat:.3f}", f"{lon:.3f}"] for _ in range(rides))

    total = sum(r for *_, r in LANDMARKS)
    threshold = int(0.1 * total)
    print(f"Wrote {total} rides to {path}")
    print(f"Expected heavy hitters at threshold 0.1 ({threshold} rides):")
    for name, lat, lon, rides in LANDMARKS:
        if rides >= threshold:
            print(f"  {name:<15} ({lat:.3f}, {lon:.3f})  count {rides}")
    print("Landmarks in separate cells:")
    check_distinct_cells()


if __name__ == "__main__":
    main()
