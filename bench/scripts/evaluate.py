#!/usr/bin/env python3
"""Evaluate the benchmark results (bench/BENCHMARK_MASTIC.md: A4, A5, A7, A10, B4).

    .\\.venv\\Scripts\\python.exe bench\\scripts\\evaluate.py
    .\\.venv\\Scripts\\python.exe bench\\scripts\\evaluate.py --combine bench\\results\\summary_mosaic.csv

Reads only bench/results/raw/*.jsonl (<system>.jsonl: one line per run; <system>_client.jsonl:
the client micro-benchmark), the plaintext ground truth bench/ground_truth/<dataset>_<subset>.json
and, if it exists, the reference bench/reference/<dataset>_<subset>.json. Smoke lines (subset
"smoke") are skipped. Warm-ups are left out of the summary, but every run, warm-ups included, is
self-checked.

Writes:
- bench/results/summary_<system>.csv: one row per encoding x subset, columns SUMMARY_COLUMNS
  (identical for both systems, so the two files can be concatenated);
- bench/results/tables.md: timings, communication, client cost, accuracy, leakage;
- bench/results/plots/<k>_<name>.png and .pdf: the six plots of A10.
With --combine FILE (the other system's summary, same columns):
- bench/results/comparison.md and bench/results/plots/comparison/ (one marker per system).

The tables and plots are made from the summary CSV as read back from disk, so --combine uses
exactly the same code on both systems' rows.

Exit code 1 if any run fails the self-check or the schema check (the outputs are still written),
2 if the input cannot be used at all.
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
import bench_common as bc  # noqa: E402
import run_bench  # noqa: E402  (self_check and the batch sizes: one definition for runner and evaluation)

RAW_DIR = bc.BENCH / "results" / "raw"
RESULTS_DIR = bc.BENCH / "results"
PLOTS_DIR = RESULTS_DIR / "plots"
COMPARISON_PLOTS_DIR = PLOTS_DIR / "comparison"
TABLES_MD = RESULTS_DIR / "tables.md"
COMPARISON_MD = RESULTS_DIR / "comparison.md"
REFERENCE_DIR = bc.BENCH / "reference"
SMOKE_SUBSET = run_bench.SMOKE_SUBSET  # "smoke"

# --- Summary columns (A10) ---------------------------------------------------------------------
#
# The one definition of the summary's columns. The Mosaic harness must write exactly these
# columns, in this order, so that `--combine` can put the two systems side by side.
# A metric with _median/_min/_max columns is summarised over the measured repetitions (warm-ups
# excluded). Per-server values are _s0/_s1. Empty cell = not applicable / not available.
SUMMARY_COLUMNS = (
    # identity and setup
    "system", "encoding", "dataset", "subset", "n_clients", "threshold_abs", "data_bits", "delta",
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
    # accuracy (A5): outputs, self-check against the same system's plaintext answer, and the
    # metrics against the reference hotspots (empty until bench/reference/ exists)
    "outputs_median", "outputs_min", "outputs_max",
    "selfcheck_precision_median", "selfcheck_precision_min", "selfcheck_precision_max",
    "selfcheck_recall_median", "selfcheck_recall_min", "selfcheck_recall_max",
    "recall_median", "recall_min", "recall_max",
    "precision_median", "precision_min", "precision_max",
    "f1_median", "f1_min", "f1_max",
    "count_error_median", "count_error_min", "count_error_max",
    # single values per row
    "selfcheck_exact", "selfcheck_failed_runs", "reference_hotspots",
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


def online_link_bytes(online):
    """The online links of A4 (collector->server and server->collector per server, server<->server),
    with null fields counted as 0."""
    c2s = per_server(online["collector_to_server"])
    s2c = per_server(online["server_to_collector"])
    return [v or 0 for v in (*c2s, *s2c, online["server_to_server"])]


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


def reported_points(output):
    """[(grid, count)] of a run's reported cells (A5: cell centre -> grid)."""
    return [(to_grid(c["lat"], c["lon"]), c["count"]) for c in output]


def match_reference(reported, reference, delta):
    """A5 metrics of reported [(grid, count)] against reference [(grid, count or None)].

    recall    = share of reference hotspots with a reported location within L-inf <= delta;
    precision = share of reported locations within L-inf <= delta of some reference hotspot;
    count error = median |reported - reference| / reference over matched pairs: each reference
    hotspot that has a reported location within delta is paired with its nearest one (L-inf,
    then Euclidean, then the larger count, then report order).
    """
    hits = [any(linf(g, h) <= delta for g, _ in reported) for h, _ in reference]
    recall = sum(hits) / len(hits) if reference else None
    precision = (sum(any(linf(g, h) <= delta for h, _ in reference) for g, _ in reported)
                 / len(reported) if reported else None)
    pairs, errors = [], []
    for (h, ref_count), hit in zip(reference, hits):
        if not hit:
            continue
        i = min(range(len(reported)), key=lambda i: (
            linf(reported[i][0], h),
            (reported[i][0][0] - h[0]) ** 2 + (reported[i][0][1] - h[1]) ** 2,
            -reported[i][1], i))
        pairs.append((h, reported[i][0]))
        if ref_count:
            errors.append(abs(reported[i][1] - ref_count) / ref_count)
    return {
        "recall": recall,
        "precision": precision,
        "f1": f1_score(precision, recall),
        "outputs": len(reported),
        "count_error": statistics.median(errors) if errors else None,
        "pairs": pairs,
    }


def self_check(line, truth):
    """A5 self-check of one run against the same encoding's plaintext ground truth.

    run_bench.self_check (cells matched by label, every count exact, centres equal), plus: the
    Mosaic-grid coordinates of every matched centre equal the ground truth's, and N and T equal the
    ground truth's. `ok` is the overall verdict.
    """
    if truth is None:
        return {"precision": None, "recall": None, "exact": False, "ok": False,
                "problems": ["no ground truth for this encoding and subset"]}
    check = run_bench.self_check(line["output"], truth)
    want = {c["label"]: tuple(c["grid"]) for c in truth["cells"]}
    problems = []
    if any(to_grid(c["lat"], c["lon"]) != want[c["label"]] for c in line["output"]
           if c["label"] in want):
        problems.append("grid coordinates differ")
    if line["n_clients"] != truth["N"]:
        problems.append(f"n_clients {line['n_clients']} != N {truth['N']}")
    if line["params"]["threshold_abs"] != truth["T"]:
        problems.append(f"threshold {line['params']['threshold_abs']} != T {truth['T']}")
    if not check["ok"]:
        problems.append("cells, counts or centres differ")
    return {**check, "ok": check["ok"] and not problems, "problems": problems}


def load_reference(path):
    """Reference hotspots as [(grid, count or None)].

    The Mosaic harness defines the file. Accepted: a list, or an object holding the list under
    "hotspots", "cells", "points" or "reference"; items [lat_grid, lon_grid(, count)] or objects
    with "grid": [lat_grid, lon_grid], or "lat_grid"/"lon_grid", or "lat"/"lon" in degrees
    (converted with Mosaic's grid formula), and optionally "count". Anything else is an error.
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    meta = {}
    if isinstance(data, dict):
        meta = {k: v for k, v in data.items() if not isinstance(v, list)}
        keys = [k for k in ("hotspots", "cells", "points", "reference") if k in data]
        if not keys:
            raise ValueError(f"{path}: no hotspot list (expected one of hotspots/cells/points)")
        data = data[keys[0]]
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a list of hotspots")
    hotspots = []
    for i, item in enumerate(data):
        if isinstance(item, (list, tuple)) and len(item) in (2, 3):
            grid, count = (int(item[0]), int(item[1])), (item[2] if len(item) == 3 else None)
        elif isinstance(item, dict) and "grid" in item:
            grid, count = (int(item["grid"][0]), int(item["grid"][1])), item.get("count")
        elif isinstance(item, dict) and {"lat_grid", "lon_grid"} <= item.keys():
            grid, count = (int(item["lat_grid"]), int(item["lon_grid"])), item.get("count")
        elif isinstance(item, dict) and {"lat", "lon"} <= item.keys():
            grid, count = to_grid(float(item["lat"]), float(item["lon"])), item.get("count")
        else:
            raise ValueError(f"{path}: hotspot {i} has an unknown format: {item!r}")
        hotspots.append((grid, count))
    return {"hotspots": hotspots, "meta": meta, "path": path}


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


def run_metrics(line, reference=None, delta=bc.DELTA):
    """The per-run metrics of RUN_METRICS, except the self-check ones."""
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
        "bytes_online_server_to_server": b["online"]["server_to_server"],
        "bytes_online_total": sum(links),
        "bytes_online_max_link": max(links),
        **leakage_metrics(line["leakage"]),
        "outputs": len(line["output"]),
    }
    for p in PROFILES:
        key = p.name.lower()
        m[f"online_comm_{key}_ms"] = comm_ms(model_rounds, links, p)
        m[f"online_est_{key}_ms"] = (None if phases["online_ms"] is None
                                     else online_est_ms(phases["online_ms"], model_rounds, links, p))
        m[f"client_upload_{key}_ms"] = None if per_client is None else client_upload_ms(per_client, p)
    acc = (match_reference(reported_points(line["output"]), reference["hotspots"], delta)
           if reference is not None else {})
    for k in ("recall", "precision", "f1", "count_error"):
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


def load_ground_truth(dataset, subset):
    path = bc.GROUND_TRUTH_DIR / f"{dataset}_{subset}.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


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


def summary_row(runs, client=None, reference=None, delta=bc.DELTA):
    """One summary row for the runs of one system x encoding x subset.

    runs: [{"line", "metrics", "check"}]; warm-ups are counted but excluded from every statistic."""
    measured = [r for r in runs if not r["line"]["warmup"]]
    lines = [r["line"] for r in measured] or [r["line"] for r in runs]
    first = lines[0]
    row = dict.fromkeys(SUMMARY_COLUMNS)
    row.update({
        "system": first["system"],
        "encoding": first["encoding"],
        "dataset": first["dataset"],
        "subset": first["subset"],
        "n_clients": joined([x["n_clients"] for x in lines]),
        "threshold_abs": joined([x["params"]["threshold_abs"] for x in lines]),
        "data_bits": joined([x["params"]["data_bits"] for x in lines]),
        "delta": delta,
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
    row["selfcheck_exact"] = bool(measured) and all(r["check"]["exact"] for r in measured)
    row["selfcheck_failed_runs"] = sum(not r["check"]["ok"] for r in measured)
    row["reference_hotspots"] = None if reference is None else len(reference["hotspots"])
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


def row_order(rows, systems=None):
    systems = systems or list(dict.fromkeys(r["system"] for r in rows))
    enc, sub = list(bc.ENCODINGS), list(bc.SUBSETS)

    def key(r):
        return (systems.index(r["system"]) if r["system"] in systems else len(systems),
                enc.index(r["encoding"]) if r["encoding"] in enc else len(enc), r["encoding"],
                sub.index(r["subset"]) if r["subset"] in sub else len(sub), r["subset"])

    return sorted(rows, key=key)


# --- Checks specific to Mastic (B3, A7 decisions) ----------------------------------------------


def mastic_checks(rows):
    """B3 sanity checks on the summary rows of the Mastic system: [(check, row, expected, got, ok)]."""
    batch = run_bench.BASE_CONFIG["add_report_share_batch_size"]
    flp_batch = run_bench.BASE_CONFIG["query_flp_batch_size"]
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
    return cols + [f"`{r['encoding']}`", subset_label(r["subset"]), fint(r["n_clients"])]


def key_header(multi):
    return (["System"] if multi else []) + ["Encoding", "Subset", "N"]


def tables_md(rows, info):
    rows = row_order(rows)
    systems = list(dict.fromkeys(r["system"] for r in rows))
    multi = len(systems) > 1
    kh = key_header(multi)
    ka = ["l"] * len(kh[:-1]) + ["r"]
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
            "each other: the driver relays everything, so server ↔ server is 0 (B3).", ""]
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
            "Self-check: each run against the same system's plaintext answer for the same encoding "
            "and subset (minimum over the measured runs; expected 1.0 / 1.0 / exact). Reference: "
            "Mosaic's plaintext fuzzy hotspots on the Mosaic grid, with recall/precision within "
            f"L∞ ≤ δ = {bc.DELTA} grid cells and the count error over matched pairs.", ""]
    out.append(md_table(
        kh + ["T", "Outputs", "Self-check P", "Self-check R", "Exact", "Reference hotspots",
              "Recall", "Precision", "F1", "Count error"],
        [key_cols(r, multi) + [
            fint(r["threshold_abs"]), frange(r, "outputs", 0),
            fnum(r["selfcheck_precision_min"], 3), fnum(r["selfcheck_recall_min"], 3),
            {True: "yes", False: "**NO**"}.get(r["selfcheck_exact"], DASH),
            fint(r["reference_hotspots"]), frange(r, "recall", 3), frange(r, "precision", 3),
            frange(r, "f1", 3), frange(r, "count_error", 3)] for r in rows],
        ka + ["r"] * 10))

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
    ("Accuracy against the reference (A5)", (("recall", "Recall", 3), ("precision", "Precision", 3),
                                            ("f1", "F1", 3), ("count_error", "Count error", 3),
                                            ("outputs", "Outputs", 0))),
    ("Leakage (A7)", (("revealed_aggregates_total", "Revealed aggregates", 0),
                      ("client_validity_bits", "Validity bits", 0))),
)
COMPARE_CLIENT = (("client_keygen_ms_median", "Report generation, median (ms)", 4),
                  ("client_keygen_ms_p95", "p95 (ms)", 4),
                  ("client_upload_bytes_per_client", "Upload per client (B)", 0))


def parameter_mismatches(rows):
    """(encoding, subset, field, {system: value}) where the common parameters differ."""
    by_key = defaultdict(dict)
    for r in rows:
        by_key[(r["encoding"], r["subset"])][r["system"]] = r
    out = []
    for (enc, sub), per in sorted(by_key.items()):
        if len(per) < 2:
            out.append((enc, sub, "missing", {s: "present" for s in per}))
            continue
        for field in ("n_clients", "threshold_abs", "delta"):
            values = {s: r[field] for s, r in per.items()}
            if len(set(values.values())) > 1:
                out.append((enc, sub, field, values))
    return out


def comparison_md(rows, systems, sources):
    rows = row_order(rows, systems)
    by_key = defaultdict(dict)
    for r in rows:
        by_key[(r["encoding"], r["subset"])][r["system"]] = r
    keys = sorted(by_key, key=lambda k: (list(bc.ENCODINGS).index(k[0]) if k[0] in bc.ENCODINGS
                                         else 99, k[0], list(bc.SUBSETS).index(k[1])
                                         if k[1] in bc.SUBSETS else 99))
    out = [f"# Comparison: {' vs '.join(systems)}", "",
           "Generated by `bench/scripts/evaluate.py --combine`. Medians over the measured runs; the "
           "last column of each metric is the ratio of the second system to the first.", ""]
    out += [f"- `{s}`: {src}" for s, src in sources.items()] + [""]
    mismatches = parameter_mismatches(rows)
    if mismatches:
        out += ["> **Common parameters differ (A2) — the comparison is not valid for these rows:**"]
        out += [f"> - `{e}` {subset_label(s)}: {f} {v}" for e, s, f, v in mismatches]
    else:
        out += ["> Common parameters (N, T, δ) are equal for every encoding × subset."]
    out.append("")
    for title, metrics in COMPARE_GROUPS:
        header = ["Encoding", "Subset", "N"]
        for _, label, _ in metrics:
            header += [f"{label} ({s})" for s in systems] + [f"{label} ratio"]
        body = []
        for k in keys:
            per = by_key[k]
            n = next(iter(per.values()))["n_clients"]
            cells = [f"`{k[0]}`", subset_label(k[1]), fint(n)]
            for metric, _, digits in metrics:
                vals = [per[s][f"{metric}_median"] if s in per else None for s in systems]
                cells += [fnum(v, digits) for v in vals]
                a, b = vals[0], vals[-1]
                cells.append(fnum(b / a, 2) if isinstance(a, float) and isinstance(b, float) and a
                             else DASH)
            body.append(cells)
        out += [f"## {title}", "", md_table(header, body, ["l", "l"] + ["r"] * (len(header) - 2)),
                ""]
    header = ["Encoding"]
    for _, label, _ in COMPARE_CLIENT:
        header += [f"{label} ({s})" for s in systems]
    body = []
    for enc in dict.fromkeys(k[0] for k in keys):
        per = {}
        for s in systems:
            per[s] = next((r for r in rows if r["system"] == s and r["encoding"] == enc), None)
        cells = [f"`{enc}`"]
        for metric, _, digits in COMPARE_CLIENT:
            cells += [fnum(per[s][metric] if per[s] else None, digits) for s in systems]
        body.append(cells)
    out += ["## Client cost (A6)", "", md_table(header, body, ["l"] + ["r"] * (len(header) - 1)), ""]
    return "\n".join(out)


# --- Plots (A10) -------------------------------------------------------------------------------

# One colour per encoding, in the fixed order of bench_common.ENCODINGS (reference categorical
# palette, slots 1-4; adjacent CVD separation checked with the dataviz validator). One marker per
# system.
ENCODING_COLORS = dict(zip(bc.ENCODINGS, ("#2a78d6", "#eb6834", "#1baf7a", "#eda100")))
OTHER_COLOR = "#8a8984"
SYSTEM_MARKERS = {"mastic": "o", "mosaic": "s"}
SPARE_MARKERS = ("^", "D", "v", "P")
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


def _encodings(rows):
    present = list(dict.fromkeys(r["encoding"] for r in rows))
    return [e for e in bc.ENCODINGS if e in present] + [e for e in present if e not in bc.ENCODINGS]


def _thousands(ax, axis="y", scale=1.0, digits=0):
    from matplotlib.ticker import FuncFormatter

    fmt = FuncFormatter(lambda v, _: f"{v / scale:,.{digits}f}")
    (ax.yaxis if axis == "y" else ax.xaxis).set_major_formatter(fmt)


def _legend(fig, encodings, systems, markers):
    from matplotlib.lines import Line2D

    handles = [Line2D([], [], color=ENCODING_COLORS.get(e, OTHER_COLOR), lw=2, label=e)
               for e in encodings]
    if len(systems) > 1:
        handles += [Line2D([], [], color=INK_2, lw=0, marker=markers[s], ms=6, label=s)
                    for s in systems]
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


def _vs_n(ax, rows, metric, systems, markers, scale=1.0):
    """One line per system x encoding: median vs N, with min-max error bars."""
    encodings = _encodings(rows)
    ns = sorted({r["n_clients"] for r in rows if isinstance(r["n_clients"], float)})
    k = len(encodings) * len(systems)
    for si, system in enumerate(systems):
        for ei, enc in enumerate(encodings):
            pts = sorted((r["n_clients"], r[f"{metric}_median"], r[f"{metric}_min"],
                          r[f"{metric}_max"]) for r in rows
                         if r["system"] == system and r["encoding"] == enc
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
            ax.errorbar(xs, med, yerr=[lo, hi], color=ENCODING_COLORS.get(enc, OTHER_COLOR),
                        marker=markers[system], ms=6, mec=SURFACE, mew=1.2, lw=1.5,
                        elinewidth=1.0, capsize=0, solid_capstyle="round")
    ax.set_xticks(ns)
    ax.set_xticklabels([f"{int(n):,}" for n in ns])
    if ns:
        ax.set_xlim(ns[0] * 0.85, ns[-1] * 1.08)
    ax.set_xlabel("Clients N")
    ax.set_ylim(bottom=0)


def _items(rows, systems, by_subset=True):
    """x positions for a dot plot: groups = encodings, items = system x subset within a group."""
    encodings = _encodings(rows)
    subsets = [s for s in bc.SUBSETS if any(r["subset"] == s for r in rows)] if by_subset else [None]
    combos = [(s, sub) for s in systems for sub in subsets]
    width = 0.7
    step = width / max(len(combos), 1)
    items = []
    for gi, enc in enumerate(encodings):
        for ci, (system, sub) in enumerate(combos):
            x = gi - width / 2 + step * (ci + 0.5)
            row = next((r for r in rows if r["system"] == system and r["encoding"] == enc
                        and (sub is None or r["subset"] == sub)), None)
            items.append({"x": x, "enc": enc, "system": system, "subset": sub, "row": row,
                          "last": ci == len(combos) - 1})
    return encodings, items


def _dots(ax, rows, systems, markers, value, by_subset=True, label_last=True, digits=0,
          whisker=None):
    """Stem-and-dot plot per encoding; value(row) -> y, whisker(row) -> (lo, hi) or None."""
    encodings, items = _items(rows, systems, by_subset)
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
        ax.plot([it["x"]], [y], marker=markers[it["system"]], color=c, ms=7, mec=SURFACE, mew=1.2,
                lw=0)
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
    """The six plots of A10 as PNG + PDF in out_dir; returns the files written."""
    plt = _plt()
    rows = row_order(rows, systems)
    markers = _markers(systems)
    encodings = _encodings(rows)
    tag = " vs ".join(systems)
    written = []

    def fig_with(ncols, title, width=10.0, height=4.2):
        fig, axes = plt.subplots(1, ncols, figsize=(width, height), squeeze=False)
        fig.suptitle(title, x=0.01, ha="left", fontsize=11, color=INK)
        return fig, axes[0]

    def finish(fig, name):
        _legend(fig, encodings, systems, markers)
        fig.tight_layout(rect=(0, 0.07, 1, 1))
        written.extend(_save(fig, out_dir, name))

    # 1. Online time vs N, LAN and WAN panels.
    fig, axes = fig_with(2, f"Online time vs N, modelled ({tag}): measured online + t_comm(online)")
    for ax, p in zip(axes, PROFILES):
        _vs_n(ax, rows, f"online_est_{p.name.lower()}_ms", systems, markers, scale=1e3)
        ax.set_title(f"{p.name}: RTT {p.rtt_ms:g} ms, "
                     + (f"{p.bandwidth_bps / 1e9:g} Gbit/s" if p.bandwidth_bps >= 1e9
                        else f"{p.bandwidth_bps / 1e6:g} Mbit/s"))
        ax.set_ylabel("Online time (s), median, bars = min–max")
    finish(fig, "1_online_vs_n")

    # 2. Bytes vs N.
    fig, axes = fig_with(2, f"Bytes vs N ({tag}), as actually sent")
    for ax, (metric, title) in zip(axes, (("bytes_upload_total", "Upload: clients → servers, all servers"),
                                          ("bytes_online_total", "Online: all links"))):
        _vs_n(ax, rows, metric, systems, markers, scale=1e6)
        ax.set_title(title)
        ax.set_ylabel("MB (10⁶ bytes)")
    finish(fig, "2_bytes_vs_n")

    # 3. Rounds per encoding.
    fig, axes = fig_with(2, f"Rounds per encoding ({tag})")
    _dots(axes[0], rows, systems, markers,
          lambda r: None if r["rounds_online_median"] is None
          else r["rounds_online_median"] + (r["rounds_extra_median"] or 0))
    axes[0].set_title("Online rounds (online + extra)")
    axes[0].set_ylabel("Rounds")
    _dots(axes[1], rows, systems, markers, lambda r: r["rounds_upload_median"])
    axes[1].axhline(1, color=INK_2, lw=0.8)
    axes[1].set_title("Upload rounds as implemented (batches);\ngrey line = model, 1 round per client")
    axes[1].set_ylabel("Rounds")
    finish(fig, "3_rounds")

    # 4. Client time and report size per encoding.
    fig, axes = fig_with(2, f"Client report generation per encoding ({tag}), one thread")
    _dots(axes[0], rows, systems, markers, lambda r: r["client_keygen_ms_median"],
          by_subset=False, digits=4,
          whisker=lambda r: (r["client_keygen_ms_median"], r["client_keygen_ms_p95"])
          if r["client_keygen_ms_p95"] is not None else None)
    axes[0].set_title("Time per client: median, line up to p95")
    axes[0].set_ylabel("ms")
    _dots(axes[1], rows, systems, markers, lambda r: r["client_upload_bytes_per_client"],
          by_subset=False)
    axes[1].set_title("Upload per client (all servers' report shares)")
    axes[1].set_ylabel("Bytes")
    _thousands(axes[1])
    finish(fig, "4_client")

    # 5. Recall and precision per encoding.
    has_ref = any(r["recall_median"] is not None for r in rows)
    if has_ref:
        title = f"Accuracy against the reference hotspots ({tag}), L∞ ≤ δ = {bc.DELTA}"
        panels = (("recall_median", "Recall"), ("precision_median", "Precision"))
    else:
        title = (f"Self-check against the plaintext answer ({tag}); reference hotspots not "
                 f"available yet")
        panels = (("selfcheck_recall_min", "Self-check recall (min over runs)"),
                  ("selfcheck_precision_min", "Self-check precision (min over runs)"))
    fig, axes = fig_with(2, title)
    for ax, (col, label) in zip(axes, panels):
        _dots(ax, rows, systems, markers, lambda r, c=col: r[c], label_last=False)
        ax.set_ylim(0, 1.1)
        ax.set_title(label)
    finish(fig, "5_accuracy")

    # 6. Revealed aggregates vs N.
    fig, axes = fig_with(1, f"Revealed aggregates vs N ({tag})", width=6.0)
    _vs_n(axes[0], rows, "revealed_aggregates_total", systems, markers)
    axes[0].set_title("Aggregate values the collector reconstructs per run")
    axes[0].set_ylabel("Revealed aggregates")
    _thousands(axes[0])
    finish(fig, "6_revealed_vs_n")
    return written


# --- Main --------------------------------------------------------------------------------------


def banner(lines, out=sys.stderr):
    bar = "!" * 100
    print(bar, file=out)
    for line in lines:
        print(f"!! {line}", file=out)
    print(bar, file=out)


def evaluate(raw):
    """Self-check and metrics for every run; summary rows per system."""
    gt_cache, ref_cache, notes = {}, {}, []
    groups = defaultdict(list)
    for r in raw["runs"]:
        line = r["line"]
        key = (line["dataset"], line["subset"])
        if key not in gt_cache:
            gt_cache[key] = load_ground_truth(*key)
            ref_path = REFERENCE_DIR / f"{key[0]}_{key[1]}.json"
            ref_cache[key] = load_reference(ref_path) if ref_path.exists() else None
        gt = gt_cache[key]
        truth = gt["encodings"].get(line["encoding"]) if gt else None
        reference = ref_cache[key]
        r["check"] = self_check(line, truth)
        r["metrics"] = run_metrics(line, reference)
        groups[(line["system"], line["dataset"], line["encoding"], line["subset"])].append(r)

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
    for (system, dataset, enc, subset), runs in groups.items():
        row = summary_row(runs, clients.get((system, enc)), ref_cache[(dataset, subset)])
        for field, get in fields.items():
            distinct = list(dict.fromkeys(get(r["line"]) for r in runs if not r["line"]["warmup"]))
            if len(distinct) > 1:
                notes.append(f"{system} {enc} {subset}: measured runs disagree on {field}: "
                             f"{distinct}")
        if row["reps_measured"] != 5:
            notes.append(f"{system} {enc} {subset}: {row['reps_measured']} measured runs "
                         f"(A2 expects 5)")
        rows[system].append(row)
    for system in clients.keys() - {(s, e) for s, _, e, _ in groups}:
        notes.append(f"client line for {system[0]} {system[1]} but no runs")
    return groups, rows, gt_cache, ref_cache, notes


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--combine", metavar="CSV",
                        help="another system's summary_<system>.csv: write comparison.md and "
                             "plots/comparison/")
    parser.add_argument("--no-plots", action="store_true", help="skip the plots")
    args = parser.parse_args()

    raw = load_raw()
    if not raw["runs"]:
        print(f"No benchmark runs in {bc.relpath(RAW_DIR)}/*.jsonl", file=sys.stderr)
        return 2
    groups, rows_by_system, gt_cache, ref_cache, notes = evaluate(raw)
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
    inputs.append(f"Ground truth: {', '.join(f'`bench/ground_truth/{d}_{s}.json`' for d, s in sorted(gt_cache) if gt_cache[(d, s)])}.")

    banners = []
    if failed:
        lines = [f"SELF-CHECK FAILED for {len(failed)} of {len(runs)} runs (A5): the benchmark is "
                 "blocked until this is fixed."]
        for r in failed:
            c = r["check"]
            lines.append(f"{run_label(r['line'])}: precision {fnum(c['precision'], 3)}, recall "
                         f"{fnum(c['recall'], 3)}, exact {'yes' if c['exact'] else 'NO'}; "
                         f"{'; '.join(c.get('problems', []))} [{r['where']}]")
        banner(lines)
        banners.append("> **⚠ SELF-CHECK FAILED — the benchmark is blocked (A5).**")
        banners += [f"> - {x}" for x in lines[1:]]
    else:
        msg = (f"Self-check: PASS. All {len(runs)} runs ({len(measured)} measured + "
               f"{len(runs) - len(measured)} warm-ups) equal the plaintext ground truth: precision "
               f"1.0, recall 1.0, every count exact, centres on the same Mosaic-grid cells.")
        print(msg)
        banners.append(f"> **{msg}**")
    if raw["problems"]:
        banner([f"{len(raw['problems'])} result lines fail the A9 schema and were skipped:"]
               + raw["problems"][:20])
        banners += ["", f"> **⚠ {len(raw['problems'])} lines fail the A9 schema and were skipped.**"]
        banners += [f"> - {p}" for p in raw["problems"][:20]]

    missing_ref = sorted(k for k, v in ref_cache.items() if v is None)
    if not REFERENCE_DIR.exists():
        msg = (f"Accuracy against the reference: not available. `{bc.relpath(REFERENCE_DIR)}/` "
               f"does not exist, so recall, precision, F1 and count error are left empty (A5, B4). "
               f"Rerun once the Mosaic harness has written "
               f"`{bc.relpath(REFERENCE_DIR)}/<dataset>_<subset>.json`.")
    elif missing_ref:
        msg = (f"Accuracy against the reference: missing for "
               f"{', '.join(f'{d}_{s}' for d, s in missing_ref)}; those rows have empty recall, "
               f"precision, F1 and count error.")
    else:
        msg = (f"Accuracy against the reference: computed from "
               f"{', '.join(f'`{bc.relpath(v['path'])}`' for v in ref_cache.values())} "
               f"(L∞ ≤ δ = {bc.DELTA} grid cells).")
    print(msg)
    banners += ["", f"> {msg}"]
    for (d, s), ref in ref_cache.items():
        if ref is None:
            continue
        t = ref["meta"].get("threshold_abs", ref["meta"].get("T"))
        delta = ref["meta"].get("delta")
        gt = gt_cache.get((d, s))
        want_t = next(iter(gt["encodings"].values()))["T"] if gt and gt["encodings"] else None
        if t is not None and want_t is not None and t != want_t:
            notes.append(f"reference {d}_{s}: T = {t}, ground truth T = {want_t}")
        if delta is not None and delta != bc.DELTA:
            notes.append(f"reference {d}_{s}: delta = {delta}, benchmark delta = {bc.DELTA}")

    if notes:
        banners += [""] + [f"> Note: {n}" for n in notes]
        for n in notes:
            print(f"Note: {n}", file=sys.stderr)

    # Summary CSVs, read back so that tables and plots use exactly what is in the files.
    all_rows, summaries = [], {}
    for system, rows in sorted(rows_by_system.items()):
        path = RESULTS_DIR / f"summary_{system}.csv"
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
    info = {"inputs": inputs, "banners": banners, "checks": checks, "drift": drift_table(groups)}
    TABLES_MD.write_text(tables_md(all_rows, info), encoding="utf-8", newline="\n")
    print(f"Wrote {bc.relpath(TABLES_MD)}")
    systems = list(summaries)
    if not args.no_plots:
        files = make_plots(all_rows, PLOTS_DIR, systems)
        print(f"Wrote {len(files)} plot files to {bc.relpath(PLOTS_DIR)}/")

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
        sources = {s: f"`{bc.relpath(p)}`" for s, p in summaries.items()}
        sources.update({s: f"`{bc.relpath(other_path)}`" for s in other_systems})
        for e, s, f, v in parameter_mismatches(combined):
            print(f"--combine: {e} {s}: {f} differs: {v}", file=sys.stderr)
        COMPARISON_MD.write_text(comparison_md(combined, combined_systems, sources) + "\n",
                                 encoding="utf-8", newline="\n")
        print(f"Wrote {bc.relpath(COMPARISON_MD)}")
        if not args.no_plots:
            files = make_plots(combined, COMPARISON_PLOTS_DIR, combined_systems)
            print(f"Wrote {len(files)} plot files to {bc.relpath(COMPARISON_PLOTS_DIR)}/")

    return 1 if failed or raw["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
