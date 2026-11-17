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
  parking every cell with parking (any 2025 bay or 2019 sensor bay), from the capacity CSV only:
          outline, 2019 sensor bays, 2025 bays. No occupancy ("Where to park" view, grey layer).
  last_level  the non-heavy candidates of the crawl's last level with their 24 hourly counts (the
          export's `value`, revealed during the search) and capacity ("Where to park").
  streets the main street(s) of every cell with hourly counts, from data/cell_streets_<enc>.csv
          (cell_streets.py; 2025 bay file road-segment descriptions).
The "Where to park" inputs (cells, parking, last_level, streets) never read the occupancy CSV.

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

SCHEMA_VERSION = 3
RESULTS = pc.APP / "results"
FRONTEND = pc.APP / "frontend"
OUT = FRONTEND / "data.js"
PREFIX = "window.PARKING_DATA = "
SUFFIX = ";\n"
COORD_DIGITS = 7
NAMES = {"morton": "Morton", "geohash": "geohash", "h3": "H3"}
OUTLINE_TOL = 1e-9
# mirrors index.html's OCC_BANDS (% full edges); only for the summary in --check:
# < 35 g0, 35-55 g1, 55-70 g2 (green, darker = more free), 70-80 r0, 80-90 r1, >= 90 r2 (red)
OCC_BANDS = (35, 55, 70, 80, 90)
BAND_NAMES = ("g0", "g1", "g2", "r0", "r1", "r2")
PARKING_KEYS = ("bits", "label", "capacity", "bays_2025", "center", "outline")
CANDIDATE_KEYS = ("bits", "label", "count", "cars", "capacity", "bays_2025", "free", "pct", "center", "outline")


# --- inputs ------------------------------------------------------------------------------------

def results_path(name):
    return RESULTS / f"mastic_{name}.json"


def capacity_path(name):
    return pc.DATA / f"capacity_{name}.csv"


def occupancy_path(name):
    return pc.GT / f"occupancy_{name}.csv"


def streets_path(name):
    return pc.DATA / f"cell_streets_{name}.csv"


def load_streets(name):
    """{bits: [street, ...]} from the lookup written by cell_streets.py (empty list: no 2025 bays)."""
    with open(streets_path(name), newline="", encoding="utf-8") as f:
        return {r["bits"]: [x for x in r["streets"].split("; ") if x] for r in csv.DictReader(f)}


def occ_band(cars, capacity):
    """The Where-to-park colour class at one hour, or None (grey) without 2019 sensor bays."""
    if not capacity:
        return None
    p = 100.0 * cars / capacity
    if p < OCC_BANDS[2] and capacity - cars < 1:
        return None
    return BAND_NAMES[sum(p >= edge for edge in OCC_BANDS)]


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

    enc = pc.encoder(name)
    parking = []
    for bits in sorted(cap):
        r = cap[bits]
        markers, bays = int(r["markers_2019"]), int(r["bays_2025"])
        if not (markers or bays):
            continue
        outl = computed_outline(name, bits)
        parking.append({"bits": bits, "label": r["label"], "capacity": markers or None, "bays_2025": bays,
                        "center": centroid_of(outl), "outline": outl})

    lvl_last = export["levels"][-1]
    last_level = []
    for c in sorted((c for c in lvl_last["candidates"] if not c["heavy"]), key=lambda c: c["bits"]):
        bits = c["bits"]
        capacity = (int(cap[bits]["markers_2019"]) if bits in cap else 0) or None
        cars = list(c["value"][:pc.HOURS])
        outl = outline(c["region"])
        last_level.append({
            "bits": bits,
            "label": cap[bits]["label"] if bits in cap else enc.label(bits),
            "count": c["count"],
            "cars": cars,
            "capacity": capacity,
            "bays_2025": int(cap[bits]["bays_2025"]) if bits in cap else 0,
            "free": [capacity - x for x in cars] if capacity else None,
            "pct": [pct(x, capacity) for x in cars],
            "center": centroid_of(outl) if outl else None,
            "outline": outl,
        })

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
        "parking": parking,
        "last_level": {"level": lvl_last["level"], "candidates": last_level},
        "streets": dict(sorted(load_streets(name).items())),
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
            cap_rows = {r["bits"]: r for r in csv.DictReader(f)}
        cap = {b: int(r["markers_2019"]) for b, r in cap_rows.items()}
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

        # (g) parking cells = capacity CSV rows with any bay; capacity data only, no occupancy
        tol = 0.5 * 10 ** -COORD_DIGITS + 1e-12
        want_park = {b: r for b, r in cap_rows.items() if int(r["markers_2019"]) or int(r["bays_2025"])}
        park = {c["bits"]: c for c in e.get("parking", [])}
        if set(park) != set(want_park):
            fail(f"{name}: parking cells differ from the capacity CSV ({len(park)} vs {len(want_park)})")
        for bits, c in park.items():
            r = want_park.get(bits)
            if tuple(c) != PARKING_KEYS:
                fail(f"{name} {bits}: parking record keys {tuple(c)} (only capacity fields allowed)")
            if r is None:
                continue
            if (c["capacity"], c["bays_2025"], c["label"]) != (int(r["markers_2019"]) or None, int(r["bays_2025"]), r["label"]):
                fail(f"{name} {bits}: parking capacity / 2025 bays / label differ from the capacity CSV")
            if not same_outline(c["outline"], computed_outline(name, bits, exact=True), tol):
                fail(f"{name} {bits}: parking outline differs from the plaintext encoder's cell")

        # (h) last-level candidates: the non-heavy ones = the export's, hourly counts = value[:24];
        #     the heavy ones = heavy_hitters (bits and hours)
        lv_last = export["levels"][-1]
        ll = e.get("last_level", {})
        want_c = {c["bits"]: c for c in lv_last["candidates"] if not c["heavy"]}
        got_c = {c["bits"]: c for c in ll.get("candidates", [])}
        if ll.get("level") != lv_last["level"] or set(got_c) != set(want_c):
            fail(f"{name}: last-level candidates differ from the export (level {ll.get('level')}, {len(got_c)} vs {len(want_c)})")
        hours_equal = 0
        for bits, c in got_c.items():
            x = want_c.get(bits)
            if tuple(c) != CANDIDATE_KEYS:
                fail(f"{name} {bits}: candidate record keys {tuple(c)}")
            if x is None:
                continue
            if c["cars"] != x["value"][:pc.HOURS] or c["count"] != x["count"]:
                fail(f"{name} {bits}: candidate hourly counts differ from the export's value")
            else:
                hours_equal += 1
            if sum(c["cars"]) != c["count"] or (len(x["value"]) > pc.HOURS and x["value"][pc.HOURS] != x["count"]):
                fail(f"{name} {bits}: candidate hours do not sum to the count")
            capacity = cap.get(bits, 0) or None
            if c["capacity"] != capacity or c["bays_2025"] != (int(cap_rows[bits]["bays_2025"]) if bits in cap_rows else 0):
                fail(f"{name} {bits}: candidate capacity {c['capacity']} != capacity CSV {capacity}")
            if c["free"] != ([capacity - v for v in c["cars"]] if capacity else None) or \
                    c["pct"] != [round(100.0 * v / capacity, 1) if capacity else None for v in c["cars"]]:
                fail(f"{name} {bits}: candidate free / pct inconsistent")
            if (c["outline"] is None) != (not x["region"]) or (
                    x["region"] and not same_outline(c["outline"], x["region"], tol)):
                fail(f"{name} {bits}: candidate outline differs from the export region")
        heavy_last = {c["bits"]: c for c in lv_last["candidates"] if c["heavy"]}
        if set(heavy_last) != set(want) or any(heavy_last[b]["value"][:pc.HOURS] != want[b]["buckets"]
                                               for b in want if b in heavy_last):
            fail(f"{name}: heavy last-level candidates differ from heavy_hitters")

        # (i) summary: hourly-data coverage, and red / green / grey per hour computed here in Python
        sensor_cells = sum(1 for c in park.values() if c["capacity"])
        with_park = sum(1 for b in list(got) + list(got_c) if b in park)
        print(f"    {name:8} last-level candidates: {hours_equal}/{len(want_c)} with hourly counts = export value; "
              f"hourly data: {len(got)} heavy + {len(got_c)} last-level = {len(got) + len(got_c)} cells "
              f"({with_park} with parking) of {len(park)} cells with parking ({sensor_cells} with 2019 sensor bays)")
        coloured = list(got.values()) + [c for c in got_c.values() if c["outline"]]
        for hr in (4, 8, 12, 17):
            bands = {b: 0 for b in BAND_NAMES}
            for c in coloured:
                b = occ_band(c["cars"][hr], c["capacity"])
                if b:
                    bands[b] += 1
            red, green = sum(bands[b] for b in BAND_NAMES[3:]), sum(bands[b] for b in BAND_NAMES[:3])
            print(f"    {'':8} {hr:02d}:30 red {red} (>=90 {bands['r2']}, 80-90 {bands['r1']}, 70-80 {bands['r0']}) | "
                  f"green {green} (<35 {bands['g0']}, 35-55 {bands['g1']}, 55-70 {bands['g2']}) | "
                  f"grey {len(park) - red - green}")

        # (j) street names = the lookup CSV, with a row for every cell with hourly counts
        with open(streets_path(name), newline="", encoding="utf-8") as f:
            st_rows = {r["bits"]: r for r in csv.DictReader(f)}
        st = e.get("streets", {})
        if st != {b: [x for x in r["streets"].split("; ") if x] for b, r in st_rows.items()}:
            fail(f"{name}: streets differ from {pc.relpath(streets_path(name))}")
        missing = (set(want) | set(want_c)) - set(st_rows)
        if missing:
            fail(f"{name}: {len(missing)} cells with hourly counts lack a street-name row (run cell_streets.py)")
        print(f"    {'':8} street names: {sum(1 for v in st.values() if v)} of {len(st)} cells with hourly counts")

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
              "(d) truth = occupancy CSV, (e) outlines, (f) crawl per level, (g) parking cells = capacity CSV, "
              "(h) last-level candidates = export values, (i) Where-to-park bands, (j) street names = lookup CSV:")
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
              f"{len(e['truth'])} truth cells, {len(e['parking'])} parking cells, "
              f"{len(e['last_level']['candidates'])} last-level candidates, T = {e['params']['T']}, N = {e['params']['N']}")


if __name__ == "__main__":
    main()
