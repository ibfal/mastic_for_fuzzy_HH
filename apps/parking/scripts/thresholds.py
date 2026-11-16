#!/usr/bin/env python3
"""Threshold re-check on the snapshot reports (P1 measured the thresholds on overlap-B).

1. Mastic: heavy cells per encoding for tau in MASTIC_TAUS, on the application set and on 3of3.
2. Mosaic, delta = 3, per 4-hour window of the application set (N = reports in the window), for tau
   in MOSAIC_WINDOW_TAUS: passing centres and connected areas.
3. Mosaic, delta = 3, on parking_day_3of3.csv as a whole day (no windows; the set of the
   Mastic-vs-Mosaic comparison), for tau in MOSAIC_DAY_TAUS.

Mosaic's neighbourhood as in P1 (explore.py, dist step): a report's grid point is its location rounded
to 0.001 degrees; centres are all grid points within delta of the data extent; a centre's count is the
number of reports in the (2 delta + 1)^2 square around it; it passes if count >= max(1, floor(tau N)).
Areas are 8-connected groups of passing centres.

    .\\.venv\\Scripts\\python.exe -I apps\\parking\\scripts\\thresholds.py

Writes data/thresholds.json and prints the tables with a recommendation.
"""

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # -I drops the script's folder
import parking_common as pc  # noqa: E402
from explore import components  # noqa: E402

MASTIC_TAUS = [0.005, 0.0075, 0.01, 0.0125, 0.015]
MOSAIC_WINDOW_TAUS = [0.05, 0.075, 0.10, 0.125, 0.15]
MOSAIC_DAY_TAUS = [0.02, 0.05, 0.075, 0.10, 0.15]
MASTIC_TARGET = (10, 30)  # heavy cells (PARKING_PLAN.md §6)
MOSAIC_TARGET = (1, 5)  # "a few areas"


def mastic_counts(reports, taus):
    out = {}
    for name in pc.ENCODINGS:
        enc = pc.encoder(name)
        counts = Counter(b for b in (enc.encode(la, lo) for la, lo, _h in reports) if b is not None)
        n = sum(counts.values())
        out[name] = {"N": n, "dropped": len(reports) - n, "distinct_cells": len(counts),
                     "by_tau": {f"{t:g}": {"T": pc.threshold_abs(n, t),
                                           "cells": sum(c >= pc.threshold_abs(n, t) for c in counts.values())}
                                for t in taus}}
    return out


def mosaic_squares(reports, delta, taus):
    cells = Counter((pc.grid_index(la), pc.grid_index(lo)) for la, lo, _h in reports)
    n = sum(cells.values())
    lat0 = min(a for a, _ in cells) - delta
    lon0 = min(b for _, b in cells) - delta
    H = max(a for a, _ in cells) - lat0 + 1 + delta
    W = max(b for _, b in cells) - lon0 + 1 + delta
    P = [[0] * (W + 1) for _ in range(H + 1)]
    for (a, b), c in cells.items():
        P[a - lat0 + 1][b - lon0 + 1] += c
    for i in range(1, H + 1):
        for j in range(1, W + 1):
            P[i][j] += P[i - 1][j] + P[i][j - 1] - P[i - 1][j - 1]

    def square(i, j):
        i0, i1 = max(0, i - delta), min(H, i + delta + 1)
        j0, j1 = max(0, j - delta), min(W, j + delta + 1)
        return P[i1][j1] - P[i0][j1] - P[i1][j0] + P[i0][j0]

    nb = {(i, j): square(i, j) for i in range(H) for j in range(W)}
    mx = max(nb.values())
    out = {"N": n, "grid_points": len(cells), "centres": len(nb), "max_square": mx,
           "max_square_share": mx / n, "by_tau": {}}
    for t in taus:
        T = pc.threshold_abs(n, t)
        passing = {p for p, v in nb.items() if v >= T}
        out["by_tau"][f"{t:g}"] = {"T": T, "centres": len(passing), "areas": components(passing)}
    return out


def pct(t):
    return f"{t * 100:g}%"


def table(cols, rows):
    print("| " + " | ".join(cols) + " |")
    print("|" + "|".join("---" for _ in cols) + "|")
    for r in rows:
        print("| " + " | ".join(str(v) for v in r) + " |")
    print()


def main():
    pc.configure_stdout()
    app = pc.read_reports(pc.report_path("snapshot"))
    s3 = pc.read_reports(pc.subset_path("3of3"))
    result = {"day": pc.DAY.isoformat(), "design": "B snapshot", "delta": pc.DELTA,
              "mastic": {"app": mastic_counts(app, MASTIC_TAUS), "3of3": mastic_counts(s3, MASTIC_TAUS)},
              "mosaic_windows": {}, "mosaic_3of3_day": None, "recommendation": {}}

    print("## 1. Mastic heavy cells (snapshot)\n")
    for label, res in result["mastic"].items():
        print(f"{label}: N = {next(iter(res.values()))['N']:,}")
        table(["encoding"] + [pct(t) for t in MASTIC_TAUS],
              [[name] + [f"{e['by_tau'][f'{t:g}']['cells']} (T={e['by_tau'][f'{t:g}']['T']})" for t in MASTIC_TAUS]
               for name, e in res.items()])

    print(f"## 2. Mosaic, delta = {pc.DELTA}, per 4-hour window (application set)\n")
    for a, b in pc.WINDOWS:
        rep = pc.read_reports(pc.report_path("snapshot", window=(a, b)))
        result["mosaic_windows"][pc.window_name(a, b)] = mosaic_squares(rep, pc.DELTA, MOSAIC_WINDOW_TAUS)
    mw = result["mosaic_windows"]
    table(["window", "N_w", "max 7x7 / N_w"] + [pct(t) for t in MOSAIC_WINDOW_TAUS],
          [[w, f"{r['N']:,}", f"{r['max_square_share']:.1%}"]
           + [f"{r['by_tau'][f'{t:g}']['centres']} / {r['by_tau'][f'{t:g}']['areas']}" for t in MOSAIC_WINDOW_TAUS]
           for w, r in mw.items()])
    print("(cells: passing centres / connected areas)\n")

    print(f"## 3. Mosaic, delta = {pc.DELTA}, parking_day_3of3.csv as a whole day\n")
    d = result["mosaic_3of3_day"] = mosaic_squares(s3, pc.DELTA, MOSAIC_DAY_TAUS)
    table(["N", "max 7x7 / N"] + [pct(t) for t in MOSAIC_DAY_TAUS],
          [[f"{d['N']:,}", f"{d['max_square_share']:.1%}"]
           + [f"{d['by_tau'][f'{t:g}']['centres']} / {d['by_tau'][f'{t:g}']['areas']}" for t in MOSAIC_DAY_TAUS]])

    # Recommendations: the accepted value if it meets the target, else the closest one that does.
    def ok_mastic(t):
        return all(MASTIC_TARGET[0] <= e["by_tau"][f"{t:g}"]["cells"] <= MASTIC_TARGET[1]
                   for res in result["mastic"].values() for e in res.values())

    def ok_windows(t):
        return all(MOSAIC_TARGET[0] <= r["by_tau"][f"{t:g}"]["areas"] <= MOSAIC_TARGET[1] for r in mw.values())

    def ok_day(t):
        return MOSAIC_TARGET[0] <= d["by_tau"][f"{t:g}"]["areas"] <= MOSAIC_TARGET[1]

    def pick(taus, ok, accepted):
        good = [t for t in taus if ok(t)]
        if accepted in good:
            return accepted
        return min(good, key=lambda t: abs(t - accepted)) if good else None

    rec = result["recommendation"] = {
        "mastic_tau": pick(MASTIC_TAUS, ok_mastic, pc.TAU_MASTIC),
        "mosaic_window_tau": pick(MOSAIC_WINDOW_TAUS, ok_windows, pc.TAU_MOSAIC),
        "mosaic_3of3_day_tau_candidates": [t for t in MOSAIC_DAY_TAUS if ok_day(t)],
        "rule": f"Mastic: {MASTIC_TARGET[0]}-{MASTIC_TARGET[1]} heavy cells for every encoding on app and 3of3; "
                f"Mosaic: {MOSAIC_TARGET[0]}-{MOSAIC_TARGET[1]} connected areas (every window, or the whole day)",
    }
    print("Recommendation (automatic rule; see the report for the reasoning):")
    for k, v in rec.items():
        print(f"  {k}: {v}")
    pc.write_json(pc.DATA / "thresholds.json", result)
    print(f"wrote {pc.relpath(pc.DATA / 'thresholds.json')}")


if __name__ == "__main__":
    main()
