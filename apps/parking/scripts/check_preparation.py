#!/usr/bin/env python3
"""Checks of the P2 preparation outputs (PARKING_PLAN.md §4 step 11). Exit code 1 if any check fails.

    .\\.venv\\Scripts\\python.exe -I apps\\parking\\scripts\\check_preparation.py

1. Step counts against P1 (reports/01-exploration.md).
2. Snapshot reports per (cell, hour) and per (segment, hour) <= primary capacity (2019 markers).
3. Subsets: nesting, 21,581 rows, same rows as the sample, SHA-256 against manifest.json; every file
   against prepare_manifest.json.
4. Every report of every file inside the Morton box, the geohash prefix and the H3 prefix.
5. Mastic heavy cells per encoding at tau (ground truth files): T = max(1, floor(tau N)), N = rows.
6. Hour buckets of each heavy cell sum to its count; occupancy (from the stays) equals the snapshot
   file aggregated per cell and hour.
7. The window files partition the snapshot file.
Checks 2, 3 (manifest part), 4, 6 (occupancy) and 7 also run for data/join2b/ if it exists.
"""

import csv
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # -I drops the script's folder
import parking_common as pc  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))


def read_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def check_join(join):
    out_dir = pc.DATA if join == pc.DEFAULT_JOIN else pc.DATA / f"join{join}"
    tag = f"[{join}] "
    man = load_json(out_dir / "prepare_manifest.json")
    print(f"\n=== join {join} ({pc.relpath(out_dir)}) ===")

    # 1. Step counts (P1 numbers are for join 2c).
    if join == pc.DEFAULT_JOIN:
        bad = [(k, man["steps"].get(k), v) for k, v in pc.P1_EXPECTED.items() if man["steps"].get(k) != v]
        check(tag + "1. step counts match P1", not bad, "; ".join(f"{k}: {g} vs P1 {v}" for k, g, v in bad)
              or ", ".join(f"{k} {v:,}" for k, v in man["steps"].items()))

    # Files against prepare_manifest.json.
    wrong = [p for p, e in man["files"].items() if pc.sha256_file(pc.REPO / p) != e["sha256"]]
    check(tag + "3. SHA-256 of all files match prepare_manifest.json", not wrong, ", ".join(wrong)
          or f"{len(man['files'])} files")

    snap_rows = read_rows(pc.report_path("snapshot", join))

    # 2. Capacity.
    for name in pc.ENCODINGS:
        enc = pc.encoder(name)
        cap = {r["bits"]: int(r["markers_2019"]) for r in read_rows(out_dir / f"capacity_{name}.csv")}
        cache = {}
        per = Counter()
        for r in snap_rows:
            key = (r["start_location_lat"], r["start_location_long"])
            if key not in cache:
                cache[key] = enc.encode(float(key[0]), float(key[1]))
            per[(cache[key], int(r["hour"]))] += 1
        over = [(b, h, n, cap.get(b, 0)) for (b, h), n in per.items() if n > cap.get(b, 0)]
        check(tag + f"2. snapshot per (cell, hour) <= markers_2019, {name}", not over,
              "; ".join(f"cell {b} hour {h}: {n} > {c}" for b, h, n, c in over[:10])
              or f"{len(per):,} cell-hours, max utilisation "
                 f"{max(n / cap[b] for (b, _h), n in per.items()):.2f}")
    dup = Counter((r["street_marker"], r["hour"]) for r in snap_rows)
    dup = {k: n for k, n in dup.items() if n > 1}
    check(tag + "2. at most one snapshot report per (bay, hour)", not dup,
          f"{len(dup)} bay-hours with 2 reports ({sum(n - 1 for n in dup.values())} extra reports) at "
          f"{len({m for m, _h in dup})} bays — overlapping sensor intervals: "
          + ", ".join(sorted({m for m, _h in dup})) if dup else "")
    segcap = {r["segment"]: int(r["markers_2019"]) for r in read_rows(out_dir / "capacity_segment.csv")}
    per = Counter((r["segment"], int(r["hour"])) for r in snap_rows)
    over = [(s, h, n, segcap.get(s, 0)) for (s, h), n in per.items() if n > segcap.get(s, 0)]
    check(tag + "2. snapshot per (segment, hour) <= markers_2019", not over,
          "; ".join(f"segment {s} hour {h}: {n} > {c}" for s, h, n, c in over[:10]) or f"{len(per):,} segment-hours")

    # 4. Every report encodes (distinct locations only; ~300 points).
    files = [pc.REPO / p for p in man["files"]]
    points = set()
    for p in files:
        for r in read_rows(p):
            points.add((float(r["start_location_lat"]), float(r["start_location_long"])))
    for name in pc.ENCODINGS:
        enc = pc.encoder(name)
        out = [pt for pt in points if enc.encode(*pt) is None]
        check(tag + f"4. all reports inside the {name} area", not out,
              f"{len(out)} locations outside" if out else f"{len(points)} distinct locations in {len(files)} files, 0 dropped")

    # 6b. Occupancy from the stays = snapshot file aggregated.
    gt_dir = out_dir / "ground_truth"
    for name in pc.ENCODINGS:
        enc = pc.encoder(name)
        agg = Counter()
        for r in snap_rows:
            agg[(enc.encode(float(r["start_location_lat"]), float(r["start_location_long"])), int(r["hour"]))] += 1
        occ = {(r["bits"], int(r["hour"])): int(r["occupied"]) for r in read_rows(gt_dir / f"occupancy_{name}.csv")}
        diff = [k for k in set(agg) | set(occ) if agg.get(k, 0) != occ.get(k, 0)]
        check(tag + f"6. occupancy (stays) = snapshot file aggregated, {name}", not diff,
              f"{len(diff)} cell-hours differ" if diff else f"{sum(agg.values()):,} car-hours")

    # 7. Windows partition the snapshot file.
    win = []
    for a, b in pc.WINDOWS:
        rows = read_rows(pc.report_path("snapshot", join, (a, b)))
        if any(not a <= int(r["hour"]) < b for r in rows):
            win = None
            break
        win += rows
    key = lambda r: tuple(r.values())  # noqa: E731
    ok = win is not None and Counter(map(key, win)) == Counter(map(key, snap_rows))
    check(tag + "7. window files partition the snapshot file", ok,
          f"{len(snap_rows):,} rows in {len(pc.WINDOWS)} windows" if ok else "mismatch")

    # 5./6a. Ground truth.
    subsets = (*pc.SUBSETS, "app") if join == pc.DEFAULT_JOIN else ("app",)
    for s in subsets:
        gt = load_json(gt_dir / f"mastic_{s}.json")
        src_rows = len(read_rows(pc.REPO / gt["source"]["path"]))
        parts, ok_t, ok_h = [], True, True
        for name, e in gt["encodings"].items():
            ok_t &= e["N"] == src_rows and e["dropped"] == 0 and e["T"] == pc.threshold_abs(e["N"], gt["threshold_frac"])
            ok_t &= all(c["count"] >= e["T"] for c in e["cells"])
            ok_h &= all(sum(c["hours"]) == c["count"] and len(c["hours"]) == pc.HOURS for c in e["cells"])
            parts.append(f"{name} {len(e['cells'])} (T={e['T']})")
        check(tag + f"5. Mastic heavy cells at tau = {gt['threshold_frac']:g}, {s}", ok_t, ", ".join(parts))
        check(tag + f"6. hour buckets sum to the count, {s}", ok_h)


def check_subsets():
    print("\n=== benchmark subsets ===")
    manifest = load_json(pc.DATA / "manifest.json")
    previous = b""
    ok_nest = True
    for f in manifest["files"]:
        path = pc.REPO / f["path"]
        data = path.read_bytes()
        check(f"3. {f['path']} SHA-256 and rows match manifest.json",
              pc.sha256_file(path) == f["sha256"] and data.count(b"\n") - 1 == f["rows"], f"{f['rows']:,} rows")
        body = data.split(b"\n", 1)[1]
        ok_nest &= body.startswith(previous)
        previous = body
    check("3. subsets are nested (each body starts with the smaller one)", ok_nest)
    sample = read_rows(pc.DATA / f"{pc.DATASET}_sample.csv")
    full = read_rows(pc.subset_path("3of3"))
    key = lambda r: tuple(r.values())  # noqa: E731
    check(f"3. sample has {pc.BENCH_N:,} rows and 3of3 holds exactly its rows",
          len(sample) == pc.BENCH_N and Counter(map(key, sample)) == Counter(map(key, full)),
          f"seed {manifest['seed']}, source sha256 {manifest['source']['sha256'][:12]}…")
    snap = Counter(map(key, read_rows(pc.report_path("snapshot"))))
    check("3. every sampled report is a snapshot report", not (Counter(map(key, sample)) - snap))


def main():
    pc.configure_stdout()
    check_join(pc.DEFAULT_JOIN)
    check_subsets()
    if (pc.DATA / "join2b" / "prepare_manifest.json").exists():
        check_join("2b")
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)} of {len(results)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
