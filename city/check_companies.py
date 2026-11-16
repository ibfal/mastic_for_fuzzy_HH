#!/usr/bin/env python3
"""Check every city run against the plaintext answer: the same heavy cells, and in each cell the
same number of rides per company.

    python3 city/check_companies.py            # every city/results/<encoding>.json

Cells are computed with the encoders of scripts/check_heavy_hitters.py, which follow src/geo.rs
independently of the Rust code (H3 needs `pip install -r scripts/requirements.txt`).
"""

import csv
import json
import sys
from collections import Counter, defaultdict
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


def check(path):
    run = json.loads(path.read_text())
    (block,) = run["blocks"]
    companies = block["labels"]
    encoder = encoder_for(run["encoding"])

    per_cell = defaultdict(Counter)
    with open(ROOT / run["dataset"], newline="") as f:
        for row in csv.DictReader(f):
            bits = encoder.encode(float(row["start_location_lat"]), float(row["start_location_long"]))
            if bits is not None:
                per_cell[bits][row["company"]] += 1
    expected = {
        bits: [c[name] for name in companies] + [sum(c.values())]
        for bits, c in per_cell.items()
        if sum(c.values()) >= run["threshold"]
    }
    got = {h["bits"]: h["value"] for h in run["heavy_hitters"]}

    problems = [f"missing {b}" for b in expected.keys() - got.keys()]
    problems += [f"extra {b}" for b in got.keys() - expected.keys()]
    problems += [
        f"{b}: mastic {got[b]} != plaintext {expected[b]}"
        for b in expected.keys() & got.keys()
        if got[b] != expected[b]
    ]
    status = "MATCH" if not problems else "MISMATCH"
    print(f"{path.stem:<12} {status}: {len(got)} heavy cells, threshold {run['threshold']}")
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
