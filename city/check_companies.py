#!/usr/bin/env python3
"""Check every city run against the plaintext answer: the same heavy cells, and in each cell the
same value for every block of beta (rides per company, per hour, per car category, per surge
state and per zone), bucketed as src/dataset.rs does. alpha is the pickup or the drop-off point,
as the run's config says.

    python3 city/check_companies.py            # every city/results/<tab>-<encoding>.json

Cells are computed with the encoders of scripts/check_heavy_hitters.py, which follow src/geo.rs
independently of the Rust code (H3 needs `pip install -r scripts/requirements.txt`).
"""

import csv
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from check_heavy_hitters import make_encoder  # noqa: E402


def encoder_for(encoding):
    if encoding == "mosaic_grid":
        return make_encoder("mosaic_grid")
    (kind, p), = encoding.items()
    if kind == "geohash":
        return make_encoder("geohash", p["prefix"], bits=p["bits"])
    if kind == "h3":
        return make_encoder("h3", p["prefix"], resolution=p["resolution"])
    if kind == "morton":
        bbox = (p["min_lat"], p["max_lat"], p["min_lon"], p["max_lon"])
        return make_encoder("morton", bbox=bbox, axis_bits=p["axis_bits"])
    raise ValueError(f"unknown encoding {encoding}")


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0088 * math.asin(math.sqrt(a))


def bucket(weight, labels, row, zones):
    """The bucket of one block, like `Weight::bucket` in src/dataset.rs."""
    if weight == "constant":
        return 0
    if weight == "company":
        return labels.index(row["company"])
    if weight in ("hour_of_day", "dropoff_hour"):
        return int(row[weight])
    if weight == "car_category":
        raw = row["requested_car_category"]
        name = raw[2:-1] if raw.startswith("b'") and raw.endswith("'") else raw
        return labels.index(name)
    if weight == "surge":
        return int(float(row["surge_factor"]) > 1.0)
    if weight in ("dropoff_zone", "pickup_zone"):
        side = "end" if weight == "dropoff_zone" else "start"
        lat, lon = float(row[f"{side}_location_lat"]), float(row[f"{side}_location_long"])
        for i, z in enumerate(zones):
            if haversine_km(lat, lon, z["lat"], z["lon"]) <= z["radius_km"]:
                return i
        return len(zones)
    raise ValueError(f"unknown weight {weight}")


def check(path):
    run = json.loads(path.read_text())
    blocks = run["blocks"]
    zones = run.get("zones") or []
    encoder = encoder_for(run["encoding"])
    # Runs exported before `alpha` existed are all pickups.
    side = "end" if run.get("alpha") == "dropoff" else "start"

    width = sum(len(b["labels"]) for b in blocks)
    per_cell = defaultdict(lambda: [0] * (width + 1))
    with open(ROOT / run["dataset"], newline="") as f:
        for row in csv.DictReader(f):
            bits = encoder.encode(float(row[f"{side}_location_lat"]), float(row[f"{side}_location_long"]))
            if bits is None:
                continue
            value, offset = per_cell[bits], 0
            for b in blocks:
                value[offset + bucket(b["weight"], b["labels"], row, zones)] += 1
                offset += len(b["labels"])
            value[-1] += 1
    expected = {bits: v for bits, v in per_cell.items() if v[-1] >= run["threshold"]}
    got = {h["bits"]: h["value"] for h in run["heavy_hitters"]}

    problems = [f"missing {b}" for b in expected.keys() - got.keys()]
    problems += [f"extra {b}" for b in got.keys() - expected.keys()]
    problems += [
        f"{b}: mastic {got[b]} != plaintext {expected[b]}"
        for b in expected.keys() & got.keys()
        if got[b] != expected[b]
    ]
    status = "MATCH" if not problems else "MISMATCH"
    print(f"{path.stem:<20} {status}: {len(got)} heavy cells, threshold {run['threshold']}")
    for p in problems[:10]:
        print(f"    {p}")
    return not problems


def main():
    paths = sorted((ROOT / "city" / "results").glob("*.json"))
    if not paths:
        raise SystemExit("no city/results/*.json; run city/run.sh first")
    ok = all([check(p) for p in paths])
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
