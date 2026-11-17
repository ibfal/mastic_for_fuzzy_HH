#!/usr/bin/env python3
"""Street names for the cells with hourly counts (frontend "Where to park", report 06).

For every cell Mastic gave hourly counts for (heavy cells and the last level's other candidates in
apps/parking/results/mastic_<encoding>.json), take the 2025 bays inside the cell (at their own
coordinates, as capacity.py counts bays_2025), group them by road segment, read each segment's main
street from its description and keep the two streets with the most bays (ties by name):

    "Lonsdale Street between Elizabeth Street and Queen Street" -> Lonsdale Street
    "Intersection of X and Y" -> X;  "X from ..." -> X

Reads the git-ignored raw bay file apps/parking/raw/on-street-parking-bays.csv after checking its
SHA-256 against apps/parking/raw_manifest.json, and writes the small committed lookup
data/cell_streets_<encoding>.csv (bits, label, streets, bays_2025, segments), so build_frontend.py
works without raw/. Deterministic.

    .\\.venv\\Scripts\\python.exe -I apps\\parking\\scripts\\cell_streets.py
"""

import csv
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))  # -I drops the script's folder
import hash_raw  # noqa: E402
import parking_common as pc  # noqa: E402

BAYS_FILE = "on-street-parking-bays.csv"
TOP_STREETS = 2
SEP = "; "  # between streets in the CSV's `streets` column


def out_path(name):
    return pc.DATA / f"cell_streets_{name}.csv"


def main_street(description):
    d = description.strip()
    m = re.match(r"(?i)intersection of (.+?) and ", d)
    if m:
        return m.group(1).strip()
    for sep in (" between ", " from "):
        if sep in d:
            return d.split(sep, 1)[0].strip()
    return d


def load_bays():
    path = hash_raw.RAW / BAYS_FILE
    manifest = json.loads(hash_raw.MANIFEST.read_text(encoding="utf-8"))
    want = manifest["files"][BAYS_FILE]["sha256"]
    got = hash_raw.sha256_file(path)
    if got != want:
        raise SystemExit(f"{path}: SHA-256 {got} != raw_manifest.json {want}")
    with open(path, newline="", encoding="utf-8") as f:
        return [(r["roadsegmentid"], r["roadsegmentdescription"], float(r["latitude"]), float(r["longitude"]))
                for r in csv.DictReader(f)]


def build(name, bays):
    enc = pc.encoder(name)
    with open(pc.APP / "results" / f"mastic_{name}.json", encoding="utf-8") as f:
        export = json.load(f)
    hourly = {h["bits"] for h in export["heavy_hitters"]} | {c["bits"] for c in export["levels"][-1]["candidates"]}
    per_cell = defaultdict(Counter)  # bits -> Counter(road segment)
    desc = {}
    for seg, d, lat, lon in bays:
        bits = enc.encode(lat, lon)
        if bits in hourly:
            per_cell[bits][seg] += 1
            desc.setdefault(seg, d)
    rows = []
    for bits in sorted(hourly):
        segs = per_cell.get(bits, Counter())
        streets = Counter()
        for seg, n in segs.items():
            streets[main_street(desc[seg])] += n
        top = [s for s, _ in sorted(streets.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_STREETS]]
        rows.append((bits, enc.label(bits), SEP.join(top), sum(segs.values()), ";".join(sorted(segs))))
    return rows


def main():
    pc.configure_stdout()
    bays = load_bays()
    print(f"{BAYS_FILE}: {len(bays):,} bays, SHA-256 matches raw_manifest.json")
    for name in pc.ENCODINGS:
        rows = build(name, bays)
        path = out_path(name)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(["bits", "label", "streets", "bays_2025", "segments"])
            w.writerows(rows)
        named = sum(1 for r in rows if r[2])
        print(f"  {pc.relpath(path)}: {len(rows)} cells with hourly counts, {named} with street names, "
              f"{len(rows) - named} without 2025 bays")


if __name__ == "__main__":
    main()
