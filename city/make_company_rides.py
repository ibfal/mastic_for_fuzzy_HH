#!/usr/bin/env python3
"""Give every RideAustin ride of the busiest day a synthetic company: Uber, Lyft, Waymo or
RideAustin. The result is the input of the city map: each company sends its rides as Mastic
reports, and the city (the collector) learns only the per-company counts of the heavy cells.

    python3 city/make_company_rides.py                # writes city/data/rides_day_companies.csv
    python3 city/make_company_rides.py --uniform      # same split everywhere

The split is uneven on purpose: Uber and Lyft are the big companies, Waymo and RideAustin the
small ones (see BASE_SHARE). Without --uniform each company is also stronger around one area of
Austin (see HOTSPOTS), so different cells end up dominated by different companies. The assignment
is random but seeded, so the file is the same on every run.
"""

import argparse
import csv
import math
import random
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "data" / "mosaic" / "sample_busiest_day.csv"
OUT = ROOT / "city" / "data" / "rides_day_companies.csv"

# Bucket order of the `company` weight: must match `companies` in city/configs/*.toml.
COMPANIES = ["Uber", "Lyft", "Waymo", "RideAustin"]

# Share of rides far from every hotspot, which is also roughly the citywide split.
BASE_SHARE = {"Uber": 0.50, "Lyft": 0.33, "Waymo": 0.05, "RideAustin": 0.12}

# (company, name, lat, lon, radius_km, boost): near the centre the company's weight is multiplied
# by 1 + boost, fading out over about `radius_km` (Gaussian).
HOTSPOTS = [
    ("Waymo", "downtown", 30.2672, -97.7431, 0.8, 12.0),
    ("Lyft", "UT campus", 30.2900, -97.7400, 1.0, 2.5),
    ("RideAustin", "north Austin / Domain", 30.4000, -97.7250, 4.0, 5.0),
    ("Uber", "airport", 30.1975, -97.6664, 2.5, 1.5),
]

# Only these columns are kept: the pickup (alpha), the columns the other weights of beta read
# (hour, car category, surge, drop-off) and the ride id and time for inspection.
KEEP = [
    "RIDE_ID", "started_on", "start_location_lat", "start_location_long", "hour_of_day",
    "requested_car_category", "surge_factor", "end_location_lat", "end_location_long",
]


def km(lat1, lon1, lat2, lon2):
    """Equirectangular distance in km, fine at city scale."""
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    y = math.radians(lat2 - lat1)
    return 6371.0 * math.hypot(x, y)


def weights(lat, lon, uniform):
    w = dict(BASE_SHARE)
    if not uniform:
        for company, _, hlat, hlon, radius, boost in HOTSPOTS:
            d = km(lat, lon, hlat, hlon)
            w[company] *= 1.0 + boost * math.exp(-((d / radius) ** 2))
    return [w[c] for c in COMPANIES]


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--uniform", action="store_true", help="same split everywhere")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    totals = Counter()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.source, newline="") as src, open(args.out, "w", newline="") as dst:
        writer = csv.DictWriter(dst, fieldnames=KEEP + ["company"])
        writer.writeheader()
        for row in csv.DictReader(src):
            lat, lon = float(row["start_location_lat"]), float(row["start_location_long"])
            company = rng.choices(COMPANIES, weights(lat, lon, args.uniform))[0]
            totals[company] += 1
            writer.writerow({**{k: row[k] for k in KEEP}, "company": company})

    n = sum(totals.values())
    print(f"wrote {n} rides to {args.out.relative_to(ROOT)}")
    for c in COMPANIES:
        print(f"  {c:<11} {totals[c]:>7}  {100 * totals[c] / n:5.1f}%")


if __name__ == "__main__":
    main()
