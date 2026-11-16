#!/usr/bin/env python3
"""Plaintext ground truth for the parking application (PARKING_PLAN.md §4 step 10).

(a) Mastic: heavy cells per encoding at tau = TAU_MASTIC for the benchmark subsets and the
    application set (snapshot reports), each cell with its 24 hour-bucket counts. Same structure as
    bench/ground_truth/*.json plus `hours`, `markers_2019` and `bays_2025`.
    T = max(1, floor(tau * N)), N = reports encoded; a cell is heavy if its count >= T.
(b) True occupancy per cell and hour (snapshot instant h:30), straight from the stays in DuckDB (not
    from the report files): `occupied` = bays with a clean located stay covering h:30,
    `occupied_all` = bays with any located stay covering it (including stays the cleaning rule
    drops), with the capacity columns. A bay counts once per hour even where overlapping sensor
    intervals give it two stays (D1).

Mosaic's ground truth (qualifying squares per window) is computed in the Mosaic repo.

    .\\.venv\\Scripts\\python.exe -I apps\\parking\\scripts\\ground_truth.py [--join 2c|2b] [--tau 0.01]

Writes data/ground_truth/mastic_<subset>.json (subset = 1of3, 2of3, 3of3, app) and
data/ground_truth/occupancy_<encoding>.csv; with --join 2b only the app set, under data/join2b/.
"""

import argparse
import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # -I drops the script's folder
import parking_common as pc  # noqa: E402

SCHEMA_VERSION = 1


def load_capacity(out_dir, name):
    with open(out_dir / f"capacity_{name}.csv", newline="", encoding="utf-8") as f:
        return {r["bits"]: (int(r["markers_2019"]), int(r["bays_2025"])) for r in csv.DictReader(f)}


def heavy_cells(name, reports, tau, capacity):
    spec = pc.ENCODINGS[name]
    enc = pc.encoder(name)
    counts, hours = Counter(), defaultdict(lambda: [0] * pc.HOURS)
    dropped = 0
    for lat, lon, hour in reports:
        bits = enc.encode(lat, lon)
        if bits is None:
            dropped += 1
            continue
        if len(bits) != spec["data_bits"]:
            raise SystemExit(f"{name}: encoder produced {len(bits)} bits, expected {spec['data_bits']}")
        counts[bits] += 1
        hours[bits][hour] += 1
    n = sum(counts.values())
    t = pc.threshold_abs(n, tau)
    cells = []
    for bits, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        if count < t:
            break
        lat, lon = enc.center(bits)
        m, b = capacity.get(bits, (0, 0))
        cells.append({"bits": bits, "label": enc.label(bits), "lat": lat, "lon": lon, "count": count,
                      "hours": hours[bits], "markers_2019": m, "bays_2025": b})
    return {"data_bits": spec["data_bits"], "params": spec["params"], "rows": len(reports),
            "dropped": dropped, "N": n, "T": t, "distinct_cells": len(counts), "cells": cells}


def mastic_ground_truth(subset, path, tau, out_dir):
    reports = pc.read_reports(path)
    return {
        "schema_version": SCHEMA_VERSION,
        "system": "mastic",
        "dataset": pc.DATASET,
        "subset": subset,
        "day": pc.DAY.isoformat(),
        "design": "B snapshot (hour h if parked at h:30)",
        "source": {"path": pc.relpath(path), "sha256": pc.sha256_file(path)},
        "threshold_frac": tau,
        "hist_buckets": pc.HOURS,
        "encodings": {name: heavy_cells(name, reports, tau, load_capacity(out_dir, name)) for name in pc.ENCODINGS},
    }


def occupancy(join, out_dir):
    """Cars present at h:30 per (location, hour), from the stays; aggregated per encoding cell."""
    import explore
    import prepare

    con = explore.connect()
    explore.build_cache(con)
    explore.register_views(con)
    ctx = explore.Ctx(con)
    prepare.build_keymap(con, ctx, join)
    D = pc.DAY.isoformat()
    rows = con.execute(f"""
        SELECT km.lat, km.lon, h.hour, count(DISTINCT v.StreetMarker) FILTER (WHERE v.clean),
               count(DISTINCT v.StreetMarker)
        FROM vp v JOIN keymap km USING (key), (SELECT range AS hour FROM range({pc.HOURS})) h
        WHERE v.ArrivalTime <= TIMESTAMP '{D}' + to_hours(h.hour) + to_minutes({pc.SNAPSHOT_MINUTE})
          AND v.DepartureTime > TIMESTAMP '{D}' + to_hours(h.hour) + to_minutes({pc.SNAPSHOT_MINUTE})
        GROUP BY ALL""").fetchall()
    # Stays that arrived before 2019 are not in vp; none can cover 2019-10-11.
    result = {}
    for name in pc.ENCODINGS:
        enc = pc.encoder(name)
        cap = load_capacity(out_dir, name)
        occ, occ_all = Counter(), Counter()
        for lat, lon, hour, n_clean, n_all in rows:
            bits = enc.encode(lat, lon)
            if bits is None:
                continue
            occ[(bits, hour)] += n_clean
            occ_all[(bits, hour)] += n_all
        cells = sorted({b for b, (m, _bays) in cap.items() if m} | {b for b, _h in occ_all})
        out = []
        for bits in cells:
            m, b = cap.get(bits, (0, 0))
            for hour in range(pc.HOURS):
                o = occ[(bits, hour)]
                out.append((bits, enc.label(bits), hour, o, occ_all[(bits, hour)], m, b, m - o))
        result[name] = out
    return result


def main():
    pc.configure_stdout()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--join", choices=sorted(pc.JOINS), default=pc.DEFAULT_JOIN)
    ap.add_argument("--tau", type=float, default=pc.TAU_MASTIC)
    args = ap.parse_args()
    out_dir = pc.DATA if args.join == pc.DEFAULT_JOIN else pc.DATA / f"join{args.join}"
    gt_dir = out_dir / "ground_truth"
    tag = "" if args.tau == pc.TAU_MASTIC else f"_tau{args.tau:g}"

    sets = {"app": pc.report_path("snapshot", args.join)}
    if args.join == pc.DEFAULT_JOIN:
        sets = {**{s: pc.subset_path(s) for s in pc.SUBSETS}, **sets}
    print(f"(a) Mastic heavy cells at tau = {args.tau:g} (join {args.join})")
    print(f"{'set':6}" + "".join(f"{n:>26}" for n in pc.ENCODINGS))
    for subset, path in sets.items():
        gt = mastic_ground_truth(subset, path, args.tau, out_dir)
        pc.write_json(gt_dir / f"mastic_{subset}{tag}.json", gt)
        print(f"{subset:6}" + "".join(
            f"{len(e['cells']):>6} cells (T={e['T']:>4}, drop {e['dropped']})" for e in gt["encodings"].values()))

    print("(b) True occupancy per cell and hour (snapshot), from the stays")
    for name, rows in occupancy(args.join, out_dir).items():
        path = gt_dir / f"occupancy_{name}.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(["bits", "label", "hour", "occupied", "occupied_all", "markers_2019", "bays_2025",
                        "free_markers"])
            w.writerows(rows)
        print(f"  {pc.relpath(path)}: {len(rows):,} rows, {sum(r[3] for r in rows):,} car-hours")


if __name__ == "__main__":
    main()
