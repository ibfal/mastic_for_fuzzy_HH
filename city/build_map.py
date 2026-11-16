#!/usr/bin/env python3
"""Build the single-file city map from the saved runs.

    python3 city/build_map.py

Reads every city/results/<encoding>.json (written by city/run.sh) and writes
city/dist/city-map.html: city/map.html with, per encoding, only the final heavy cells inlined,
each with its per-company counts (the revealed beta) and its outline. The rest of the crawl is
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
    (block,) = run["blocks"]
    companies = block["labels"]
    # Outlines are only on the crawl levels; the final level holds the heavy hitters.
    regions = {c["bits"]: c["region"] for c in run["levels"][-1]["candidates"]}
    cells = []
    for h in run["heavy_hitters"]:
        *counts, total = h["value"]
        cells.append({
            "bits": h["bits"],
            "label": h["label"],
            "center": h["center"],
            "counts": counts[: len(companies)],
            "total": total,
            "region": regions.get(h["bits"], []),
        })
    return {
        "encoding": run["encoding"],
        "companies": companies,
        "num_clients": run["num_clients"],
        "threshold": run["threshold"],
        "threshold_fraction": run["threshold_fraction"],
        "total_seconds": run["total_seconds"],
        "cells": cells,
    }


def main():
    paths = {p.stem: p for p in (CITY / "results").glob("*.json")}
    names = sorted(paths, key=lambda n: (ORDER.index(n) if n in ORDER else len(ORDER), n))
    runs = {n: summarize(json.loads(paths[n].read_text())) for n in names}

    page = (CITY / "map.html").read_text()
    # "</" inside a script element would end it early.
    data = json.dumps(runs, separators=(",", ":")).replace("</", "<\\/")
    page, n = INLINE_TAG.subn(lambda m: m.group(1) + data + m.group(3), page)
    if n != 1:
        raise SystemExit('city/map.html has no <script id="inline-runs"> element')
    out = CITY / "dist" / "city-map.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page)
    for name, run in runs.items():
        print(f"{name:<12} {len(run['cells'])} heavy cells")
    print(f"wrote {out.relative_to(ROOT)} ({out.stat().st_size / 1e3:.0f} KB)")


if __name__ == "__main__":
    main()
