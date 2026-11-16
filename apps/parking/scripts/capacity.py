#!/usr/bin/env python3
"""Capacity per street segment and per cell of each encoding (PARKING_PLAN.md §4 step 9, D15).

- markers_2019 (primary): distinct 2019 StreetMarkers with any sensor row overlapping the chosen day
  (any status), placed at their segment location by the same join as the reports. These are the
  bays that could produce reports, so a cell's snapshot count per hour cannot exceed it.
- bays_2025 (secondary): bays of the 2025-26 On-street Parking Bays file, at their own coordinates.

    .\\.venv\\Scripts\\python.exe -I apps\\parking\\scripts\\capacity.py [--join 2c|2b]

Writes data/capacity_segment.csv, data/capacity_<encoding>.csv and data/capacity_summary.json
(data/join2b/ for --join 2b).
"""

import argparse
import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # -I drops the script's folder
import parking_common as pc  # noqa: E402
import explore  # noqa: E402
import prepare  # noqa: E402


def segment_locations(ctx, join):
    """(StreetName, BetweenStreet1, BetweenStreet2) -> (lat, lon, segment) under the join. The join
    only looks at these three columns, so every sensor of a street segment gets the same location."""
    J = ctx.memo("joins", explore.joins)
    m = J["methods"][pc.JOINS[join]]["map"]
    out = {}
    for key, _dev, _mk, _bay, st, b1, b2, _n, _nc in J["keys"]:
        if key in m:
            lat, lon, seg = m[key]
            out[(st, b1, b2)] = (round(lat, 7), round(lon, 7), seg.removeprefix("seg:"))
    return out


def write_rows(path, header, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)
    print(f"  {pc.relpath(path)}: {len(rows):,} rows")


def main():
    pc.configure_stdout()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--join", choices=sorted(pc.JOINS), default=pc.DEFAULT_JOIN)
    args = ap.parse_args()
    out_dir = pc.DATA if args.join == pc.DEFAULT_JOIN else pc.DATA / f"join{args.join}"

    con = explore.connect()
    explore.build_cache(con)
    explore.register_views(con)
    ctx = explore.Ctx(con)
    segloc = segment_locations(ctx, args.join)

    D = pc.DAY.isoformat()
    active = con.execute(f"""SELECT DISTINCT StreetMarker, StreetName, BetweenStreet1, BetweenStreet2 FROM s
        WHERE ArrivalTime < TIMESTAMP '{D}' + INTERVAL 1 DAY AND DepartureTime > TIMESTAMP '{D}'""").fetchall()
    markers = {}  # StreetMarker -> (lat, lon, segment)
    unlocated = []
    for mk, st, b1, b2 in active:
        loc = segloc.get((st, b1, b2))
        if loc:
            markers[mk] = loc
        else:
            unlocated.append(mk)
    bays = con.execute("SELECT roadsegmentid, latitude, longitude FROM bays").fetchall()

    # Per segment.
    seg_markers = Counter(seg for _lat, _lon, seg in markers.values())
    seg_loc = {seg: (lat, lon) for lat, lon, seg in markers.values()}
    bays_by_roadseg = Counter(str(r) for r, _la, _lo in bays)
    seg_rows = []
    for seg in sorted(seg_markers):
        bays_2025 = sum(bays_by_roadseg[r] for r in seg.split("+"))
        seg_rows.append((seg, *seg_loc[seg], seg_markers[seg], bays_2025))
    print(f"Capacity on {D} (join {args.join}):")
    write_rows(out_dir / "capacity_segment.csv",
               ["segment", "start_location_lat", "start_location_long", "markers_2019", "bays_2025"], seg_rows)

    summary = {
        "day": D, "join": pc.JOINS[args.join],
        "markers_active": len(active), "markers_located": len(markers), "markers_unlocated": len(unlocated),
        "bays_2025": len(bays), "segments": len(seg_rows), "encodings": {},
    }
    for name in pc.ENCODINGS:
        enc = pc.encoder(name)
        cap_m, cap_b, segs = Counter(), Counter(), defaultdict(set)
        dropped_m = dropped_b = 0
        for lat, lon, seg in markers.values():
            bits = enc.encode(lat, lon)
            if bits is None:
                dropped_m += 1
                continue
            cap_m[bits] += 1
            segs[bits].add(seg)
        for _r, lat, lon in bays:
            bits = enc.encode(lat, lon)
            if bits is None:
                dropped_b += 1
            else:
                cap_b[bits] += 1
        rows = []
        for bits in sorted(set(cap_m) | set(cap_b)):
            clat, clon = enc.center(bits)
            rows.append((bits, enc.label(bits), round(clat, 7), round(clon, 7), cap_m[bits], cap_b[bits],
                         ";".join(sorted(segs[bits]))))
        write_rows(out_dir / f"capacity_{name}.csv",
                   ["bits", "label", "center_lat", "center_lon", "markers_2019", "bays_2025", "segments"], rows)
        summary["encodings"][name] = {
            "cells": len(rows), "cells_with_markers": len(cap_m), "markers_in_area": sum(cap_m.values()),
            "markers_dropped": dropped_m, "bays_in_area": sum(cap_b.values()), "bays_dropped": dropped_b,
        }
    pc.write_json(out_dir / "capacity_summary.json", summary)
    print(f"Active markers {len(active):,}: located {len(markers):,}, unlocated {len(unlocated):,}")
    for name, e in summary["encodings"].items():
        print(f"  {name:8} cells {e['cells']:>5,} (with 2019 markers {e['cells_with_markers']:,}); markers dropped "
              f"{e['markers_dropped']}, bays in area {e['bays_in_area']:,}, bays dropped {e['bays_dropped']:,}")


if __name__ == "__main__":
    main()
