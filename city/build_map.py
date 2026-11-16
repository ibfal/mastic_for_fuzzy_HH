#!/usr/bin/env python3
"""Build the single-file city map from the saved runs.

    python3 city/build_map.py

Reads every city/results/<tab>-<encoding>.json (written by city/run.sh; tab is pickup or dropoff)
and writes city/dist/city-map.html: city/map.html with, per tab and encoding, only the final heavy
cells inlined,
each with its outline and its revealed beta: the per-company counts and the totals of the other
blocks. The rest of the crawl is
left out: the map shows only the weighted result.
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CITY = ROOT / "city"
INLINE_TAG = re.compile(r'(<script id="inline-runs" type="application/json">)(.*?)(</script>)', re.S)
ORDER = ["mosaic-grid", "geohash", "h3", "morton"]


def summarize(run):
    """The final heavy cells of one run: per cell the company counts, and the totals of every
    other block of beta (hour, car category, surge, drop-off zone) in `stats`."""
    blocks, offset = [], 0
    for b in run["blocks"]:
        blocks.append({"weight": b["weight"], "labels": b["labels"], "start": offset})
        offset += len(b["labels"])
    company = next(b for b in blocks if b["weight"] == "company")
    others = [b for b in blocks if b["weight"] != "company"]
    # Outlines are only on the crawl levels; the final level holds the heavy hitters.
    regions = {c["bits"]: c["region"] for c in run["levels"][-1]["candidates"]}

    def part(value, b):
        return value[b["start"]: b["start"] + len(b["labels"])]

    cells = []
    for h in run["heavy_hitters"]:
        value = h["value"]
        cells.append({
            "bits": h["bits"],
            "label": h["label"],
            "center": h["center"],
            "counts": part(value, company),
            "stats": [part(value, b) for b in others],
            "total": value[-1],
            "region": regions.get(h["bits"], []),
        })
    return {
        "encoding": run["encoding"],
        "num_rides": run["rides"]["total"] if run.get("rides") else run["num_clients"],
        "companies": company["labels"],
        "blocks": [{"weight": b["weight"], "labels": b["labels"]} for b in others],
        "num_clients": run["num_clients"],
        "threshold": run["threshold"],
        "threshold_fraction": run["threshold_fraction"],
        "total_seconds": run["total_seconds"],
        "cells": cells,
    }


def main():
    runs = {"pickup": {}, "dropoff": {}}
    for path in (CITY / "results").glob("*.json"):
        tab, _, encoding = path.stem.partition("-")
        if tab not in runs:
            print(f"skipping {path.name}: not <tab>-<encoding>.json")
            continue
        runs[tab][encoding] = summarize(json.loads(path.read_text()))
    for tab in runs:
        order = lambda n: (ORDER.index(n) if n in ORDER else len(ORDER), n)
        runs[tab] = {n: runs[tab][n] for n in sorted(runs[tab], key=order)}

    page = (CITY / "map.html").read_text()
    # "</" inside a script element would end it early.
    data = json.dumps(runs, separators=(",", ":")).replace("</", "<\\/")
    page, n = INLINE_TAG.subn(lambda m: m.group(1) + data + m.group(3), page)
    if n != 1:
        raise SystemExit('city/map.html has no <script id="inline-runs"> element')
    out = CITY / "dist" / "city-map.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page)
    for tab, by_encoding in runs.items():
        for name, run in by_encoding.items():
            print(f"{tab:<8} {name:<12} {len(run['cells'])} heavy cells")
    print(f"wrote {out.relative_to(ROOT)} ({out.stat().st_size / 1e3:.0f} KB)")


if __name__ == "__main__":
    main()
