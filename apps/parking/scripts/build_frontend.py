#!/usr/bin/env python3
"""Data file for the parking frontend (PARKING_PLAN.md §8, P6): apps/parking/frontend/data.js.

Reads only saved files, never starts a run:
  apps/parking/results/mastic_<encoding>.json        Mastic's export (run_app.py, P3)
  apps/parking/data/capacity_<encoding>.csv          2019 sensor bays per cell (markers_2019)
  apps/parking/data/ground_truth/occupancy_<enc>.csv true occupancy, for the comparison layer only

and writes one JS file that assigns `window.PARKING_DATA`, so index.html works from file:// without a
web server. Per encoding:
  cells   every heavy cell Mastic output: outline (the export's region), centre, label, 24 hour counts
          (cars present at h:30), capacity, free spaces and % full per hour;
  params  tau, T, N, day, data_bits, encoding parameters;
  privacy the region totals revealed during the crawl (all levels' candidates, kept and pruned)
          against the heavy cells output;
  crawl   every candidate of every level with its region (the "What the collector saw" layer);
  truth   true occupancy of every sensor cell (comparison layer; not available in a deployment).

    .\\.venv\\Scripts\\python.exe -I apps\\parking\\scripts\\build_frontend.py            # write data.js
    .\\.venv\\Scripts\\python.exe -I apps\\parking\\scripts\\build_frontend.py --check    # verify, exit 1 on mismatch
    .\\.venv\\Scripts\\python.exe -I apps\\parking\\scripts\\build_frontend.py --out x.js # write elsewhere

The output is deterministic: fixed key order, coordinates rounded to 7 decimals, no timestamps.
"""

import argparse
import csv
import json
import math
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))  # -I drops the script's folder
import parking_common as pc  # noqa: E402

SCHEMA_VERSION = 1
RESULTS = pc.APP / "results"
FRONTEND = pc.APP / "frontend"
OUT = FRONTEND / "data.js"
PREFIX = "window.PARKING_DATA = "
SUFFIX = ";\n"
COORD_DIGITS = 7
NAMES = {"morton": "Morton", "geohash": "geohash", "h3": "H3"}
OUTLINE_TOL = 1e-9


# --- inputs ------------------------------------------------------------------------------------

def results_path(name):
    return RESULTS / f"mastic_{name}.json"


def capacity_path(name):
    return pc.DATA / f"capacity_{name}.csv"


def occupancy_path(name):
    return pc.GT / f"occupancy_{name}.csv"


def load_export(name):
    with open(results_path(name), encoding="utf-8") as f:
        export = json.load(f)
    if export.get("format") != "mastic-crawl/1":
        raise SystemExit(f"{results_path(name)}: unexpected format {export.get('format')!r}")
    if export["params"]["encoding"] != name:
        raise SystemExit(f"{results_path(name)}: encoding {export['params']['encoding']!r}, expected {name!r}")
    return export


def load_capacity(name):
    with open(capacity_path(name), newline="", encoding="utf-8") as f:
        return {r["bits"]: r for r in csv.DictReader(f)}


def load_occupancy(name):
    """{bits: {"label", "markers_2019", "occupied"[24], "free_markers"[24]}} in file order."""
    cells = {}
    with open(occupancy_path(name), newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            c = cells.setdefault(r["bits"], {"label": r["label"], "markers_2019": int(r["markers_2019"]),
                                             "occupied": [None] * pc.HOURS, "free_markers": [None] * pc.HOURS})
            h = int(r["hour"])
            c["occupied"][h] = int(r["occupied"])
            c["free_markers"][h] = int(r["free_markers"])
    for bits, c in cells.items():
        if None in c["occupied"]:
            raise SystemExit(f"{occupancy_path(name)}: cell {bits} lacks some hours")
    return cells


# --- geometry ----------------------------------------------------------------------------------

def rnd(v):
    return round(v, COORD_DIGITS)


def ring(points, exact=False):
    return [[float(lat), float(lon)] if exact else [rnd(lat), rnd(lon)] for lat, lon in points]


def outline(region):
    """The export's region (a list of rings of [lat, lon]) rounded; None if empty (H3 non-cells)."""
    return [ring(r) for r in region] if region else None


def computed_outline(name, bits, exact=False):
    """A cell's outline from the plaintext encoders (for the truth layer's below-threshold cells);
    `exact` skips the rounding (for comparing with the export's regions)."""
    enc = pc.encoder(name)
    if name == "morton":
        p = pc.ENCODINGS[name]["params"]
        lat_i, lon_i = int(bits[1::2], 2), int(bits[0::2], 2)  # check_heavy_hitters.bits_to_grid
        n = float(1 << p["axis_bits"])

        def edge(lo, hi, i):
            return lo + (hi - lo) * i / n

        la0, la1 = edge(p["min_lat"], p["max_lat"], lat_i), edge(p["min_lat"], p["max_lat"], lat_i + 1)
        lo0, lo1 = edge(p["min_lon"], p["max_lon"], lon_i), edge(p["min_lon"], p["max_lon"], lon_i + 1)
        return [ring([(la0, lo0), (la0, lo1), (la1, lo1), (la1, lo0)], exact)]
    if name == "geohash":
        (la0, la1), (lo0, lo1) = pc.chh.geohash_box(enc.prefix + bits)
        return [ring([(la0, lo0), (la0, lo1), (la1, lo1), (la1, lo0)], exact)]
    if name == "h3":
        return [ring(enc.h3.cell_to_boundary(enc.cell(bits)), exact)]
    raise ValueError(name)


def same_outline(a, b, tol=OUTLINE_TOL):
    """Equal as point sets per ring (start vertex and direction may differ)."""
    if a is None or b is None or len(a) != len(b):
        return False
    for ra, rb in zip(a, b):
        if len(ra) != len(rb):
            return False
        sa, sb = sorted(map(tuple, ra)), sorted(map(tuple, rb))
        if any(abs(p - q) > tol for pa, pb in zip(sa, sb) for p, q in zip(pa, pb)):
            return False
    return True


def centroid_of(outl):
    pts = [p for r in outl for p in r]
    return [rnd(sum(p[0] for p in pts) / len(pts)), rnd(sum(p[1] for p in pts) / len(pts))]


# --- build -------------------------------------------------------------------------------------

def pct(cars, cap):
    return None if not cap else round(100.0 * cars / cap, 1)


def build_encoding(name):
    export = load_export(name)
    cap = load_capacity(name)
    occ = load_occupancy(name)
    params = export["params"]
    last = {c["bits"]: c for c in export["levels"][-1]["candidates"]}

    cells = []
    for hh in sorted(export["heavy_hitters"], key=lambda h: h["bits"]):
        bits = hh["bits"]
        region = last.get(bits, {}).get("region")
        if not region:
            raise SystemExit(f"{name}: heavy cell {bits} has no region in the export's last level")
        capacity = int(cap[bits]["markers_2019"]) if bits in cap else 0
        capacity = capacity or None
        cars = list(hh["buckets"])
        cells.append({
            "bits": bits,
            "label": hh["label"],
            "center": [rnd(hh["center"][0]), rnd(hh["center"][1])],
            "count": hh["count"],
            "capacity": capacity,
            "bays_2025": int(cap[bits]["bays_2025"]) if bits in cap else None,
            "cars": cars,
            "free": [capacity - c for c in cars] if capacity else None,
            "pct": [pct(c, capacity) for c in cars],
            "outline": outline(region),
        })

    levels, crawl = [], []
    for lvl in export["levels"]:
        cands = [{"bits": c["bits"], "count": c["count"], "heavy": bool(c["heavy"]),
                  "outline": outline(c["region"])} for c in lvl["candidates"]]
        kept = sum(c["heavy"] for c in cands)
        no_region = sum(c["outline"] is None for c in cands)
        levels.append({"level": lvl["level"], "candidates": len(cands), "kept": kept,
                       "pruned": len(cands) - kept, "no_region": no_region})
        crawl.append({"level": lvl["level"], "candidates": cands})
    revealed = sum(lv["candidates"] for lv in levels)
    kept = sum(lv["kept"] for lv in levels)

    heavy_bits = {c["bits"] for c in cells}
    truth = []
    for bits in sorted(occ):
        o = occ[bits]
        capacity = o["markers_2019"] or None
        outl = computed_outline(name, bits)
        truth.append({"bits": bits, "label": o["label"], "heavy": bits in heavy_bits, "capacity": capacity,
                      "occupied": o["occupied"], "center": centroid_of(outl), "outline": outl})

    return {
        "name": NAMES[name],
        "params": {
            "tau": params["tau"], "T": params["T"], "N": params["N"], "day": pc.DAY.isoformat(),
            "data_bits": params["data_bits"], "hist_buckets": params["hist_buckets"],
            "encoding_params": params["encoding_params"],
            "dataset": Path(export["dataset"]).name,
        },
        "privacy": {
            "N": params["N"], "T": params["T"], "heavy_cells": len(cells),
            "revealed": revealed, "kept": kept, "pruned": revealed - kept,
            "no_region": sum(lv["no_region"] for lv in levels),
            "hour_counts_per_region": params["hist_buckets"],
            "levels": levels,
        },
        "cells": cells,
        "crawl": crawl,
        "truth": truth,
    }


def build():
    p = pc.ENCODINGS["morton"]["params"]
    area = ring([(p["min_lat"], p["min_lon"]), (p["min_lat"], p["max_lon"]),
                 (p["max_lat"], p["max_lon"]), (p["max_lat"], p["min_lon"])])
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_by": "apps/parking/scripts/build_frontend.py",
        "day": pc.DAY.isoformat(),
        "snapshot_minute": pc.SNAPSHOT_MINUTE,
        "tau": pc.TAU_MASTIC,
        "study_area": area,
        "sources": {name: {"results": pc.relpath(results_path(name)), "capacity": pc.relpath(capacity_path(name)),
                           "occupancy": pc.relpath(occupancy_path(name))} for name in pc.ENCODINGS},
        "encodings": {name: build_encoding(name) for name in pc.ENCODINGS},
    }


# --- serialisation: readable diffs, one record per line ----------------------------------------

def _leaf(o):
    """A record printed on one line: a dict holding no list of dicts."""
    return isinstance(o, dict) and not any(
        isinstance(v, list) and v and isinstance(v[0], dict) or isinstance(v, dict) and not _leaf(v)
        for v in o.values())


def dump(o, ind=0):
    pad, pad1 = " " * ind, " " * (ind + 1)
    compact = lambda v: json.dumps(v, separators=(",", ":"), ensure_ascii=True, allow_nan=False)  # noqa: E731
    if isinstance(o, dict) and not (ind and _leaf(o)):
        items = [f"{pad1}{json.dumps(k)}:{dump(v, ind + 1)}" for k, v in o.items()]
        return "{\n" + ",\n".join(items) + "\n" + pad + "}"
    if isinstance(o, list) and o and isinstance(o[0], dict):
        return "[\n" + ",\n".join(pad1 + dump(v, ind + 1) for v in o) + "\n" + pad + "]"
    return compact(o)


def render(data):
    return PREFIX + dump(data) + SUFFIX


def parse_data_js(text):
    if not text.startswith(PREFIX) or not text.endswith(SUFFIX):
        raise ValueError("data.js does not have the form `window.PARKING_DATA = {...};`")
    return json.loads(text[len(PREFIX):-len(SUFFIX)])


# --- checks (independent of build(): re-read the sources) ---------------------------------------

def check(path):
    problems = []
    fail = problems.append
    text = path.read_text(encoding="utf-8") if path.exists() else None
    fresh = render(build())
    print(f"(a) {pc.relpath(path)} equals a fresh build: {'yes' if text == fresh else 'NO'}")
    if text != fresh:
        fail("data.js differs from a fresh build (run build_frontend.py)")
    if text is None:
        return problems
    data = parse_data_js(text)

    for name in pc.ENCODINGS:
        e = data["encodings"].get(name)
        if e is None:
            fail(f"{name}: missing")
            continue
        with open(results_path(name), encoding="utf-8") as f:
            export = json.load(f)
        with open(capacity_path(name), newline="", encoding="utf-8") as f:
            cap = {r["bits"]: int(r["markers_2019"]) for r in csv.DictReader(f)}
        occ = {}
        with open(occupancy_path(name), newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                occ.setdefault(r["bits"], {})[int(r["hour"])] = (int(r["occupied"]), int(r["free_markers"]),
                                                                 int(r["markers_2019"]))
        n0 = len(problems)

        # (b) heavy cells and hour counts
        want = {h["bits"]: h for h in export["heavy_hitters"]}
        got = {c["bits"]: c for c in e["cells"]}
        if set(want) != set(got):
            fail(f"{name}: heavy cells differ: missing {sorted(set(want) - set(got))}, extra {sorted(set(got) - set(want))}")
        for bits, h in want.items():
            c = got.get(bits)
            if c is None:
                continue
            if c["cars"] != h["buckets"] or c["cars"] != h["value"][:pc.HOURS] or c["count"] != h["count"]:
                fail(f"{name} {bits}: hour counts or count differ from the export")
            if sum(c["cars"]) != c["count"] or c["label"] != h["label"]:
                fail(f"{name} {bits}: hour counts do not sum to the count, or label differs")
            # (c) capacity and free spaces
            capacity = cap.get(bits, 0) or None
            if c["capacity"] != capacity:
                fail(f"{name} {bits}: capacity {c['capacity']} != capacity CSV {capacity}")
            if capacity:
                if c["free"] != [capacity - x for x in c["cars"]]:
                    fail(f"{name} {bits}: free != capacity - cars")
                if c["pct"] != [round(100.0 * x / capacity, 1) for x in c["cars"]]:
                    fail(f"{name} {bits}: pct != 100 * cars / capacity")
                o = occ.get(bits, {})
                if [o.get(hr, (None, None, None))[1] for hr in range(pc.HOURS)] != c["free"]:
                    fail(f"{name} {bits}: free spaces differ from free_markers in the occupancy CSV")
            # (e) outline equals the export region and the plaintext encoder's cell
            region = next(x["region"] for x in export["levels"][-1]["candidates"] if x["bits"] == bits)
            if not same_outline(c["outline"], region, 0.5 * 10 ** -COORD_DIGITS + 1e-12):
                fail(f"{name} {bits}: outline differs from the export region")
            if not same_outline(region, computed_outline(name, bits, exact=True)):
                fail(f"{name} {bits}: export region differs from the plaintext encoder's cell")

        # params
        p = export["params"]
        for k in ("tau", "T", "N", "data_bits", "hist_buckets"):
            if e["params"][k] != p[k]:
                fail(f"{name}: params.{k} {e['params'][k]} != export {p[k]}")

        # (d) truth layer = occupancy CSV
        truth = {t["bits"]: t for t in e["truth"]}
        if set(truth) != set(occ):
            fail(f"{name}: truth cells differ from the occupancy CSV")
        for bits, hours in occ.items():
            t = truth.get(bits)
            if t and (t["occupied"] != [hours[hr][0] for hr in range(pc.HOURS)]
                      or t["capacity"] != (hours[0][2] or None) or t["heavy"] != (bits in want)):
                fail(f"{name} {bits}: truth layer differs from the occupancy CSV")
        heavy_truth = sum(1 for b in want if b in truth and truth[b]["occupied"] == want[b]["buckets"])

        # (f) crawl and privacy figures
        lv_e, lv_x = e["crawl"], export["levels"]
        if len(lv_e) != len(lv_x):
            fail(f"{name}: {len(lv_e)} crawl levels, export has {len(lv_x)}")
        revealed = kept = no_region = 0
        for a, b, s in zip(lv_e, lv_x, e["privacy"]["levels"]):
            ca, cb = a["candidates"], b["candidates"]
            drawable = sum(c["outline"] is not None for c in ca)
            nr = sum(c["outline"] is None for c in ca)
            if len(ca) != len(cb) or drawable + nr != len(cb) or s["candidates"] != len(cb):
                fail(f"{name} level {b['level']}: {len(ca)} candidates (drawable {drawable} + {nr}), export {len(cb)}")
            if nr != sum(1 for c in cb if not c["region"]) or s["no_region"] != nr:
                fail(f"{name} level {b['level']}: no-region count differs")
            for x, y in zip(ca, cb):
                if (x["bits"], x["count"], x["heavy"]) != (y["bits"], y["count"], bool(y["heavy"])):
                    fail(f"{name} level {b['level']} {y['bits']}: candidate differs from the export")
            k = sum(bool(c["heavy"]) for c in cb)
            if (s["kept"], s["pruned"]) != (k, len(cb) - k):
                fail(f"{name} level {b['level']}: kept/pruned differ")
            revealed += len(cb)
            kept += k
            no_region += nr
        pv = e["privacy"]
        if (pv["revealed"], pv["kept"], pv["pruned"], pv["no_region"], pv["heavy_cells"]) != (
                revealed, kept, revealed - kept, no_region, len(want)):
            fail(f"{name}: privacy totals differ from the export")

        status = "pass" if len(problems) == n0 else "FAIL"
        print(f"    {name:8} {status}: {len(got)} heavy cells (export {len(want)}), {len(truth)} truth cells, "
              f"heavy cells with truth = Mastic hours: {heavy_truth}/{len(want)}, "
              f"{len(lv_e)} levels, {revealed} regions revealed ({kept} kept, {revealed - kept} pruned, "
              f"{no_region} without region)")
    return problems


def main():
    pc.configure_stdout()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=OUT, help="output file (default: apps/parking/frontend/data.js)")
    ap.add_argument("--check", action="store_true", help="verify the output file against the sources; exit 1 on mismatch")
    args = ap.parse_args()
    if args.check:
        print("checks (b) heavy cells and hour counts = results JSON, (c) capacity / free = CSVs, "
              "(d) truth = occupancy CSV, (e) outlines, (f) crawl per level:")
        problems = check(args.out)
        for p in problems[:50]:
            print(f"  FAIL {p}")
        print("all checks pass" if not problems else f"{len(problems)} problem(s)")
        raise SystemExit(1 if problems else 0)
    text = render(build())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8", newline="\n")
    data = parse_data_js(text)
    print(f"wrote {pc.relpath(args.out) if args.out.is_relative_to(pc.REPO) else args.out} ({len(text):,} bytes)")
    for name, e in data["encodings"].items():
        print(f"  {name:8} {len(e['cells'])} heavy cells, {e['privacy']['revealed']} regions revealed, "
              f"{len(e['truth'])} truth cells, T = {e['params']['T']}, N = {e['params']['N']}")


if __name__ == "__main__":
    main()
