#!/usr/bin/env python3
"""Exact heavy cells per encoding: the plaintext answer every Mastic run must equal (A5, B4).

    .\\.venv\\Scripts\\python.exe bench\\scripts\\ground_truth.py
    .\\.venv\\Scripts\\python.exe bench\\scripts\\ground_truth.py --tau 0.02
    .\\.venv\\Scripts\\python.exe bench\\scripts\\ground_truth.py --csv data\\synthetic\\ten_locations.csv --threshold 0.1
    .\\.venv\\Scripts\\python.exe bench\\scripts\\ground_truth.py --check-grid

Without options: every subset in bench/data/manifest.json (checked against its SHA-256) at
Mastic's tau = 0.005, written to bench/ground_truth/<dataset>_<subset>.json. With --tau: the same at
another tau, written to bench/ground_truth/<dataset>_<subset>_tau<tau>.json (the 0.005 files keep
their names). With --csv: any ride CSV at the given threshold, written to
bench/ground_truth/smoke_<name>.json. With --check-grid: converts
data/mosaic/sample_busiest_day.csv with Mosaic's grid formula and compares the result with
data/mosaic/start_busiest_day.json.

The encoders are imported from scripts/check_heavy_hitters.py, with the parameters in
bench_common.ENCODINGS. Each cell has its label, centre, Mosaic-grid coordinates of the centre
(A5) and count.
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.dont_write_bytecode = True  # no __pycache__ in the repo
sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench_common as bc  # noqa: E402


def heavy_cells(name, points, threshold_frac):
    """The cells whose count reaches max(1, floor(threshold_frac * N)), N = rides encoded."""
    spec = bc.ENCODINGS[name]
    enc = bc.checker_encoder(name)
    to_grid = bc.checker_module().to_grid

    encoded = [enc.encode(lat, lon) for lat, lon in points]
    kept = [bits for bits in encoded if bits is not None]
    wrong = {len(bits) for bits in kept} - {spec["data_bits"]}
    if wrong:
        raise SystemExit(f"{name}: encoder produces {wrong} bits, expected {spec['data_bits']}")
    counts = Counter(kept)
    t = bc.threshold_abs(len(kept), threshold_frac)

    cells = []
    for bits, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        if count < t:
            break
        lat, lon = enc.center(bits)
        cells.append({
            "bits": bits,
            "label": enc.label(bits),
            "lat": lat,
            "lon": lon,
            "grid": list(to_grid(lat, lon)),
            "count": count,
        })
    return {
        "data_bits": spec["data_bits"],
        "params": spec["params"],
        "rows": len(points),
        "dropped": len(points) - len(kept),
        "N": len(kept),
        "T": t,
        "distinct_cells": len(counts),
        "cells": cells,
    }


def ground_truth(csv_path, threshold_frac, dataset, subset, sha256):
    points = bc.read_points(csv_path)
    return {
        "schema_version": bc.SCHEMA_VERSION,
        "system": bc.SYSTEM,
        "dataset": dataset,
        "subset": subset,
        "source": {"path": bc.relpath(csv_path), "sha256": sha256},
        "threshold_frac": threshold_frac,
        "delta": bc.DELTA,
        "encodings": {name: heavy_cells(name, points, threshold_frac) for name in bc.ENCODINGS},
    }


def busiest_count(name, points):
    """Rides in the busiest cell of an encoding (console only; not written to the files)."""
    enc = bc.checker_encoder(name)
    counts = Counter(b for b in (enc.encode(lat, lon) for lat, lon in points) if b is not None)
    return max(counts.values(), default=0)


def print_table(results, busiest=None):
    """One row per encoding, one column per result: heavy cells, T, the busiest cell's count (if
    given) and dropped rides."""
    busiest = busiest or {}
    rows = {
        name: [f"{len(e['cells'])} cells (T={e['T']}"
               + (f", busiest {busiest[label][name]}" if label in busiest else "")
               + f", dropped {e['dropped']})"
               for label, e in ((label, gt["encodings"][name]) for label, gt in results.items())]
        for name in bc.ENCODINGS
    }
    width = 2 + max(len(c) for c in [*results, *(c for cols in rows.values() for c in cols)])
    print(f"{'encoding':14}" + "".join(f"{label:>{width}}" for label in results))
    for name, cols in rows.items():
        print(f"{name:14}" + "".join(f"{c:>{width}}" for c in cols))


def check_grid():
    """Mosaic's grid formula on the day CSV must give data/mosaic/start_busiest_day.json."""
    to_grid = bc.checker_module().to_grid
    ours = [list(to_grid(lat, lon)) for lat, lon in bc.read_points(bc.SOURCE_CSV)]
    reference_path = bc.ROOT / "data" / "mosaic" / "start_busiest_day.json"
    with open(reference_path, encoding="utf-8") as f:
        reference = [[int(a), int(b)] for a, b in json.load(f)]
    same = sum(a == b for a, b in zip(ours, reference))
    as_sets = Counter(map(tuple, ours)) == Counter(map(tuple, reference))
    ok = len(ours) == len(reference) == same
    print(f"{bc.relpath(bc.SOURCE_CSV)}: {len(ours)} points; {bc.relpath(reference_path)}: "
          f"{len(reference)} points")
    print(f"Equal in file order: {same} of {len(reference)}; equal as multisets: {as_sets}")
    print("Grid check:", "PASS" if ok else "FAIL")
    return ok


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tau", type=float,
                        help=f"threshold fraction for the subsets (default {bc.THRESHOLD_FRAC}, "
                             f"Mastic's tau, A2)")
    parser.add_argument("--csv", help="any ride CSV (smoke test) instead of the subsets")
    parser.add_argument("--threshold", type=float, help="threshold fraction for --csv")
    parser.add_argument("--out", help="output file for --csv (default bench/ground_truth/smoke_<name>.json)")
    parser.add_argument("--check-grid", action="store_true", help="only run the grid sanity check")
    args = parser.parse_args()
    if args.tau is not None and not 0 < args.tau <= 1:
        parser.error(f"--tau {args.tau}: must be in (0, 1]")

    if args.check_grid:
        raise SystemExit(0 if check_grid() else 1)

    if args.csv:
        if args.threshold is None and args.tau is None:
            parser.error("--csv needs --threshold")
        if args.threshold is not None and args.tau is not None and args.threshold != args.tau:
            parser.error("--threshold and --tau differ")
        threshold = args.threshold if args.threshold is not None else args.tau
        csv_path = Path(args.csv).resolve()
        gt = ground_truth(csv_path, threshold, None, None, bc.sha256_file(csv_path))
        out = Path(args.out) if args.out else bc.GROUND_TRUTH_DIR / f"smoke_{csv_path.stem}.json"
        bc.write_json(out, gt)
        print(f"{bc.relpath(csv_path)} at threshold {threshold}:")
        for name, e in gt["encodings"].items():
            counts = "/".join(str(c["count"]) for c in e["cells"])
            print(f"  {name:12} N={e['N']} T={e['T']} dropped={e['dropped']}: "
                  f"{len(e['cells'])} cells, counts {counts}")
        print(f"Wrote {bc.relpath(out)}")
        return

    tau = bc.THRESHOLD_FRAC if args.tau is None else args.tau
    manifest = bc.load_manifest()
    results, busiest = {}, {}
    for f in manifest["files"]:
        path = bc.ROOT / f["path"]
        actual = bc.sha256_file(path)
        if actual != f["sha256"]:
            raise SystemExit(f"{f['path']}: sha256 {actual} does not match the manifest "
                             f"({f['sha256']}); regenerate with make_subsets.py")
        gt = ground_truth(path, tau, manifest["dataset"], f["subset"], actual)
        out = bc.ground_truth_path(manifest["dataset"], f["subset"], tau)
        bc.write_json(out, gt)
        label = f"{manifest['dataset']}_{f['subset']} (N={f['rows']})"
        results[label] = gt
        points = bc.read_points(path)
        busiest[label] = {name: busiest_count(name, points) for name in bc.ENCODINGS}
        print(f"Wrote {bc.relpath(out)}")
    print(f"\nHeavy cells at threshold tau = {tau} (T = max(1, floor(tau * N))); busiest = rides "
          f"in the busiest cell:")
    print_table(results, busiest)


if __name__ == "__main__":
    main()
