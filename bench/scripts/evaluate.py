#!/usr/bin/env python3
"""Evaluate the benchmark results (bench/BENCHMARK_MASTIC.md: A4, A5, A7, A10, B4).

    .\\.venv\\Scripts\\python.exe bench\\scripts\\evaluate.py
    .\\.venv\\Scripts\\python.exe bench\\scripts\\evaluate.py --combine bench\\results\\summary_mosaic.csv
    .\\.venv\\Scripts\\python.exe bench\\scripts\\evaluate.py --reference-dir DIR --out-dir DIR2

Reads only bench/results/raw/*.jsonl (<system>.jsonl: one line per run; <system>_client.jsonl:
the client micro-benchmark), the plaintext ground truth at each run's tau
(bench_common.ground_truth_path) and, if it exists, the reference
bench/reference/<dataset>_<subset>.json. The reference must have A5's format (extra fields are
ignored) and A5's setting: tau = 0.02, delta = 5 and T = max(1, floor(0.02 N)) for the subset's N.
Anything else is the wrong file: exit code 2.
Smoke lines (subset "smoke") are skipped. Warm-ups are left out of the summary, but every run,
warm-ups included, is self-checked.

Result lines may use the A9 spec v3.1 additions (Mosaic): server_to_server per server (the
network model uses the larger direction, totals the sum), output counts null (the self-check then
compares locations only and selfcheck_exact is empty), output lat/lon null (not a real cell:
counted in outputs_no_coords, not in outputs or the accuracy) and an `extra` object (shown in
tables.md). The only module of this harness it imports is bench_common.py.

Writes (to --out-dir instead of bench/results/ if given):
- bench/results/summary_<system>.csv: one row per encoding x subset x tau, columns SUMMARY_COLUMNS
  (identical for both systems, so the two files can be concatenated);
- bench/results/tables.md: timings, communication, client cost, accuracy, leakage;
- bench/results/plots/<k>_<name>.png and .pdf: the six plots of A10.
With --combine FILE (the other system's summary, same columns):
- bench/results/comparison.md and bench/results/plots/comparison/ (one marker per system).

The tables and plots are made from the summary CSV as read back from disk, so --combine uses
exactly the same code on both systems' rows.

Exit code 1 if any run fails the self-check or the schema check (the outputs are still written),
2 if the input cannot be used at all (no runs, a reference or ground truth file that does not fit,
a --combine file with other columns).
"""

import argparse
import csv
import json
import math
import re
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

sys.dont_write_bytecode = True  # no __pycache__ in the repo
sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench_common as bc  # noqa: E402  (self_check, BASE_CONFIG: one definition for runner and evaluation)

RAW_DIR = bc.BENCH / "results" / "raw"
RESULTS_DIR = bc.BENCH / "results"
REFERENCE_DIR = bc.BENCH / "reference"
SMOKE_SUBSET = bc.SMOKE_SUBSET  # "smoke"

# --- Summary columns (A10) ---------------------------------------------------------------------
#
# The one definition of the summary's columns. The Mosaic harness must write exactly these
# columns, in this order, so that `--combine` can put the two systems side by side.
# Row key: system x dataset x encoding x subset x threshold_frac (A10).
# A metric with _median/_min/_max columns is summarised over the measured repetitions (warm-ups
# excluded). Per-server values are _s0/_s1. Empty cell = not applicable / not available.
SUMMARY_COLUMNS = (
    # identity and setup; threshold_frac (tau) is part of the row key
    "system", "encoding", "dataset", "subset", "threshold_frac", "n_clients", "threshold_abs",
    "data_bits", "delta",
    "reps_measured", "warmups_excluded", "commit", "dirty", "threads", "cpu",
    # phases (A3), measured on localhost, ms
    "server_offline_ms_median", "server_offline_ms_min", "server_offline_ms_max",
    "upload_ms_median", "upload_ms_min", "upload_ms_max",
    "online_ms_median", "online_ms_min", "online_ms_max",
    # modelled communication (A4): t_comm(online) and t_online_est per profile, ms
    "online_comm_lan_ms_median", "online_comm_lan_ms_min", "online_comm_lan_ms_max",
    "online_est_lan_ms_median", "online_est_lan_ms_min", "online_est_lan_ms_max",
    "online_comm_wan_ms_median", "online_comm_wan_ms_min", "online_comm_wan_ms_max",
    "online_est_wan_ms_median", "online_est_wan_ms_min", "online_est_wan_ms_max",
    # modelled upload of one client (A4): 1 x RTT + report bytes / client uplink, ms
    "client_upload_lan_ms_median", "client_upload_lan_ms_min", "client_upload_lan_ms_max",
    "client_upload_wan_ms_median", "client_upload_wan_ms_min", "client_upload_wan_ms_max",
    # rounds (A4); rounds_upload as implemented, not the model's 1 per client
    "rounds_upload_median", "rounds_upload_min", "rounds_upload_max",
    "rounds_online_median", "rounds_online_min", "rounds_online_max",
    "rounds_extra_median", "rounds_extra_min", "rounds_extra_max",
    # bytes as actually sent (A4), per link and phase
    "bytes_upload_client_to_server_s0_median", "bytes_upload_client_to_server_s0_min",
    "bytes_upload_client_to_server_s0_max",
    "bytes_upload_client_to_server_s1_median", "bytes_upload_client_to_server_s1_min",
    "bytes_upload_client_to_server_s1_max",
    "bytes_upload_total_median", "bytes_upload_total_min", "bytes_upload_total_max",
    "bytes_upload_per_client_median", "bytes_upload_per_client_min", "bytes_upload_per_client_max",
    "bytes_online_collector_to_server_s0_median", "bytes_online_collector_to_server_s0_min",
    "bytes_online_collector_to_server_s0_max",
    "bytes_online_collector_to_server_s1_median", "bytes_online_collector_to_server_s1_min",
    "bytes_online_collector_to_server_s1_max",
    "bytes_online_server_to_collector_s0_median", "bytes_online_server_to_collector_s0_min",
    "bytes_online_server_to_collector_s0_max",
    "bytes_online_server_to_collector_s1_median", "bytes_online_server_to_collector_s1_min",
    "bytes_online_server_to_collector_s1_max",
    "bytes_online_server_to_server_median", "bytes_online_server_to_server_min",
    "bytes_online_server_to_server_max",
    "bytes_online_total_median", "bytes_online_total_min", "bytes_online_total_max",
    "bytes_online_max_link_median", "bytes_online_max_link_min", "bytes_online_max_link_max",
    # leakage counters (A7); kept = sum of frontier_per_level, pruned = revealed - kept
    "revealed_aggregates_total_median", "revealed_aggregates_total_min",
    "revealed_aggregates_total_max",
    "revealed_kept_total_median", "revealed_kept_total_min", "revealed_kept_total_max",
    "revealed_pruned_total_median", "revealed_pruned_total_min", "revealed_pruned_total_max",
    "frontier_final_median", "frontier_final_min", "frontier_final_max",
    "client_validity_bits_median", "client_validity_bits_min", "client_validity_bits_max",
    # accuracy (A5): outputs (reported locations; outputs_no_coords = outputs with null lat/lon,
    # not real cells, A9 spec v3.1), self-check against the same system's plaintext answer at the
    # same tau, and the metrics against the reference, Mosaic's qualifying squares Q at
    # tau = 0.02, delta = 5, grouped into areas (empty until bench/reference/ exists)
    "outputs_median", "outputs_min", "outputs_max",
    "outputs_no_coords_median", "outputs_no_coords_min", "outputs_no_coords_max",
    "selfcheck_precision_median", "selfcheck_precision_min", "selfcheck_precision_max",
    "selfcheck_recall_median", "selfcheck_recall_min", "selfcheck_recall_max",
    "area_recall_median", "area_recall_min", "area_recall_max",
    "precision_median", "precision_min", "precision_max",
    "f1_median", "f1_min", "f1_max",
    "areas_found_median", "areas_found_min", "areas_found_max",
    # single values per row; reference_areas / reference_squares describe the reference (|Q|)
    "selfcheck_exact", "selfcheck_failed_runs", "reference_areas", "reference_squares",
    "online_steps_median_ms", "online_steps_min_ms", "online_steps_max_ms",
    "revealed_kind", "revealed_per_level", "frontier_per_level",
    # client micro-benchmark (A6), one run per encoding
    "client_k", "client_keygen_ms_mean", "client_keygen_ms_median", "client_keygen_ms_p95",
    "client_report_bytes_s0", "client_report_bytes_s1", "client_upload_bytes_per_client",
)
STATS = ("median", "min", "max")


def stat_metrics(columns=SUMMARY_COLUMNS):
    """The metrics summarised as _median/_min/_max, in column order."""
    names = set(columns)
    return tuple(c[: -len("_median")] for c in columns if c.endswith("_median")
                 and all(f"{c[: -len('_median')]}_{s}" in names for s in STATS))


RUN_METRICS = stat_metrics()
STRING_COLUMNS = {"system", "encoding", "dataset", "subset", "commit", "cpu", "revealed_kind",
                  "online_steps_median_ms", "online_steps_min_ms", "online_steps_max_ms",
                  "revealed_per_level", "frontier_per_level"}
BOOL_COLUMNS = {"dirty", "selfcheck_exact"}

# --- Network model (A4) ------------------------------------------------------------------------


@dataclass(frozen=True)
class Profile:
    name: str
    rtt_ms: float
    bandwidth_bps: float  # bits per second, decimal (1 Gbit/s = 1e9 bit/s)


LAN = Profile("LAN", rtt_ms=0.5, bandwidth_bps=1e9)
WAN = Profile("WAN", rtt_ms=50.0, bandwidth_bps=100e6)
PROFILES = (LAN, WAN)
CLIENT_UPLINK_BPS = 10e6  # client uplink, upload only


def transfer_ms(nbytes, bandwidth_bps):
    """Time to push nbytes through a link of bandwidth_bps, in ms."""
    return nbytes * 8 / bandwidth_bps * 1e3


def comm_ms(rounds, link_bytes, profile):
    """t_comm = R x RTT + max_link(B_link) / bandwidth (A4)."""
    return rounds * profile.rtt_ms + transfer_ms(max(link_bytes, default=0), profile.bandwidth_bps)


def online_est_ms(online_ms, rounds, link_bytes, profile):
    """t_online_est = online_ms (measured) + t_comm(online) (A4)."""
    return online_ms + comm_ms(rounds, link_bytes, profile)


def client_upload_ms(report_bytes, profile, uplink_bps=CLIENT_UPLINK_BPS):
    """One client's upload: 1 x RTT + report bytes / client uplink (A4)."""
    return profile.rtt_ms + transfer_ms(report_bytes, uplink_bps)


def per_server(pair):
    """[server0, server1]; a null pair (field does not apply) is [None, None]."""
    return [None, None] if pair is None else list(pair)


def server_to_server_parts(value):
    """server_to_server as a list: [one number] (Mastic: [0]), [sent by server0, sent by server1]
    (A9 spec v3.1), or [None]."""
    return list(value) if isinstance(value, list) else [value]


def server_to_server_total(value):
    """Total server<->server bytes: the number, the sum of both directions, or None."""
    parts = server_to_server_parts(value)
    return None if all(v is None for v in parts) else sum(v or 0 for v in parts)


def online_link_bytes(online):
    """The online links of A4 (collector->server and server->collector per server, server<->server),
    with null fields counted as 0. A per-server server_to_server gives one link per direction (links
    are full duplex, A9 spec v3.1), so max() is the larger direction and sum() the total."""
    c2s = per_server(online["collector_to_server"])
    s2c = per_server(online["server_to_collector"])
    s2s = server_to_server_parts(online["server_to_server"])
    return [v or 0 for v in (*c2s, *s2c, *s2s)]


# --- Accuracy (A5) -----------------------------------------------------------------------------


def to_grid(lat, lon):
    """Cell centre -> Mosaic grid coordinates (scripts/check_heavy_hitters.py, not re-implemented)."""
    return tuple(bc.checker_module().to_grid(lat, lon))


def linf(a, b):
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def f1_score(precision, recall):
    if precision is None or recall is None:
        return None
    return 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)


def reported_grid(output):
    """Mosaic-grid squares of a run's reported locations (A5: cell centre -> grid). Outputs without
    coordinates are not real cells and are not reported locations (A9, spec v3.1)."""
    return [to_grid(c["lat"], c["lon"]) for c in output
            if c.get("lat") is not None and c.get("lon") is not None]


def connected_areas(squares):
    """Squares split into connected groups (A5): two squares are connected if they touch on a side
    or a corner (8-neighbourhood, L-inf distance 1). Sets, in order of each group's first square."""
    order = list(dict.fromkeys(tuple(g) for g in squares))
    remaining = set(order)
    areas = []
    for start in order:
        if start not in remaining:
            continue
        remaining.discard(start)
        area, stack = {start}, [start]
        while stack:
            a, b = stack.pop()
            for n in ((a + da, b + db) for da in (-1, 0, 1) for db in (-1, 0, 1)):
                if n in remaining:
                    remaining.discard(n)
                    area.add(n)
                    stack.append(n)
        areas.append(area)
    return areas


def match_reference(reported, reference):
    """A5 accuracy of reported grid squares against a reference (parse_reference), within
    L-inf <= the reference's delta:

    area_recall = share of reference areas with at least one reported location within delta of a
                  square of that area;
    precision   = share of reported locations within delta of some square in Q;
    f1          = 2 P R / (P + R);
    areas_found / reference_areas, outputs / reference_squares (|Q|).
    Undefined values (no areas, no outputs) are None. Counts are not compared (A5).
    """
    delta, area_of = reference["delta"], reference["area_of"]
    offsets = [(da, db) for da in range(-delta, delta + 1) for db in range(-delta, delta + 1)]
    found, near = set(), 0
    for a, b in reported:
        hit = {area_of[(a + da, b + db)] for da, db in offsets if (a + da, b + db) in area_of}
        near += bool(hit)
        found |= hit
    n_areas = len(reference["areas"])
    recall = len(found) / n_areas if n_areas else None
    precision = near / len(reported) if reported else None
    return {
        "area_recall": recall,
        "precision": precision,
        "f1": f1_score(precision, recall),
        "areas_found": len(found),
        "reference_areas": n_areas,
        "outputs": len(reported),
        "reference_squares": len(area_of),
        "found": sorted(found),
    }


def self_check(line, truth):
    """A5 self-check of one run against the same encoding's plaintext ground truth at the run's tau.

    bench_common.self_check (outputs matched by label, every count exact or none revealed, centres
    equal where both sides have coordinates), plus: the Mosaic-grid coordinates of every matched
    centre equal the ground truth's, and N and T equal the ground truth's. `ok` is the overall
    verdict. A ground-truth cell for an output without coordinates has null lat, lon and grid.
    """
    if truth is None:
        return {"precision": None, "recall": None, "exact": False, "ok": False,
                "problems": ["no ground truth for this encoding, subset and τ "
                             "(ground_truth.py --tau)"]}
    check = bc.self_check(line["output"], truth)
    want = {c["label"]: c.get("grid") for c in truth["cells"]}
    problems = list(check["problems"])
    if any(to_grid(c["lat"], c["lon"]) != tuple(want[c["label"]]) for c in line["output"]
           if want.get(c["label"]) is not None and c["lat"] is not None and c["lon"] is not None):
        problems.append("grid coordinates differ")
    if line["n_clients"] != truth["N"]:
        problems.append(f"n_clients {line['n_clients']} != N {truth['N']}")
    if line["params"]["threshold_abs"] != truth["T"]:
        problems.append(f"threshold {line['params']['threshold_abs']} != T {truth['T']}")
    if not check["ok"]:
        problems.append("cells, counts or centres differ")
    return {**check, "ok": check["ok"] and not problems, "problems": problems}


class ReferenceFormatError(ValueError):
    """A reference file that does not have the A5 format or is inconsistent with A5's definition."""


REFERENCE_KEYS = ("dataset", "subset", "tau", "T", "delta", "squares", "areas")
SQUARE_KEYS = ("grid", "count", "area")
AREA_KEYS = ("id", "peak", "peak_count", "size")


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _grid(v):
    """[lat_grid, lon_grid] -> (lat_grid, lon_grid), or None if v is not two integers."""
    if isinstance(v, list) and len(v) == 2 and all(_is_int(x) for x in v):
        return tuple(v)
    return None


def _keys(obj, keys, where):
    """obj must be an object with every A5 field in `keys`. Extra fields (e.g. the Mosaic harness's
    N and source) are allowed and ignored."""
    if not isinstance(obj, dict):
        raise ReferenceFormatError(f"{where}: expected an object with {list(keys)}, got "
                                   f"{type(obj).__name__}")
    missing = [k for k in keys if k not in obj]
    if missing:
        raise ReferenceFormatError(f"{where}: A5 fields missing: {missing}; required "
                                   f"{list(keys)} (extra fields are ignored)")


def parse_reference(data, where="reference", dataset=None, subset=None):
    """The A5 reference, checked: Mosaic's qualifying squares Q with their neighbourhood counts,
    grouped into areas of squares that touch, including diagonally.

        {"dataset": "day", "subset": "3of3", "tau": 0.02, "T": 431, "delta": 5,
         "squares": [{"grid": [lat_grid, lon_grid], "count": 1192, "area": 0}, ...],
         "areas":   [{"id": 0, "peak": [lat_grid, lon_grid], "peak_count": 3238, "size": 870}, ...]}

    Raises ReferenceFormatError for any other format and for a file that contradicts A5: every one
    of these fields (extra fields at any level are allowed and ignored), integer grid coordinates,
    no square twice, every count >= T, every area id used, size = the area's squares, peak = a
    square of the area with the highest count, and the areas equal to the 8-connected components
    of Q (connected_areas). dataset/subset, if given, must match the file. Whether tau, delta and T
    are A5's is checked separately (check_reference_setting). Returns {"dataset", "subset", "tau",
    "T", "delta", "squares": {grid: count}, "area_of": {grid: area id}, "areas": [...],
    "path": where}.
    """
    _keys(data, REFERENCE_KEYS, where)
    if not isinstance(data["dataset"], str) or not isinstance(data["subset"], str):
        raise ReferenceFormatError(f"{where}: dataset and subset must be strings")
    if dataset is not None and (data["dataset"], data["subset"]) != (dataset, subset):
        raise ReferenceFormatError(f"{where}: holds {data['dataset']}_{data['subset']}, expected "
                                   f"{dataset}_{subset}")
    if not _is_num(data["tau"]) or not 0 < data["tau"] <= 1:
        raise ReferenceFormatError(f"{where}: tau must be a number in (0, 1], got {data['tau']!r}")
    if not _is_int(data["T"]) or data["T"] < 1:
        raise ReferenceFormatError(f"{where}: T must be a positive integer, got {data['T']!r}")
    if not _is_int(data["delta"]) or data["delta"] < 0:
        raise ReferenceFormatError(f"{where}: delta must be an integer >= 0, got {data['delta']!r}")
    for key in ("squares", "areas"):
        if not isinstance(data[key], list):
            raise ReferenceFormatError(f"{where}: {key} must be a list")

    areas = {}
    for i, a in enumerate(data["areas"]):
        w = f"{where}: areas[{i}]"
        _keys(a, AREA_KEYS, w)
        peak = _grid(a["peak"])
        if not _is_int(a["id"]) or peak is None or not _is_int(a["peak_count"]) \
                or not _is_int(a["size"]):
            raise ReferenceFormatError(f"{w}: id, peak_count and size must be integers and peak "
                                       f"[lat_grid, lon_grid]: {a!r}")
        if a["id"] in areas:
            raise ReferenceFormatError(f"{w}: area id {a['id']} appears twice")
        areas[a["id"]] = {"id": a["id"], "peak": peak, "peak_count": a["peak_count"],
                          "size": a["size"]}

    squares, area_of = {}, {}
    for i, s in enumerate(data["squares"]):
        w = f"{where}: squares[{i}]"
        _keys(s, SQUARE_KEYS, w)
        grid = _grid(s["grid"])
        if grid is None or not _is_int(s["count"]) or not _is_int(s["area"]):
            raise ReferenceFormatError(f"{w}: grid must be [lat_grid, lon_grid] and count and "
                                       f"area integers: {s!r}")
        if grid in squares:
            raise ReferenceFormatError(f"{w}: square {list(grid)} appears twice")
        if s["count"] < data["T"]:
            raise ReferenceFormatError(f"{w}: count {s['count']} < T = {data['T']}, so the "
                                       f"square does not qualify (A5: Q = squares with count >= T)")
        if s["area"] not in areas:
            raise ReferenceFormatError(f"{w}: area {s['area']} is not in areas")
        squares[grid], area_of[grid] = s["count"], s["area"]

    members = defaultdict(set)
    for grid, aid in area_of.items():
        members[aid].add(grid)
    for aid, a in areas.items():
        own = members.get(aid, set())
        w = f"{where}: area {aid}"
        if len(own) != a["size"]:
            raise ReferenceFormatError(f"{w}: size {a['size']}, but {len(own)} squares have "
                                       f"area {aid}")
        top = max((squares[g] for g in own), default=None)
        if a["peak"] not in own or squares[a["peak"]] != a["peak_count"] or a["peak_count"] != top:
            raise ReferenceFormatError(f"{w}: peak {list(a['peak'])} with peak_count "
                                       f"{a['peak_count']} is not a square of the area with its "
                                       f"highest count ({top})")
    components = connected_areas(squares)
    if {frozenset(c) for c in components} != {frozenset(m) for m in members.values()}:
        raise ReferenceFormatError(
            f"{where}: the areas are not the connected groups of Q (squares touching on a side or "
            f"a corner, A5): the file has {len(areas)} areas of sizes "
            f"{sorted((len(m) for m in members.values()), reverse=True)[:8]}, Q has "
            f"{len(components)} groups of sizes "
            f"{sorted((len(c) for c in components), reverse=True)[:8]}")
    return {"dataset": data["dataset"], "subset": data["subset"], "tau": data["tau"],
            "T": data["T"], "delta": data["delta"], "squares": squares, "area_of": area_of,
            "areas": [areas[k] for k in sorted(areas)], "path": where}


def load_reference(path, dataset=None, subset=None):
    """parse_reference of a JSON file; any problem is a ReferenceFormatError naming the file."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ReferenceFormatError(f"{bc.relpath(path)}: cannot read JSON ({e})") from None
    ref = parse_reference(data, bc.relpath(path), dataset, subset)
    ref["path"] = Path(path)
    return ref


def check_reference_setting(ref, ns):
    """A5's setting for the reference: tau = 0.02, delta = 5 and T = max(1, floor(0.02 N)) for
    every N in ns (the subset's N, from its runs). A file made with other parameters is the wrong
    file: raises ReferenceFormatError naming every difference."""
    where = bc.relpath(ref["path"]) if isinstance(ref["path"], Path) else ref["path"]
    problems = []
    if not math.isclose(ref["tau"], bc.REFERENCE_TAU, rel_tol=1e-9):
        problems.append(f"tau = {ref['tau']:g}, A5 requires {bc.REFERENCE_TAU:g}")
    if ref["delta"] != bc.REFERENCE_DELTA:
        problems.append(f"delta = {ref['delta']}, A5 requires {bc.REFERENCE_DELTA}")
    for n in sorted(ns):
        want = bc.threshold_abs(n, bc.REFERENCE_TAU)
        if ref["T"] != want:
            problems.append(f"T = {ref['T']}, A5 requires max(1, floor({bc.REFERENCE_TAU:g} x {n})) "
                            f"= {want} for N = {n}")
    if problems:
        raise ReferenceFormatError(f"{where}: {'; '.join(problems)}. This is not A5's reference "
                                   f"(wrong file?)")


# --- Leakage (A7) and statistics ---------------------------------------------------------------


def leakage_metrics(leakage):
    """Totals of the leakage counters. Kept prefixes = frontier_per_level; pruned = revealed - kept."""
    total = leakage["revealed_aggregates_total"]
    frontier = leakage["frontier_per_level"]
    kept = sum(frontier) if frontier else None
    return {
        "revealed_aggregates_total": total,
        "revealed_kept_total": kept,
        "revealed_pruned_total": None if total is None or kept is None else total - kept,
        "frontier_final": frontier[-1] if frontier else None,
        "client_validity_bits": leakage["client_validity_bits"],
    }


def summarize(values):
    """(median, min, max) of the non-null values, or (None, None, None)."""
    values = [v for v in values if v is not None]
    if not values:
        return None, None, None
    return statistics.median(values), min(values), max(values)


ACCURACY_METRICS = ("area_recall", "precision", "f1", "areas_found")


def run_metrics(line, reference=None):
    """The per-run metrics of RUN_METRICS, except the self-check ones. reference: parse_reference
    output, or None (accuracy metrics None)."""
    phases, rounds, b = line["phases"], line["rounds"], line["bytes"]
    n = line["n_clients"]
    up = per_server(b["upload"]["client_to_server"])
    c2s = per_server(b["online"]["collector_to_server"])
    s2c = per_server(b["online"]["server_to_collector"])
    links = online_link_bytes(b["online"])
    # The Merkle re-sends (rounds.extra) are sequential steps of the online phase.
    model_rounds = (rounds["online"] or 0) + (rounds["extra"] or 0)
    upload_total = sum(v or 0 for v in up)
    per_client = upload_total / n if n else None
    m = {
        "server_offline_ms": phases["server_offline_ms"],
        "upload_ms": phases["upload_ms"],
        "online_ms": phases["online_ms"],
        "rounds_upload": rounds["upload"],
        "rounds_online": rounds["online"],
        "rounds_extra": rounds["extra"],
        "bytes_upload_client_to_server_s0": up[0],
        "bytes_upload_client_to_server_s1": up[1],
        "bytes_upload_total": upload_total,
        "bytes_upload_per_client": per_client,
        "bytes_online_collector_to_server_s0": c2s[0],
        "bytes_online_collector_to_server_s1": c2s[1],
        "bytes_online_server_to_collector_s0": s2c[0],
        "bytes_online_server_to_collector_s1": s2c[1],
        "bytes_online_server_to_server": server_to_server_total(b["online"]["server_to_server"]),
        "bytes_online_total": sum(links),
        "bytes_online_max_link": max(links),
        **leakage_metrics(line["leakage"]),
    }
    reported = reported_grid(line["output"])
    m["outputs"] = len(reported)
    m["outputs_no_coords"] = len(line["output"]) - len(reported)
    for p in PROFILES:
        key = p.name.lower()
        m[f"online_comm_{key}_ms"] = comm_ms(model_rounds, links, p)
        m[f"online_est_{key}_ms"] = (None if phases["online_ms"] is None
                                     else online_est_ms(phases["online_ms"], model_rounds, links, p))
        m[f"client_upload_{key}_ms"] = None if per_client is None else client_upload_ms(per_client, p)
    acc = match_reference(reported, reference) if reference is not None else {}
    for k in ACCURACY_METRICS:
        m[k] = acc.get(k)
    return m


# --- Loading -----------------------------------------------------------------------------------


def run_label(line):
    warm = " (warm-up)" if line.get("warmup") else ""
    return f"{line.get('system')} {line.get('encoding')} {line.get('subset')} rep {line.get('rep')}{warm}"


def load_raw(raw_dir=RAW_DIR):
    """Run lines, client lines, skipped smoke lines and schema problems from raw_dir/*.jsonl."""
    runs, clients, smoke, problems = [], [], 0, []
    files = sorted(Path(raw_dir).glob("*.jsonl"))
    for path in files:
        kind = "client" if path.stem.endswith("_client") else "run"
        for i, text in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not text.strip():
                continue
            where = f"{bc.relpath(path)}:{i}"
            try:
                line = json.loads(text)
            except json.JSONDecodeError as e:
                problems.append(f"{where}: not JSON ({e})")
                continue
            if line.get("subset") == SMOKE_SUBSET:
                smoke += 1
                continue
            errors = bc.validate_result(line, kind)
            if errors:
                problems.append(f"{where}: schema: {errors[0]}"
                                + (f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""))
                continue
            (clients if kind == "client" else runs).append({"line": line, "where": where})
    return {"files": files, "runs": runs, "clients": clients, "smoke": smoke, "problems": problems}


def load_ground_truth(dataset, subset, tau):
    """The plaintext answer at this tau (bench_common.ground_truth_path), or None if there is no
    file. A file whose threshold_frac is not tau is an error (ValueError)."""
    path = bc.ground_truth_path(dataset, subset, tau)
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        gt = json.load(f)
    if gt.get("threshold_frac") != tau:
        raise ValueError(f"{bc.relpath(path)}: threshold_frac {gt.get('threshold_frac')!r}, "
                         f"expected {tau}")
    gt["path"] = path
    return gt


# --- Summary rows ------------------------------------------------------------------------------


def joined(values):
    """The single value, or the distinct values joined by '+' (flagged by the caller)."""
    distinct = list(dict.fromkeys(values))
    return distinct[0] if len(distinct) == 1 else "+".join(str(v) for v in distinct)


def steps_string(lines, stat):
    """name=ms;... over the measured runs (median, min or max per step), in step order."""
    names, per_name = [], defaultdict(list)
    for line in lines:
        for step in line["phases"]["online_steps"]:
            if step["name"] not in per_name:
                names.append(step["name"])
            per_name[step["name"]].append(step["ms"])
    i = STATS.index(stat)
    parts = []
    for name in names:
        v = summarize(per_name[name])[i]
        parts.append(f"{name}={'' if v is None else round(v, 3)}")
    return ";".join(parts)


def list_string(lists):
    """'2;4;8' if every run has the same list, 'varies' otherwise, '' if empty."""
    distinct = {tuple(x) for x in lists}
    if not distinct:
        return ""
    return ";".join(map(str, distinct.pop())) if len(distinct) == 1 else "varies"


def summary_row(runs, client=None, reference=None):
    """One summary row for the runs of one system x encoding x subset x tau.

    runs: [{"line", "metrics", "check"}]; warm-ups are counted but excluded from every statistic.
    delta is the evaluation radius: the reference's delta if there is a reference, else A2's."""
    measured = [r for r in runs if not r["line"]["warmup"]]
    lines = [r["line"] for r in measured] or [r["line"] for r in runs]
    first = lines[0]
    row = dict.fromkeys(SUMMARY_COLUMNS)
    row.update({
        "system": first["system"],
        "encoding": first["encoding"],
        "dataset": first["dataset"],
        "subset": first["subset"],
        "threshold_frac": joined([x["params"]["threshold_frac"] for x in lines]),
        "n_clients": joined([x["n_clients"] for x in lines]),
        "threshold_abs": joined([x["params"]["threshold_abs"] for x in lines]),
        "data_bits": joined([x["params"]["data_bits"] for x in lines]),
        "delta": bc.DELTA if reference is None else reference["delta"],
        "reps_measured": len(measured),
        "warmups_excluded": len(runs) - len(measured),
        "commit": joined([x["env"]["commit"] for x in lines]),
        "dirty": joined([x["env"]["dirty"] for x in lines]),
        "threads": joined([x["env"]["threads"] for x in lines]),
        "cpu": joined([x["env"]["cpu"] for x in lines]),
        "revealed_kind": joined([x["leakage"]["revealed_kind"] for x in lines]),
    })
    for metric in RUN_METRICS:
        source = "check" if metric.startswith("selfcheck_") else "metrics"
        key = metric[len("selfcheck_"):] if source == "check" else metric
        values = summarize([r[source].get(key) for r in measured])
        for stat, v in zip(STATS, values):
            row[f"{metric}_{stat}"] = v
    exact = [r["check"]["exact"] for r in measured]
    # Empty (None) when a measured run reveals no counts (A9 spec v3.1): only locations compared.
    row["selfcheck_exact"] = None if any(e is None for e in exact) else bool(measured) and all(exact)
    row["selfcheck_failed_runs"] = sum(not r["check"]["ok"] for r in measured)
    row["reference_areas"] = None if reference is None else len(reference["areas"])
    row["reference_squares"] = None if reference is None else len(reference["squares"])
    for stat in STATS:
        row[f"online_steps_{stat}_ms"] = steps_string([r["line"] for r in measured], stat)
    row["revealed_per_level"] = list_string(
        [r["line"]["leakage"]["revealed_aggregates_per_level"] for r in measured])
    row["frontier_per_level"] = list_string(
        [r["line"]["leakage"]["frontier_per_level"] for r in measured])
    if client is not None:
        c = client["client"]
        report = per_server(c["report_bytes_per_server"])
        row.update({
            "client_k": c["k"],
            "client_keygen_ms_mean": c["keygen_ms_mean"],
            "client_keygen_ms_median": c["keygen_ms_median"],
            "client_keygen_ms_p95": c["keygen_ms_p95"],
            "client_report_bytes_s0": report[0],
            "client_report_bytes_s1": report[1],
            "client_upload_bytes_per_client": c["upload_bytes_per_client"],
        })
    return row


def _fextra(v):
    return f"{int(v):,}" if v == int(v) else f"{v:,.3f}"


def extra_values(runs):
    """{key: text} for the runs' A9 `extra` objects (spec v3.1), over the measured runs (warm-ups
    excluded): numbers as median [min–max], anything else as its distinct JSON values. A key that
    only some runs have gets "(k of n runs)". Empty if no run has `extra`."""
    measured = [r["line"] for r in runs if not r["line"]["warmup"]]
    keys = list(dict.fromkeys(k for x in measured for k in (x.get("extra") or {})))
    out = {}
    for key in keys:
        values = [x["extra"][key] for x in measured if key in (x.get("extra") or {})]
        if all(_is_num(v) for v in values):
            med, lo, hi = summarize(values)
            text = _fextra(med) if lo == hi else f"{_fextra(med)} [{_fextra(lo)}–{_fextra(hi)}]"
        else:
            text = " / ".join(dict.fromkeys(json.dumps(v, ensure_ascii=False, sort_keys=True,
                                                       separators=(",", ":")) for v in values))
        if len(values) < len(measured):
            text += f" ({len(values)} of {len(measured)} runs)"
        out[key] = text
    return out


def csv_value(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        if math.isfinite(v) and v == int(v) and abs(v) < 1e15:
            return str(int(v))
        return f"{v:.6f}".rstrip("0").rstrip(".")
    return str(v)


def write_summary(rows, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(SUMMARY_COLUMNS)
        for row in rows:
            w.writerow([csv_value(row[c]) for c in SUMMARY_COLUMNS])


def parse_value(column, text):
    if column in STRING_COLUMNS:
        return text
    if text == "":
        return None
    if column in BOOL_COLUMNS:
        return {"true": True, "false": False}.get(text.lower(), text)
    try:
        return float(text)
    except ValueError:
        return text  # e.g. "7194+7195" when the runs disagree; flagged when the row was made


def read_summary(path):
    """Rows of a summary CSV, typed; raises ValueError if the columns are not SUMMARY_COLUMNS."""
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = tuple(next(reader, ()))
        if header != SUMMARY_COLUMNS:
            missing = [c for c in SUMMARY_COLUMNS if c not in header]
            extra = [c for c in header if c not in SUMMARY_COLUMNS]
            raise ValueError(f"{path}: columns differ from SUMMARY_COLUMNS"
                             f" (missing {missing[:5]}, unexpected {extra[:5]}"
                             f"{', or a different order' if not missing and not extra else ''})")
        return [{c: parse_value(c, v) for c, v in zip(header, values)} for values in reader]


def tau_key(tau):
    """Sort key for tau values: numbers ascending, then anything else (e.g. a foreign "a+b")."""
    return (0, tau, "") if _is_num(tau) else (1, 0, str(tau))


def ftau(tau):
    """tau for tables and labels: 0.005 -> "0.005", None -> dash."""
    if tau is None:
        return DASH
    return f"{tau:g}" if _is_num(tau) else str(tau)


def row_key(r):
    """The row key of A10: system x dataset x encoding x subset x tau."""
    return (r["system"], r["dataset"], r["encoding"], r["subset"], r["threshold_frac"])


def row_order(rows, systems=None):
    """System (given order), then tau ascending, encoding (ENCODINGS order), subset."""
    systems = systems or list(dict.fromkeys(r["system"] for r in rows))
    enc, sub = list(bc.ENCODINGS), list(bc.SUBSETS)

    def key(r):
        return (systems.index(r["system"]) if r["system"] in systems else len(systems),
                tau_key(r["threshold_frac"]),
                enc.index(r["encoding"]) if r["encoding"] in enc else len(enc), r["encoding"],
                sub.index(r["subset"]) if r["subset"] in sub else len(sub), r["subset"])

    return sorted(rows, key=key)


def duplicate_keys(rows):
    """Row keys (A10) that appear more than once."""
    seen = defaultdict(int)
    for r in rows:
        seen[row_key(r)] += 1
    return [k for k, n in seen.items() if n > 1]


# --- Checks specific to Mastic (B3, A7 decisions) ----------------------------------------------


def mastic_checks(rows):
    """B3 sanity checks on the summary rows of the Mastic system: [(check, row, expected, got, ok)]."""
    batch = bc.BASE_CONFIG["add_report_share_batch_size"]
    flp_batch = bc.BASE_CONFIG["query_flp_batch_size"]
    out = []
    for r in rows:
        if r["system"] != "mastic" or not isinstance(r["n_clients"], (int, float)):
            continue
        n_clients, n = int(r["n_clients"]), r["data_bits"]
        where = f"{r['encoding']} {r['subset']}"
        if isinstance(n, (int, float)) and r["rounds_extra_max"] == 0:
            want = 2 * int(n) + 3 + 2 * math.ceil(n_clients / flp_batch)
            out.append(("online rounds = 2n + 5 (extra = 0)", where, want, r["rounds_online_max"],
                        r["rounds_online_min"] == r["rounds_online_max"] == want))
        want = math.ceil(n_clients / batch)
        out.append(("upload rounds as implemented = ceil(N / 1000)", where, want,
                    r["rounds_upload_max"], r["rounds_upload_min"] == r["rounds_upload_max"] == want))
        want = 2 * n_clients
        out.append(("client validity bits = 2N", where, want, r["client_validity_bits_max"],
                    r["client_validity_bits_min"] == r["client_validity_bits_max"] == want))
        if r["client_upload_bytes_per_client"] is not None:
            got = r["bytes_upload_per_client_median"]
            want = r["client_upload_bytes_per_client"]
            out.append(("upload bytes per client (runs) = report size (micro-benchmark)", where,
                        want, round(got, 3), abs(got - want) < 0.01))
        out.append(("server <-> server bytes = 0 (B3 topology)", where, 0,
                    r["bytes_online_server_to_server_max"],
                    r["bytes_online_server_to_server_max"] == 0))
    return out


# --- Formatting --------------------------------------------------------------------------------

DASH = "—"


def fnum(v, digits=1):
    if v is None:
        return DASH
    if isinstance(v, str):
        return v
    return f"{v:,.{digits}f}"


def fint(v):
    if v is None:
        return DASH
    if isinstance(v, str):
        return v
    return f"{int(round(v)):,}"


def frange(row, metric, digits=1, scale=1.0):
    med, lo, hi = (row[f"{metric}_{s}"] for s in STATS)
    if med is None:
        return DASH
    if lo == hi:
        return fnum(med / scale, digits)
    return f"{fnum(med / scale, digits)} [{fnum(lo / scale, digits)}–{fnum(hi / scale, digits)}]"


def fpair(row, metric, fmt):
    """Per-server median: one value if both servers are equal, 's0 / s1' otherwise."""
    a, b = row[f"{metric}_s0_median"], row[f"{metric}_s1_median"]
    return fmt(a) if a == b else f"{fmt(a)} / {fmt(b)}"


def md_table(header, rows, align=None):
    align = align or ["l"] + ["r"] * (len(header) - 1)
    rule = ["---:" if a == "r" else "---" for a in align]
    out = ["| " + " | ".join(header) + " |", "|" + "|".join(rule) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def subset_label(subset):
    m = re.fullmatch(r"(\d+)of(\d+)", str(subset))
    return f"{m.group(1)}/{m.group(2)}" if m else str(subset)


def parse_steps(text):
    out = []
    for part in (text or "").split(";"):
        if "=" in part:
            name, v = part.split("=", 1)
            out.append((name, float(v) if v else None))
    return out


def grouped_steps(text):
    """Steps with level_<i> folded into one 'levels' entry (sum of the per-level values)."""
    out, levels = [], None
    for name, v in parse_steps(text):
        if re.fullmatch(r"level_\d+", name):
            if levels is None:
                levels = len(out)
                out.append(["levels (sum)", 0.0, 0])
            out[levels][1] += v or 0.0
            out[levels][2] += 1
        else:
            out.append([name, v, None])
    return out


# --- tables.md ---------------------------------------------------------------------------------


def key_cols(r, multi):
    cols = [r["system"]] if multi else []
    return cols + [f"`{r['encoding']}`", subset_label(r["subset"]), ftau(r["threshold_frac"]),
                   fint(r["n_clients"])]


def key_header(multi):
    return (["System"] if multi else []) + ["Encoding", "Subset", "τ", "N"]


def tables_md(rows, info):
    rows = row_order(rows)
    systems = list(dict.fromkeys(r["system"] for r in rows))
    multi = len(systems) > 1
    kh = key_header(multi)
    ka = ["l"] * len(kh[:-2]) + ["r", "r"]
    out = [f"# Benchmark results: {', '.join(systems)}", "",
           "Generated by `bench/scripts/evaluate.py` from `bench/results/raw/`. Do not edit by hand; "
           "rerun the script. Values are medians over the measured repetitions, with [min–max] when "
           "they differ; warm-ups and smoke lines are excluded.", ""]
    out += [f"- {x}" for x in info["inputs"]] + [""]
    out += info["banners"] + [""]

    out += ["## 1. Timings (localhost, ms)", "",
            "Server offline is 0 for Mastic: it has no input-independent server preprocessing (B2). "
            "Upload is reported separately and is never part of online (A3).", ""]
    out.append(md_table(kh + ["T", "Server offline", "Upload", "Online"],
                        [key_cols(r, multi) + [fint(r["threshold_abs"]),
                                               frange(r, "server_offline_ms"),
                                               frange(r, "upload_ms"), frange(r, "online_ms")]
                         for r in rows], ka + ["r"] * 4))

    out += ["", "### Online steps (median ms)", "",
            "`levels (sum)` adds the medians of the per-level steps (`level_0` … `level_{n-2}`); "
            "all steps per level are in the summary CSV (`online_steps_*_ms`).", ""]
    names = []
    for r in rows:
        for name, _, _ in grouped_steps(r["online_steps_median_ms"]):
            if name not in names:
                names.append(name)
    body = []
    for r in rows:
        steps = {name: (v, k) for name, v, k in grouped_steps(r["online_steps_median_ms"])}
        cells = []
        for name in names:
            v, k = steps.get(name, (None, None))
            cells.append(fnum(v) + (f" ({k})" if k else ""))
        total = sum(v or 0 for v, _ in steps.values())
        body.append(key_cols(r, multi) + cells + [fnum(total), fnum(r["online_ms_median"])])
    out.append(md_table(kh + names + ["Sum of step medians", "Online"], body,
                        ka + ["r"] * (len(names) + 2)))

    out += ["", "## 2. Modelled communication and online time (A4)", "",
            f"t_comm(online) = R × RTT + max_link(B) / bandwidth with R = online + extra rounds and "
            f"the busiest single link; t_online_est = measured online + t_comm(online). "
            f"LAN: RTT {LAN.rtt_ms:g} ms, {LAN.bandwidth_bps / 1e9:g} Gbit/s. WAN: RTT "
            f"{WAN.rtt_ms:g} ms, {WAN.bandwidth_bps / 1e6:g} Mbit/s (1 Gbit = 10⁹ bit). "
            f"Medians [min–max] over the measured runs.", ""]
    out.append(md_table(
        kh + ["Online (measured)", "Rounds", "Max link (MB)", "LAN t_comm", "LAN online est.",
              "WAN t_comm", "WAN online est."],
        [key_cols(r, multi) + [
            fnum(r["online_ms_median"]),
            fint((r["rounds_online_median"] or 0) + (r["rounds_extra_median"] or 0)),
            fnum(r["bytes_online_max_link_median"] / 1e6, 3)
            if r["bytes_online_max_link_median"] is not None else DASH,
            frange(r, "online_comm_lan_ms"), frange(r, "online_est_lan_ms"),
            frange(r, "online_comm_wan_ms"), frange(r, "online_est_wan_ms")] for r in rows],
        ka + ["r"] * 7))

    out += ["", "### Client upload (modelled per client, A4)", "",
            f"Each client uploads once: 1 × RTT + report bytes / {CLIENT_UPLINK_BPS / 1e6:g} Mbit/s "
            f"client uplink. Report bytes = measured upload bytes (both servers) / N. "
            f"`rounds.upload` as implemented (batches sent one after another) is in section 3; the "
            f"model does not use it, and upload is never added to online time.", ""]
    out.append(md_table(
        kh + ["Report bytes per client", "LAN client upload (ms)", "WAN client upload (ms)"],
        [key_cols(r, multi) + [fnum(r["bytes_upload_per_client_median"], 1),
                               frange(r, "client_upload_lan_ms", 3),
                               frange(r, "client_upload_wan_ms", 3)] for r in rows],
        ka + ["r"] * 3))

    out += ["", "## 3. Communication (as measured)", "",
            "Bytes are the application-level size of each message as actually sent (bincode varint "
            "for Mastic), per server; framing is not counted. Mastic's two servers never talk to "
            "each other: the driver relays everything, so server ↔ server is 0 (B3). A system that "
            "records server ↔ server per direction (A9 spec v3.1) shows the sum of both directions "
            "here; the network model (section 2) uses the larger direction.", ""]
    out.append(md_table(
        kh + ["Rounds upload (impl.)", "Rounds online", "Rounds extra",
              "Upload client→server per server (MB)", "Online collector→server per server (kB)",
              "Online server→collector per server (MB)", "Server↔server (B)", "Online total (MB)"],
        [key_cols(r, multi) + [
            frange(r, "rounds_upload", 0), frange(r, "rounds_online", 0),
            frange(r, "rounds_extra", 0),
            fpair(r, "bytes_upload_client_to_server", lambda v: fnum(None if v is None else v / 1e6, 3)),
            fpair(r, "bytes_online_collector_to_server",
                  lambda v: fnum(None if v is None else v / 1e3, 2)),
            fpair(r, "bytes_online_server_to_collector",
                  lambda v: fnum(None if v is None else v / 1e6, 3)),
            fint(r["bytes_online_server_to_server_median"]),
            fnum(None if r["bytes_online_total_median"] is None
                 else r["bytes_online_total_median"] / 1e6, 3)] for r in rows],
        ka + ["r"] * 8))

    out += ["", "## 4. Client cost (A6)", "",
            "One thread, the first k rows of `day_3of3.csv`, after one warm-up pass; one run per "
            "encoding. Report size = serialized report per server as sent.", ""]
    seen, body = set(), []
    for r in rows:
        if (r["system"], r["encoding"]) in seen:
            continue
        seen.add((r["system"], r["encoding"]))
        s0, s1 = r["client_report_bytes_s0"], r["client_report_bytes_s1"]
        body.append(([r["system"]] if multi else []) + [
            f"`{r['encoding']}`", fint(r["client_k"]), fnum(r["client_keygen_ms_mean"], 4),
            fnum(r["client_keygen_ms_median"], 4), fnum(r["client_keygen_ms_p95"], 4),
            fint(s0) if s0 == s1 else f"{fint(s0)} / {fint(s1)}",
            fint(r["client_upload_bytes_per_client"])])
    out.append(md_table((["System"] if multi else []) + [
        "Encoding", "k", "Mean (ms)", "Median (ms)", "p95 (ms)", "Report per server (B)",
        "Upload per client (B)"], body, (["l"] if multi else []) + ["l"] + ["r"] * 6))

    out += ["", "## 5. Accuracy (A5)", "",
            "Self-check: each run against the same system's plaintext answer for the same "
            "encoding, subset and τ (minimum over the measured runs; expected 1.0 / 1.0 / exact). "
            f"Reference: Mosaic's qualifying squares Q (neighbourhood count ≥ T) at τ = "
            f"{bc.REFERENCE_TAU:g} and δ = {bc.REFERENCE_DELTA}, grouped into areas of squares that "
            "touch, including diagonally; every run of every τ is measured against it, on the "
            "Mosaic grid. Area recall = share of reference areas with a reported location within "
            "L∞ ≤ δ of one of the area's squares; precision = share of reported locations within "
            "L∞ ≤ δ of a square in Q; F1 = 2PR / (P + R). Counts are not compared: an in-cell count "
            "and a neighbourhood count measure different things. Outputs = reported locations; "
            "outputs without coordinates (not real cells, A9 spec v3.1) are counted separately and "
            "take no part in the accuracy. Exact is — when the system reveals no counts (A9 spec "
            "v3.1); the self-check then compares the reported locations only.", ""]
    out.append(md_table(
        kh + ["T", "Outputs", "Outputs without coordinates", "Self-check P", "Self-check R",
              "Exact", "Areas found / reference", "Area recall", "Precision", "F1",
              "Reference squares (Q)"],
        [key_cols(r, multi) + [
            fint(r["threshold_abs"]), frange(r, "outputs", 0), frange(r, "outputs_no_coords", 0),
            fnum(r["selfcheck_precision_min"], 3), fnum(r["selfcheck_recall_min"], 3),
            {True: "yes", False: "**NO**"}.get(r["selfcheck_exact"], DASH),
            DASH if r["reference_areas"] is None
            else f"{frange(r, 'areas_found', 0)} / {fint(r['reference_areas'])}",
            frange(r, "area_recall", 3), frange(r, "precision", 3), frange(r, "f1", 3),
            fint(r["reference_squares"])] for r in rows],
        ka + ["r"] * 11))
    if info.get("references"):
        out += ["", "Reference files:", ""] + [f"- {x}" for x in info["references"]]

    out += ["", "## 6. Leakage counters (A7)", "",
            "Revealed = aggregate values the collector reconstructs during the run; kept = Σ "
            "`frontier_per_level` (prefixes that survive); pruned = revealed − kept; validity bits "
            "= per-client accept/reject results revealed (2N for Mastic: FLP decision + final-proof "
            "comparison).", ""]
    out.append(md_table(
        kh + ["Revealed", "Kind", "Kept", "Pruned", "Final frontier", "Validity bits",
              "Validity bits / N"],
        [key_cols(r, multi) + [
            frange(r, "revealed_aggregates_total", 0), r["revealed_kind"] or DASH,
            frange(r, "revealed_kept_total", 0), frange(r, "revealed_pruned_total", 0),
            frange(r, "frontier_final", 0), frange(r, "client_validity_bits", 0),
            fnum(r["client_validity_bits_median"] / r["n_clients"], 2)
            if isinstance(r["client_validity_bits_median"], float)
            and isinstance(r["n_clients"], float) and r["n_clients"] else DASH] for r in rows],
        ka + ["r", "l"] + ["r"] * 5))

    if info.get("checks"):
        out += ["", "## 7. Sanity checks (B3)", "",
                "One line per check over all encoding × subset rows; the expected and measured "
                "values are listed once each, in row order.", ""]
        by_check = {}
        for c, w, e, g, ok in info["checks"]:
            by_check.setdefault(c, []).append((w, e, g, ok))

        def fval(v):
            if not isinstance(v, (int, float)):
                return str(v)
            return f"{v:,.3f}" if isinstance(v, float) and v != int(v) else f"{int(v):,}"

        body = []
        for c, items in by_check.items():
            n_ok = sum(ok for *_, ok in items)
            failed = [w for w, _, _, ok in items if not ok]
            body.append([c, f"{n_ok} of {len(items)}",
                         " / ".join(dict.fromkeys(fval(e) for _, e, _, _ in items)),
                         " / ".join(dict.fromkeys(fval(g) for _, _, g, _ in items)),
                         "pass" if not failed else f"**FAIL**: {', '.join(failed)}"])
        out.append(md_table(["Check", "Rows passing", "Expected", "Measured", "Status"], body,
                            ["l", "r", "r", "r", "l"]))

    if info.get("drift"):
        out += ["", "## 8. Repetition drift", "",
                "Online time of each repetition relative to its configuration's median over the "
                "measured runs (the loop runs repetitions outermost, A8). A steady rise means the "
                "machine slowed down during the benchmark (e.g. thermal throttling).", ""]
        out.append(md_table(
            ["Rep", "Warm-up", "Runs", "Median ratio", "Min ratio", "Max ratio"],
            [[d["rep"], "yes" if d["warmup"] else "no", d["runs"], fnum(d["median"], 3),
              fnum(d["min"], 3), fnum(d["max"], 3)] for d in info["drift"]],
            ["r", "l", "r", "r", "r", "r"]))

    extra = info.get("extra") or {}
    with_extra = [r for r in rows if extra.get(row_key(r))]
    if with_extra:
        keys = list(dict.fromkeys(k for r in with_extra for k in extra[row_key(r)]))
        out += ["", "## 9. System-specific values (A9 `extra`)", "",
                "The values of each run's `extra` object (A9 spec v3.1), which have no field or "
                "summary column of their own: numbers as median [min–max] over the measured runs, "
                "other values as their distinct values. They are not in the summary CSV, so "
                "`--combine` does not compare them.", ""]
        out.append(md_table(
            kh + [f"`{k}`" for k in keys],
            [key_cols(r, multi) + [extra[row_key(r)].get(k, DASH) for k in keys]
             for r in with_extra],
            ka + ["r"] * len(keys)))
    return "\n".join(out) + "\n"


def drift_table(groups):
    """Per repetition index: online_ms / the configuration's median over its measured runs."""
    by_rep = defaultdict(list)
    warm = {}
    for runs in groups.values():
        med = summarize([r["line"]["phases"]["online_ms"] for r in runs
                         if not r["line"]["warmup"]])[0]
        if not med:
            continue
        for r in runs:
            v = r["line"]["phases"]["online_ms"]
            if v is not None:
                by_rep[r["line"]["rep"]].append(v / med)
                warm[r["line"]["rep"]] = r["line"]["warmup"]
    return [{"rep": rep, "warmup": warm[rep], "runs": len(v),
             **dict(zip(STATS, summarize(v)))} for rep, v in sorted(by_rep.items())]


# --- comparison.md (--combine) -----------------------------------------------------------------

COMPARE_GROUPS = (
    ("Timings (localhost, ms)", (("online_ms", "Online", 1), ("upload_ms", "Upload", 1),
                                 ("server_offline_ms", "Server offline", 1))),
    ("Modelled online time (A4, ms)", (("online_est_lan_ms", "LAN online est.", 1),
                                       ("online_est_wan_ms", "WAN online est.", 1))),
    ("Rounds", (("rounds_online", "Online", 0), ("rounds_extra", "Extra", 0),
                ("rounds_upload", "Upload (impl.)", 0))),
    ("Bytes", (("bytes_upload_per_client", "Upload per client (B)", 0),
               ("bytes_online_total", "Online total (B)", 0),
               ("bytes_online_max_link", "Online busiest link (B)", 0))),
    ("Accuracy against the reference (A5)", (("area_recall", "Area recall", 3),
                                            ("precision", "Precision", 3), ("f1", "F1", 3),
                                            ("areas_found", "Areas found", 0),
                                            ("outputs", "Outputs", 0),
                                            ("outputs_no_coords", "Outputs without coordinates",
                                             0))),
    ("Leakage (A7)", (("revealed_aggregates_total", "Revealed aggregates", 0),
                      ("client_validity_bits", "Validity bits", 0))),
)
COMPARE_CLIENT = (("client_keygen_ms_median", "Report generation, median (ms)", 4),
                  ("client_keygen_ms_p95", "p95 (ms)", 4),
                  ("client_upload_bytes_per_client", "Upload per client (B)", 0))


def series_of(rows, systems=None):
    """(system, tau) pairs present in rows: systems in the given order, tau ascending."""
    systems = systems or list(dict.fromkeys(r["system"] for r in rows))
    present = {(r["system"], r["threshold_frac"]) for r in rows}
    return [(s, t) for s in systems
            for t in sorted({t for s2, t in present if s2 == s}, key=tau_key)]


def series_label(series):
    return f"{series[0]} τ={ftau(series[1])}"


def parameter_mismatches(rows):
    """(encoding, subset, field, {series label: value}) where the common parameters differ (A2):
    N and delta per encoding x subset across every system and tau; T per encoding x subset x tau
    across systems; T against max(1, floor(tau * N)) in every row; and an encoding x subset that
    only one system has. Different taus are expected (each system runs at its own tau)."""
    by_key = defaultdict(dict)
    for r in rows:
        by_key[(r["encoding"], r["subset"])][(r["system"], r["threshold_frac"])] = r
    out = []
    for (enc, sub), per in sorted(by_key.items()):
        if len({s for s, _ in per}) < 2:
            out.append((enc, sub, "missing", {series_label(k): "present" for k in per}))
        for field in ("n_clients", "delta"):
            values = {series_label(k): r[field] for k, r in per.items()}
            if len(set(values.values())) > 1:
                out.append((enc, sub, field, values))
        for tau in sorted({t for _, t in per}, key=tau_key):
            same = {k: r for k, r in per.items() if k[1] == tau}
            values = {series_label(k): r["threshold_abs"] for k, r in same.items()}
            if len(same) > 1 and len(set(values.values())) > 1:
                out.append((enc, sub, f"threshold_abs at τ={ftau(tau)}", values))
        for k, r in per.items():
            n, t, tau = r["n_clients"], r["threshold_abs"], r["threshold_frac"]
            if _is_num(n) and _is_num(t) and _is_num(tau) and t != bc.threshold_abs(n, tau):
                out.append((enc, sub, "threshold_abs != max(1, floor(τ·N))",
                            {series_label(k): f"T={fint(t)}, expected "
                                              f"{bc.threshold_abs(n, tau)}"}))
    return out


# The baseline of every ratio in comparison.md: Mastic at its own tau (0.005). The main comparison
# is Mastic at 0.005 vs Mosaic at its native 0.02 (A2).
BASELINE_SERIES = ("mastic", bc.THRESHOLDS["mastic"])


def baseline_first(series, system="mastic", tau=None):
    """series with the baseline first: (system, tau) if present (tau None = A2's tau of that
    system), else the first series. Returns (baseline, ordered series)."""
    if not series:
        return None, []
    want = (system, bc.THRESHOLDS.get(system) if tau is None else tau)
    base = want if want in series else series[0]
    return base, [base] + [s for s in series if s != base]


def compare_tables(by_key, keys, series, level="##"):
    """The COMPARE_GROUPS tables for `series` (baseline first): one column per series and metric,
    then one ratio column per later series, divided by the baseline and labelled with both."""
    base, out = series[0], []
    for title, metrics in COMPARE_GROUPS:
        header = ["Encoding", "Subset", "N"]
        for _, label, _ in metrics:
            header += [f"{label} ({series_label(s)})" for s in series]
            header += [f"{label} ratio {series_label(s)} / {series_label(base)}"
                       for s in series[1:]]
        body = []
        for k in keys:
            per = by_key[k]
            if not any(s in per for s in series):
                continue
            n = next(per[s] for s in series if s in per)["n_clients"]
            cells = [f"`{k[0]}`", subset_label(k[1]), fint(n)]
            for metric, _, digits in metrics:
                vals = [per[s][f"{metric}_median"] if s in per else None for s in series]
                cells += [fnum(v, digits) for v in vals]
                a = vals[0]
                cells += [fnum(b / a, 2) if isinstance(a, float) and isinstance(b, float) and a
                          else DASH for b in vals[1:]]
            body.append(cells)
        out += [f"{level} {title}", "",
                md_table(header, body, ["l", "l"] + ["r"] * (len(header) - 2)), ""]
    return out


def comparison_md(rows, systems, sources):
    rows = row_order(rows, systems)
    base, series = baseline_first(series_of(rows, systems), *BASELINE_SERIES)
    by_key = defaultdict(dict)
    for r in rows:
        by_key[(r["encoding"], r["subset"])][(r["system"], r["threshold_frac"])] = r
    keys = sorted(by_key, key=lambda k: (list(bc.ENCODINGS).index(k[0]) if k[0] in bc.ENCODINGS
                                         else 99, k[0], list(bc.SUBSETS).index(k[1])
                                         if k[1] in bc.SUBSETS else 99))
    # Taus at which two or more systems ran: a same-tau section each.
    shared = [t for t in sorted({t for _, t in series}, key=tau_key)
              if len({s for s, t2 in series if t2 == t}) > 1]
    base_label = series_label(base) if base else ""
    out = [f"# Comparison: {' vs '.join(systems)}", "",
           "Generated by `bench/scripts/evaluate.py --combine`. Medians over the measured runs. "
           "One column per system and threshold τ: each system runs at its own τ (A2), so the "
           f"main comparison is Mastic at τ = {bc.THRESHOLDS['mastic']:g} vs Mosaic at τ = "
           f"{bc.THRESHOLDS['mosaic']:g}. Every ratio divides a column by the baseline, "
           f"{base_label}, and its label names both τ values. A ratio between different τ "
           "compares answers to different questions (A2), so it is only indicative.", ""]
    if len(shared) == 1:
        out += [f"The section *Same τ = {ftau(shared[0])}* at the end compares the systems at "
                "that one τ.", ""]
    elif shared:
        out += [f"The sections *Same τ* at the end compare the systems at one τ each "
                f"({', '.join(f'τ = {ftau(t)}' for t in shared)}).", ""]
    else:
        out += ["No τ is shared by two systems (Mosaic at τ = "
                f"{bc.THRESHOLDS['mastic']:g} is optional, A2), so there is no same-τ table.", ""]
    out += [f"- `{s}`: {src}" for s, src in sources.items()]
    out += [f"- Series: {', '.join(series_label(s) for s in series)}", ""]
    mismatches = parameter_mismatches(rows)
    if mismatches:
        out += ["> **Common parameters differ (A2) — the comparison is not valid for these rows:**"]
        out += [f"> - `{e}` {subset_label(s)}: {f} {v}" for e, s, f, v in mismatches]
    else:
        out += ["> Common parameters are consistent: N and δ are equal for every encoding × subset, "
                "T is equal wherever two systems ran the same τ, and T = max(1, ⌊τ·N⌋) in every "
                "row."]
    out.append("")
    out += compare_tables(by_key, keys, series)

    # Client cost does not depend on tau: one column per system, the baseline's system first.
    client_systems = list(dict.fromkeys(([base[0]] if base else []) + list(systems)))
    header = ["Encoding"]
    for _, label, _ in COMPARE_CLIENT:
        header += [f"{label} ({s})" for s in client_systems]
    body = []
    for enc in dict.fromkeys(k[0] for k in keys):
        per = {}
        for s in client_systems:
            per[s] = next((r for r in rows if r["system"] == s and r["encoding"] == enc), None)
        cells = [f"`{enc}`"]
        for metric, _, digits in COMPARE_CLIENT:
            cells += [fnum(per[s][metric] if per[s] else None, digits) for s in client_systems]
        body.append(cells)
    out += ["## Client cost (A6)", "", md_table(header, body, ["l"] + ["r"] * (len(header) - 1)), ""]

    for tau in shared:
        _, same = baseline_first([s for s in series if s[1] == tau], BASELINE_SERIES[0], tau)
        out += [f"## Same τ = {ftau(tau)}: {' vs '.join(series_label(s) for s in same)}", "",
                f"Every column here is at the same τ = {ftau(tau)}, so the systems answer at the "
                f"same threshold (A2). Ratios divide by {series_label(same[0])}.", ""]
        out += compare_tables(by_key, keys, same, level="###")
    return "\n".join(out)


# --- Plots (A10) -------------------------------------------------------------------------------

# One colour per encoding, in the fixed order of bench_common.ENCODINGS (reference categorical
# palette, slots 1-4; adjacent CVD separation checked with the dataviz validator). One marker per
# system. A series is one system at one tau: the lowest tau has solid lines and filled markers,
# higher taus dashed/dotted lines and hollow markers.
ENCODING_COLORS = dict(zip(bc.ENCODINGS, ("#2a78d6", "#eb6834", "#1baf7a", "#eda100")))
OTHER_COLOR = "#8a8984"
SYSTEM_MARKERS = {"mastic": "o", "mosaic": "s"}
SPARE_MARKERS = ("^", "D", "v", "P")
TAU_LINESTYLES = ("-", "--", ":", "-.")
INK, INK_2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#ffffff"


def _plt():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({
        "font.size": 9, "axes.titlesize": 10, "axes.titlelocation": "left",
        "axes.titlepad": 10, "axes.labelsize": 9, "axes.labelcolor": INK_2,
        "axes.edgecolor": GRID, "axes.linewidth": 0.8, "axes.spines.top": False,
        "axes.spines.right": False, "axes.grid": True, "axes.grid.axis": "y",
        "axes.axisbelow": True, "grid.color": GRID, "grid.linewidth": 0.8, "grid.linestyle": "-",
        "xtick.color": INK_2, "ytick.color": INK_2, "xtick.major.size": 0, "ytick.major.size": 0,
        "legend.frameon": False, "legend.fontsize": 8, "text.color": INK,
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "pdf.fonttype": 42,
    })
    return plt


def _markers(systems):
    spare = iter(SPARE_MARKERS)
    return {s: SYSTEM_MARKERS.get(s) or next(spare, "o") for s in systems}


def _styles(series, systems):
    """{(system, tau): marker, line style, hollow}; (system, None) = the system at any tau."""
    markers = _markers(systems)
    taus = sorted({t for _, t in series}, key=tau_key)
    styles = {(s, None): {"marker": markers[s], "ls": "-", "hollow": False} for s in systems}
    for s, t in series:
        i = taus.index(t)
        styles[(s, t)] = {"marker": markers[s], "ls": TAU_LINESTYLES[i % len(TAU_LINESTYLES)],
                          "hollow": i > 0}
    return styles


def _marker_kw(style, color, ms=6):
    """Matplotlib marker arguments: filled with a white edge, or hollow with a coloured edge."""
    if style["hollow"]:
        return {"marker": style["marker"], "ms": ms, "mfc": SURFACE, "mec": color, "mew": 1.5}
    return {"marker": style["marker"], "ms": ms, "mfc": color, "mec": SURFACE, "mew": 1.2}


def _in_series(row, series):
    return row["system"] == series[0] and (series[1] is None or row["threshold_frac"] == series[1])


def _encodings(rows):
    present = list(dict.fromkeys(r["encoding"] for r in rows))
    return [e for e in bc.ENCODINGS if e in present] + [e for e in present if e not in bc.ENCODINGS]


def _thousands(ax, axis="y", scale=1.0, digits=0):
    from matplotlib.ticker import FuncFormatter

    fmt = FuncFormatter(lambda v, _: f"{v / scale:,.{digits}f}")
    (ax.yaxis if axis == "y" else ax.xaxis).set_major_formatter(fmt)


def _legend(fig, encodings, series, styles):
    """Encodings by colour; systems by marker if there are several; taus by line style and
    marker fill if there are several."""
    from matplotlib.lines import Line2D

    handles = [Line2D([], [], color=ENCODING_COLORS.get(e, OTHER_COLOR), lw=2, label=e)
               for e in encodings]
    systems = list(dict.fromkeys(s for s, _ in series))
    taus = sorted({t for _, t in series}, key=tau_key)
    if len(systems) > 1:
        handles += [Line2D([], [], color=INK_2, lw=0, label=s,
                           **_marker_kw(styles[(s, None)], INK_2)) for s in systems]
    if len(taus) > 1:
        for t in taus:
            st = next(styles[k] for k in series if k[1] == t)
            handles.append(Line2D([], [], color=INK_2, lw=1.5, ls=st["ls"], label=f"τ = {ftau(t)}",
                                  **_marker_kw({**st, "marker": "o"}, INK_2)))
    fig.legend(handles=handles, loc="lower center", ncol=len(handles), frameon=False,
               bbox_to_anchor=(0.5, 0.0))


def _save(fig, out_dir, name):
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for ext in ("png", "pdf"):
        path = out_dir / f"{name}.{ext}"
        meta = {"CreationDate": None} if ext == "pdf" else {}
        fig.savefig(path, dpi=200, metadata=meta, bbox_inches="tight")
        paths.append(path)
    import matplotlib.pyplot as plt

    plt.close(fig)
    return paths


def _vs_n(ax, rows, metric, series, styles, scale=1.0):
    """One line per series (system x tau) x encoding: median vs N, with min-max error bars."""
    encodings = _encodings(rows)
    ns = sorted({r["n_clients"] for r in rows if isinstance(r["n_clients"], float)})
    k = len(encodings) * len(series)
    for si, ser in enumerate(series):
        for ei, enc in enumerate(encodings):
            pts = sorted((r["n_clients"], r[f"{metric}_median"], r[f"{metric}_min"],
                          r[f"{metric}_max"]) for r in rows
                         if _in_series(r, ser) and r["encoding"] == enc
                         and isinstance(r["n_clients"], float) and r[f"{metric}_median"] is not None)
            if not pts:
                continue
            j = si * len(encodings) + ei
            off = (j - (k - 1) / 2) * 0.008  # small x offset so error bars don't overlap
            xs = [n * (1 + off) for n, *_ in pts]
            med = [m / scale for _, m, _, _ in pts]
            # min/max may be missing or inconsistent in a foreign summary: never a negative bar.
            lo = [max(0.0, m - (m if a is None else a)) / scale for _, m, a, _ in pts]
            hi = [max(0.0, (m if b is None else b) - m) / scale for _, m, _, b in pts]
            color = ENCODING_COLORS.get(enc, OTHER_COLOR)
            ax.errorbar(xs, med, yerr=[lo, hi], color=color, ls=styles[ser]["ls"], lw=1.5,
                        elinewidth=1.0, capsize=0, solid_capstyle="round",
                        **_marker_kw(styles[ser], color))
    ax.set_xticks(ns)
    ax.set_xticklabels([f"{int(n):,}" for n in ns])
    if ns:
        ax.set_xlim(ns[0] * 0.85, ns[-1] * 1.08)
    ax.set_xlabel("Clients N")
    ax.set_ylim(bottom=0)


def _items(rows, series, by_subset=True):
    """x positions for a dot plot: groups = encodings, items = series x subset within a group."""
    encodings = _encodings(rows)
    subsets = [s for s in bc.SUBSETS if any(r["subset"] == s for r in rows)] if by_subset else [None]
    combos = [(ser, sub) for ser in series for sub in subsets]
    width = 0.7
    step = width / max(len(combos), 1)
    items = []
    for gi, enc in enumerate(encodings):
        for ci, (ser, sub) in enumerate(combos):
            x = gi - width / 2 + step * (ci + 0.5)
            row = next((r for r in rows if _in_series(r, ser) and r["encoding"] == enc
                        and (sub is None or r["subset"] == sub)), None)
            items.append({"x": x, "enc": enc, "series": ser, "subset": sub, "row": row,
                          "last": ci == len(combos) - 1})
    return encodings, items


def _dots(ax, rows, series, styles, value, by_subset=True, label_last=True, digits=0,
          whisker=None):
    """Stem-and-dot plot per encoding; value(row) -> y, whisker(row) -> (lo, hi) or None.
    series: (system, tau) pairs, or (system, None) for values that do not depend on tau."""
    encodings, items = _items(rows, series, by_subset)
    ymax = 0.0
    for it in items:
        if it["row"] is None:
            continue
        y = value(it["row"])
        if y is None:
            continue
        c = ENCODING_COLORS.get(it["enc"], OTHER_COLOR)
        ax.vlines(it["x"], 0, y, color=c, lw=1.5, alpha=0.45)
        if whisker:
            w = whisker(it["row"])
            if w:
                ax.vlines(it["x"], w[0], w[1], color=c, lw=1.5)
                ymax = max(ymax, w[1])
        ax.plot([it["x"]], [y], color=c, lw=0, **_marker_kw(styles[it["series"]], c, ms=7))
        ymax = max(ymax, y)
        if label_last and it["last"]:
            # Beside the dot when a whisker rises above it, on top otherwise.
            offset, ha, va = ((7, 0), "left", "center") if whisker else ((0, 6), "center", "bottom")
            ax.annotate(f"{y:,.{digits}f}", (it["x"], y), xytext=offset,
                        textcoords="offset points", ha=ha, va=va, fontsize=7.5, color=INK_2)
    ax.set_xticks(range(len(encodings)))
    ax.set_xticklabels(encodings)
    ax.tick_params(axis="x", which="major", pad=14 if by_subset else 4)
    if by_subset:
        ax.xaxis.remove_overlapping_locs = False  # keep the middle subset's label under the group
        per_group = len(items) // max(len(encodings), 1)
        compact = {"1of3": "⅓", "2of3": "⅔", "3of3": "1"}
        labels = [compact.get(it["subset"], subset_label(it["subset"])) if per_group > 3
                  else subset_label(it["subset"]) for it in items]
        ax.set_xticks([it["x"] for it in items], minor=True)
        ax.set_xticklabels(labels, minor=True, fontsize=7)
        ax.tick_params(axis="x", which="minor", length=0, colors=INK_2)
    ax.set_xlim(-0.6, len(encodings) - 0.4)
    ax.set_ylim(0, ymax * 1.18 if ymax else 1)
    ax.grid(axis="x", visible=False)
    return ymax


def make_plots(rows, out_dir, systems):
    """The six plots of A10 as PNG + PDF in out_dir; returns the files written. One colour per
    encoding, one marker per system; several taus are told apart by line style and marker fill."""
    plt = _plt()
    rows = row_order(rows, systems)
    series = series_of(rows, systems)
    styles = _styles(series, systems)
    by_system = [(s, None) for s in systems]  # client metrics do not depend on tau
    taus = sorted({t for _, t in series}, key=tau_key)
    encodings = _encodings(rows)
    tag = " vs ".join(systems)
    written = []

    def fig_with(ncols, title, width=10.0, height=4.2, nrows=1):
        fig, axes = plt.subplots(nrows, ncols, figsize=(width, height * nrows), squeeze=False)
        fig.suptitle(title, x=0.01, ha="left", fontsize=11, color=INK)
        return fig, (axes[0] if nrows == 1 else axes)

    def finish(fig, name, nrows=1):
        _legend(fig, encodings, series, styles)
        fig.tight_layout(rect=(0, 0.07 / nrows, 1, 1))
        written.extend(_save(fig, out_dir, name))

    # 1. Online time vs N, LAN and WAN panels.
    fig, axes = fig_with(2, f"Online time vs N, modelled ({tag}): measured online + t_comm(online)")
    for ax, p in zip(axes, PROFILES):
        _vs_n(ax, rows, f"online_est_{p.name.lower()}_ms", series, styles, scale=1e3)
        ax.set_title(f"{p.name}: RTT {p.rtt_ms:g} ms, "
                     + (f"{p.bandwidth_bps / 1e9:g} Gbit/s" if p.bandwidth_bps >= 1e9
                        else f"{p.bandwidth_bps / 1e6:g} Mbit/s"))
        ax.set_ylabel("Online time (s), median, bars = min–max")
    finish(fig, "1_online_vs_n")

    # 2. Bytes vs N.
    fig, axes = fig_with(2, f"Bytes vs N ({tag}), as actually sent")
    for ax, (metric, title) in zip(axes, (("bytes_upload_total", "Upload: clients → servers, all servers"),
                                          ("bytes_online_total", "Online: all links"))):
        _vs_n(ax, rows, metric, series, styles, scale=1e6)
        ax.set_title(title)
        ax.set_ylabel("MB (10⁶ bytes)")
    finish(fig, "2_bytes_vs_n")

    # 3. Rounds per encoding.
    fig, axes = fig_with(2, f"Rounds per encoding ({tag})")
    _dots(axes[0], rows, series, styles,
          lambda r: None if r["rounds_online_median"] is None
          else r["rounds_online_median"] + (r["rounds_extra_median"] or 0))
    axes[0].set_title("Online rounds (online + extra)")
    axes[0].set_ylabel("Rounds")
    _dots(axes[1], rows, series, styles, lambda r: r["rounds_upload_median"])
    axes[1].axhline(1, color=INK_2, lw=0.8)
    axes[1].set_title("Upload rounds as implemented (batches);\ngrey line = model, 1 round per client")
    axes[1].set_ylabel("Rounds")
    finish(fig, "3_rounds")

    # 4. Client time and report size per encoding.
    fig, axes = fig_with(2, f"Client report generation per encoding ({tag}), one thread")
    _dots(axes[0], rows, by_system, styles, lambda r: r["client_keygen_ms_median"],
          by_subset=False, digits=4,
          whisker=lambda r: (r["client_keygen_ms_median"], r["client_keygen_ms_p95"])
          if r["client_keygen_ms_p95"] is not None else None)
    axes[0].set_title("Time per client: median, line up to p95")
    axes[0].set_ylabel("ms")
    _dots(axes[1], rows, by_system, styles, lambda r: r["client_upload_bytes_per_client"],
          by_subset=False)
    axes[1].set_title("Upload per client (all servers' report shares)")
    axes[1].set_ylabel("Bytes")
    _thousands(axes[1])
    finish(fig, "4_client")

    # 5. Area recall and precision per encoding, one row of panels per tau.
    has_ref = any(r["area_recall_median"] is not None or r["precision_median"] is not None
                  for r in rows)
    if has_ref:
        title = (f"Accuracy against the reference ({tag}): Mosaic's qualifying squares at "
                 f"τ = {bc.REFERENCE_TAU:g}, δ = {bc.REFERENCE_DELTA}; hit = within L∞ ≤ δ")
        panels = (("area_recall_median", "Area recall"), ("precision_median", "Precision"))
    else:
        title = (f"Self-check against the plaintext answer ({tag}); reference not available yet")
        panels = (("selfcheck_recall_min", "Self-check recall (min over runs)"),
                  ("selfcheck_precision_min", "Self-check precision (min over runs)"))
    nrows = max(len(taus), 1)
    fig, grid = fig_with(2, title, height=4.2 if nrows == 1 else 3.6, nrows=nrows)
    for i, tau in enumerate(taus or [None]):
        axes_row = grid if nrows == 1 else grid[i]
        at_tau = [r for r in rows if tau is None or r["threshold_frac"] == tau]
        for ax, (col, label) in zip(axes_row, panels):
            _dots(ax, at_tau, [s for s in series if s[1] == tau], styles,
                  lambda r, c=col: r[c], label_last=False)
            ax.set_ylim(0, 1.1)
            ax.set_title(f"{label}, runs at τ = {ftau(tau)}")
    finish(fig, "5_accuracy", nrows)

    # 6. Revealed aggregates vs N.
    fig, axes = fig_with(1, f"Revealed aggregates vs N ({tag})", width=6.0)
    _vs_n(axes[0], rows, "revealed_aggregates_total", series, styles)
    axes[0].set_title("Aggregate values the collector reconstructs per run")
    axes[0].set_ylabel("Revealed aggregates")
    _thousands(axes[0])
    finish(fig, "6_revealed_vs_n")
    return written


# --- Main --------------------------------------------------------------------------------------


def banner(lines, out=None):
    out = out or sys.stderr  # looked up at call time (tests capture stderr)
    bar = "!" * 100
    print(bar, file=out)
    for line in lines:
        print(f"!! {line}", file=out)
    print(bar, file=out)


def evaluate(raw, reference_dir=REFERENCE_DIR):
    """Self-check and metrics for every run; summary rows per system, one per encoding x subset x
    tau. Raises ValueError (ReferenceFormatError) for a reference or ground truth that does not fit.

    The self-check uses the ground truth at the run's own tau; the accuracy uses the one reference
    per subset (Mosaic's squares at tau = 0.02), for runs of every tau. Each reference must have
    A5's setting for the N of its subset's runs (check_reference_setting)."""
    gt_cache, ref_cache, notes = {}, {}, []
    groups = defaultdict(list)
    n_by_subset = defaultdict(set)
    for r in raw["runs"]:
        n_by_subset[(r["line"]["dataset"], r["line"]["subset"])].add(r["line"]["n_clients"])
    for r in raw["runs"]:
        line = r["line"]
        tau = line["params"]["threshold_frac"]
        gt_key, ref_key = (line["dataset"], line["subset"], tau), (line["dataset"], line["subset"])
        if gt_key not in gt_cache:
            gt_cache[gt_key] = load_ground_truth(*gt_key)
        if ref_key not in ref_cache:
            ref_path = Path(reference_dir) / f"{ref_key[0]}_{ref_key[1]}.json"
            ref = load_reference(ref_path, *ref_key) if ref_path.exists() else None
            if ref is not None:
                check_reference_setting(ref, n_by_subset[ref_key])
            ref_cache[ref_key] = ref
        gt = gt_cache[gt_key]
        truth = gt["encodings"].get(line["encoding"]) if gt else None
        reference = ref_cache[ref_key]
        r["check"] = self_check(line, truth)
        r["metrics"] = run_metrics(line, reference)
        groups[(line["system"], line["dataset"], line["encoding"], line["subset"], tau)].append(r)

    clients = {}
    for c in raw["clients"]:
        k = (c["line"]["system"], c["line"]["encoding"])
        if k in clients:
            notes.append(f"several client lines for {k[0]} {k[1]}; using the last one "
                         f"({c['where']})")
        clients[k] = c["line"]

    rows = defaultdict(list)
    fields = {"n_clients": lambda x: x["n_clients"],
              "threshold_abs": lambda x: x["params"]["threshold_abs"],
              "commit": lambda x: x["env"]["commit"], "dirty": lambda x: x["env"]["dirty"],
              "threads": lambda x: x["env"]["threads"], "cpu": lambda x: x["env"]["cpu"]}
    for (system, dataset, enc, subset, tau), runs in groups.items():
        row = summary_row(runs, clients.get((system, enc)), ref_cache[(dataset, subset)])
        where = f"{system} {enc} {subset} τ={ftau(tau)}"
        for field, get in fields.items():
            distinct = list(dict.fromkeys(get(r["line"]) for r in runs if not r["line"]["warmup"]))
            if len(distinct) > 1:
                notes.append(f"{where}: measured runs disagree on {field}: {distinct}")
        if row["reps_measured"] != 5:
            notes.append(f"{where}: {row['reps_measured']} measured runs (A2 expects 5)")
        rows[system].append(row)
    for system in clients.keys() - {(s, e) for s, _, e, _, _ in groups}:
        notes.append(f"client line for {system[0]} {system[1]} but no runs")
    return groups, rows, gt_cache, ref_cache, notes


def reference_descriptions(ref_cache):
    """A one-line description of each reference (its setting was checked when it was loaded)."""
    described = []
    for (d, s), ref in sorted(ref_cache.items()):
        if ref is None:
            continue
        peaks ="; ".join(f"area {a['id']}: {a['size']} squares, peak {list(a['peak'])} "
                          f"({a['peak_count']:,})" for a in ref["areas"][:6])
        more = f"; … {len(ref['areas']) - 6} more" if len(ref["areas"]) > 6 else ""
        described.append(f"`{bc.relpath(ref['path'])}`: τ {ref['tau']:g}, T {ref['T']:,}, "
                         f"δ {ref['delta']}; |Q| = {len(ref['squares']):,} squares in "
                         f"{len(ref['areas'])} areas ({peaks}{more})")
    return described


def safe_console():
    """Never fail on a console or pipe that cannot encode τ, δ, ≥ … (on Windows, redirected output
    is cp1252): such characters are written as \\u escapes instead."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass


def main(argv=None):
    safe_console()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--combine", metavar="CSV",
                        help="another system's summary_<system>.csv: write comparison.md and "
                             "plots/comparison/")
    parser.add_argument("--no-plots", action="store_true", help="skip the plots")
    parser.add_argument("--reference-dir", type=Path, default=REFERENCE_DIR,
                        help=f"where <dataset>_<subset>.json references are read (default "
                             f"{bc.relpath(REFERENCE_DIR)})")
    parser.add_argument("--out-dir", type=Path, default=RESULTS_DIR,
                        help=f"where the summary, tables.md, plots/ and comparison go (default "
                             f"{bc.relpath(RESULTS_DIR)})")
    args = parser.parse_args(argv)
    reference_dir = args.reference_dir.resolve()
    out_dir = args.out_dir.resolve()
    plots_dir, tables_path = out_dir / "plots", out_dir / "tables.md"
    comparison_path, comparison_plots = out_dir / "comparison.md", out_dir / "plots" / "comparison"

    raw = load_raw()
    if not raw["runs"]:
        print(f"No benchmark runs in {bc.relpath(RAW_DIR)}/*.jsonl", file=sys.stderr)
        return 2
    try:
        groups, rows_by_system, gt_cache, ref_cache, notes = evaluate(raw, reference_dir)
    except ValueError as e:
        kind = "reference" if isinstance(e, ReferenceFormatError) else "ground truth"
        banner([f"Cannot use the {kind}: {e}",
                f"Expected: A5 of bench/BENCHMARK_MASTIC.md (its format, tau = "
                f"{bc.REFERENCE_TAU:g}, delta = {bc.REFERENCE_DELTA}, T = max(1, floor("
                f"{bc.REFERENCE_TAU:g} N))). Nothing was written."
                if kind == "reference" else "Regenerate it with ground_truth.py. Nothing was written."])
        return 2
    runs = raw["runs"]
    failed = [r for r in runs if not r["check"]["ok"]]
    measured = [r for r in runs if not r["line"]["warmup"]]

    # Inputs and status banners (also written into tables.md).
    inputs = []
    for path in raw["files"]:
        n_lines = sum(1 for t in path.read_text(encoding="utf-8").splitlines() if t.strip())
        inputs.append(f"`{bc.relpath(path)}`: {n_lines} lines")
    inputs.append(f"Runs used: {len(runs)} ({len(measured)} measured, summarised; "
                  f"{len(runs) - len(measured)} warm-ups, self-checked but excluded); smoke lines "
                  f"skipped: {raw['smoke']}; lines failing the schema: {len(raw['problems'])}.")
    envs = {(r["line"]["env"]["cpu"], r["line"]["env"]["threads"], r["line"]["env"]["commit"],
             r["line"]["env"]["dirty"]) for r in runs}
    for cpu, threads, commit, dirty in sorted(envs, key=str):
        inputs.append(f"Environment: {cpu}, {threads} threads per server, commit "
                      f"`{(commit or '?')[:7]}`, dirty {str(dirty).lower()}.")
    loaded = [f"`{bc.relpath(gt['path'])}` (τ {ftau(k[2])})"
              for k, gt in sorted(gt_cache.items(), key=lambda kv: (tau_key(kv[0][2]), kv[0][:2]))
              if gt]
    inputs.append(f"Ground truth: {', '.join(loaded) or 'none'}.")

    banners = []
    if failed:
        lines = [f"SELF-CHECK FAILED for {len(failed)} of {len(runs)} runs (A5): the benchmark is "
                 "blocked until this is fixed."]
        for r in failed:
            c = r["check"]
            exact = {True: "yes", False: "NO", None: "not revealed"}[c["exact"]]
            lines.append(f"{run_label(r['line'])}: precision {fnum(c['precision'], 3)}, recall "
                         f"{fnum(c['recall'], 3)}, exact {exact}; "
                         f"{'; '.join(c.get('problems', []))} [{r['where']}]")
        banner(lines)
        banners.append("> **⚠ SELF-CHECK FAILED — the benchmark is blocked (A5).**")
        banners += [f"> - {x}" for x in lines[1:]]
    else:
        no_counts = sum(r["check"]["exact"] is None for r in runs)
        counts = ("every count exact" if not no_counts else
                  f"every revealed count exact ({no_counts} runs reveal no counts: locations "
                  f"compared only)")
        msg = (f"Self-check: PASS. All {len(runs)} runs ({len(measured)} measured + "
               f"{len(runs) - len(measured)} warm-ups) equal the plaintext ground truth: precision "
               f"1.0, recall 1.0, {counts}, centres on the same Mosaic-grid cells.")
        print(msg)
        banners.append(f"> **{msg}**")
    if raw["problems"]:
        banner([f"{len(raw['problems'])} result lines fail the A9 schema and were skipped:"]
               + raw["problems"][:20])
        banners += ["", f"> **⚠ {len(raw['problems'])} lines fail the A9 schema and were skipped.**"]
        banners += [f"> - {p}" for p in raw["problems"][:20]]

    missing_ref = sorted(k for k, v in ref_cache.items() if v is None)
    empty_cols = "area recall, precision, F1 and areas found"
    if not reference_dir.exists():
        msg = (f"Accuracy against the reference: not available. `{bc.relpath(reference_dir)}/` "
               f"does not exist, so {empty_cols} are left empty (A5, B4). Rerun once the Mosaic "
               f"harness has written `{bc.relpath(reference_dir)}/<dataset>_<subset>.json` "
               f"(A5 format).")
    elif missing_ref:
        msg = (f"Accuracy against the reference: missing for "
               f"{', '.join(f'{d}_{s}' for d, s in missing_ref)} in "
               f"`{bc.relpath(reference_dir)}/`; those rows have empty {empty_cols}.")
    else:
        msg = (f"Accuracy against the reference: computed from "
               f"{', '.join(f'`{bc.relpath(v['path'])}`' for _, v in sorted(ref_cache.items()))} "
               f"for runs of every τ (Mosaic's qualifying squares; hit = within L∞ ≤ δ on the "
               f"Mosaic grid).")
    print(msg)
    banners += ["", f"> {msg}"]
    ref_described = reference_descriptions(ref_cache)

    if notes:
        banners += [""] + [f"> Note: {n}" for n in notes]
        for n in notes:
            print(f"Note: {n}", file=sys.stderr)

    # Summary CSVs, read back so that tables and plots use exactly what is in the files.
    all_rows, summaries = [], {}
    for system, rows in sorted(rows_by_system.items()):
        path = out_dir / f"summary_{system}.csv"
        write_summary(row_order(rows), path)
        summaries[system] = path
        all_rows += read_summary(path)
        print(f"Wrote {bc.relpath(path)} ({len(rows)} rows, {len(SUMMARY_COLUMNS)} columns)")

    checks = mastic_checks(all_rows)
    bad_checks = [c for c in checks if not c[4]]
    if bad_checks:
        banner([f"{len(bad_checks)} B3 sanity checks failed:"]
               + [f"{c}: {w}: expected {e}, got {g}" for c, w, e, g, _ in bad_checks])
    elif checks:
        print(f"B3 sanity checks: {len(checks)} of {len(checks)} pass")
    info = {"inputs": inputs, "banners": banners, "checks": checks, "drift": drift_table(groups),
            "references": ref_described,
            "extra": {key: extra_values(runs) for key, runs in groups.items()}}
    tables_path.write_text(tables_md(all_rows, info), encoding="utf-8", newline="\n")
    print(f"Wrote {bc.relpath(tables_path)}")
    systems = list(summaries)
    if not args.no_plots:
        files = make_plots(all_rows, plots_dir, systems)
        print(f"Wrote {len(files)} plot files to {bc.relpath(plots_dir)}/")

    if args.combine:
        other_path = Path(args.combine)
        try:
            other = read_summary(other_path)
        except (OSError, ValueError, StopIteration) as e:
            print(f"--combine: {e}", file=sys.stderr)
            return 2
        other_systems = list(dict.fromkeys(r["system"] for r in other))
        if set(other_systems) & set(systems):
            print(f"--combine: {bc.relpath(other_path)} holds system(s) {other_systems}, which "
                  f"are already in bench/results/raw/", file=sys.stderr)
            return 2
        combined_systems = systems + other_systems
        combined = all_rows + other
        duplicates = duplicate_keys(combined)
        if duplicates:
            print(f"--combine: {len(duplicates)} rows appear more than once (system, dataset, "
                  f"encoding, subset, τ): {duplicates[:5]}", file=sys.stderr)
            return 2
        sources = {s: f"`{bc.relpath(p)}`" for s, p in summaries.items()}
        sources.update({s: f"`{bc.relpath(other_path)}`" for s in other_systems})
        for e, s, f, v in parameter_mismatches(combined):
            print(f"--combine: {e} {s}: {f} differs: {v}", file=sys.stderr)
        comparison_path.write_text(comparison_md(combined, combined_systems, sources) + "\n",
                                   encoding="utf-8", newline="\n")
        print(f"Wrote {bc.relpath(comparison_path)}")
        if not args.no_plots:
            files = make_plots(combined, comparison_plots, combined_systems)
            print(f"Wrote {len(files)} plot files to {bc.relpath(comparison_plots)}/")

    return 1 if failed or raw["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
