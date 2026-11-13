#!/usr/bin/env python3
"""Shared helpers for the Mastic benchmark (bench/BENCHMARK_MASTIC.md).

- The experiment's parameters in one place (A2): encodings, the per-system thresholds, seed, delta,
  the driver settings that are not encoding parameters (BASE_CONFIG) and the smoke subset's name.
- The plaintext encoders, imported from scripts/check_heavy_hitters.py (B4).
- The environment record (A8), the result schema check (A9, including the spec v3.1 additions) and
  a JSONL append helper.
- The self-check of a run's output against the plaintext answer (A5), shared by run_bench.py and
  evaluate.py, so that evaluate.py needs no other module of this harness.

    .\\.venv\\Scripts\\python.exe bench\\scripts\\bench_common.py              # environment, config check
    .\\.venv\\Scripts\\python.exe bench\\scripts\\bench_common.py --self-test  # schema check self-test
"""

import argparse
import copy
import csv
import datetime
import hashlib
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BENCH = ROOT / "bench"
DATA_DIR = BENCH / "data"
GROUND_TRUTH_DIR = BENCH / "ground_truth"
SOURCE_CSV = ROOT / "data" / "mosaic" / "sample_busiest_day.csv"

SYSTEM = "mastic"
SCHEMA_VERSION = 1
DATASET = "day"
SEED = 20261107
SUBSETS = ("1of3", "2of3", "3of3")
# Threshold fraction tau per system (A2): each system runs at its own native threshold.
# Mastic: in-cell counts, the existing day configs. Mosaic: neighbourhood counts, Mosaic's code and
# its results.json (optionally also run at Mastic's 0.005). T = max(1, floor(tau * N)).
THRESHOLDS = {"mastic": 0.005, "mosaic": 0.02}
THRESHOLD_FRAC = THRESHOLDS[SYSTEM]  # this codebase's tau: the default of every script
DELTA = 5  # Mosaic's radius in grid cells, L-infinity (data/mosaic/results.json)
# The reference hotspots (A5): Mosaic's qualifying squares at Mosaic's native tau and delta.
REFERENCE_TAU = THRESHOLDS["mosaic"]
REFERENCE_DELTA = DELTA
COORD_COLUMNS = ("start_location_lat", "start_location_long")

# Driver settings that are not encoding parameters; equal to src/configs/ride-austin-day-*.toml
# (run_bench.py checks this at start). evaluate.py's B3 checks use the two batch sizes.
BASE_CONFIG = {
    "hist_buckets": 1,
    "add_report_share_batch_size": 1000,
    "query_flp_batch_size": 50000,
    "zipf_unique_buckets": 1000,
    "zipf_exponent": 1.03,
}

# Smoke runs (run_bench.py --smoke) carry this dataset and subset; evaluate.py skips them.
SMOKE_DATASET, SMOKE_SUBSET = "synthetic", "smoke"

# Encoding parameters. `params` is the encoding table of a driver config ([dataset] encoding);
# `config` is the suffix of src/configs/ride-austin-day-<config>.toml. Geohash and H3 use the
# area-matched values of those configs (commit 34168c5), not A2's 20 bits / resolution 9: A2 and
# B3 are to be updated in both benchmark files.
ENCODINGS = {
    "mosaic_grid": {"data_bits": 22, "params": {}, "config": "mosaic-grid"},
    "geohash": {"data_bits": 21, "params": {"prefix": "9v6", "bits": 21}, "config": "geohash"},
    "h3": {
        "data_bits": 24,
        "params": {"prefix": "82489ffffffffff", "resolution": 10},
        "config": "h3",
    },
    "morton": {
        "data_bits": 20,
        "params": {
            "min_lat": 29.9005,
            "max_lat": 30.9245,
            "min_lon": -98.0505,
            "max_lon": -97.0265,
            "axis_bits": 10,
        },
        "config": "morton",
    },
}


def threshold_abs(n, tau):
    """T = max(1, floor(tau * n)), the driver's rule. tau is always explicit: the two systems run
    at different thresholds (A2)."""
    return max(1, int(tau * n))


def tau_tag(tau):
    """tau as it appears in file names: 0.02 -> "0.02", 0.005 -> "0.005"."""
    return f"{tau:g}"


def ground_truth_path(dataset, subset, tau):
    """bench/ground_truth/<dataset>_<subset>.json at Mastic's own tau (the files the benchmark was
    run against, unchanged), <dataset>_<subset>_tau<tau>.json at any other tau."""
    suffix = "" if tau == THRESHOLD_FRAC else f"_tau{tau_tag(tau)}"
    return GROUND_TRUTH_DIR / f"{dataset}_{subset}{suffix}.json"


# --- Plaintext encoders ----------------------------------------------------------------------


def checker_module():
    """scripts/check_heavy_hitters.py, imported without writing bytecode into the repo."""
    sys.dont_write_bytecode = True
    scripts = str(ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import check_heavy_hitters

    return check_heavy_hitters


def checker_encoder(name):
    """The plaintext encoder for one of ENCODINGS, with its parameters."""
    make = checker_module().make_encoder
    p = ENCODINGS[name]["params"]
    if name == "mosaic_grid":
        return make("mosaic_grid")
    if name == "geohash":
        return make("geohash", prefix=p["prefix"], bits=p["bits"])
    if name == "h3":
        return make("h3", prefix=p["prefix"], resolution=p["resolution"])
    if name == "morton":
        bbox = (p["min_lat"], p["max_lat"], p["min_lon"], p["max_lon"])
        return make("morton", bbox=bbox, axis_bits=p["axis_bits"])
    raise ValueError(f"unknown encoding {name!r}")


def config_encoding(name):
    """The [dataset] encoding value a driver config has for this encoding."""
    p = ENCODINGS[name]["params"]
    return {name: dict(p)} if p else name


def compare_configs(prefix="ride-austin-day"):
    """Differences between src/configs/<prefix>-<encoding>.toml and ENCODINGS / Mastic's tau."""
    results = []
    for name, spec in ENCODINGS.items():
        path = ROOT / "src" / "configs" / f"{prefix}-{spec['config']}.toml"
        diffs = []
        if not path.exists():
            diffs.append("file missing")
        else:
            cfg = tomllib.loads(path.read_text(encoding="utf-8"))
            threshold = cfg.get("mode", {}).get("weighted_heavy_hitters", {}).get("threshold")
            dataset = cfg.get("dataset", {})
            checks = [
                ("data_bits", cfg.get("data_bits"), spec["data_bits"]),
                ("threshold", threshold, THRESHOLD_FRAC),
                ("encoding", dataset.get("encoding"), config_encoding(name)),
                ("weight", dataset.get("weight", "constant"), "constant"),
            ]
            diffs = [f"{key} = {got!r}, harness uses {want!r}" for key, got, want in checks
                     if got != want]
        results.append({"encoding": name, "file": relpath(path), "differences": diffs})
    return results


# --- Files -------------------------------------------------------------------------------------


def relpath(path):
    """Path relative to the repo, with forward slashes (for manifests and reports)."""
    path = Path(path).resolve()
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def read_points(path):
    """(lat, lon) of every row of a ride CSV."""
    points = []
    with open(path, newline="", encoding="utf-8") as f:
        for line, row in enumerate(csv.DictReader(f), start=2):
            try:
                points.append((float(row[COORD_COLUMNS[0]]), float(row[COORD_COLUMNS[1]])))
            except (KeyError, TypeError, ValueError) as e:
                raise ValueError(f"{path}:{line}: bad coordinates ({e})") from None
    return points


def load_manifest():
    with open(DATA_DIR / "manifest.json", encoding="utf-8") as f:
        return json.load(f)


def write_json(path, obj, indent=1):
    """Write JSON with LF line endings."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=indent) + "\n", encoding="utf-8", newline="\n")


def timestamp():
    """Local time with UTC offset, e.g. 2026-11-10T14:03:11+01:00."""
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


# --- Environment (A8) --------------------------------------------------------------------------


def _cpu_name():
    if sys.platform == "win32":
        try:
            import winreg

            key = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
                return winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
        except OSError:
            pass
    elif sys.platform.startswith("linux"):
        try:
            with open("/proc/cpuinfo", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("model name"):
                        return line.split(":", 1)[1].strip()
        except OSError:
            pass
    elif sys.platform == "darwin":
        try:
            return subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                                  capture_output=True, text=True, check=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            pass
    return platform.processor() or "unknown"


def _run(*cmd):
    """stdout of a command, or None if it cannot run."""
    try:
        return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _rustc_version():
    exe = "rustc.exe" if sys.platform == "win32" else "rustc"
    for candidate in (shutil.which("rustc"), str(Path.home() / ".cargo" / "bin" / exe)):
        if candidate and Path(candidate).exists():
            version = _run(candidate, "--version")
            if version:
                return version
    return None


def server_threads(cores_physical):
    """Threads per server process: floor(physical cores / 2), at least 1 (A8)."""
    return max(1, cores_physical // 2)


def collect_env():
    """The `env` object of a result line (A8, A9)."""
    import psutil

    physical = psutil.cpu_count(logical=False) or os.cpu_count() or 1
    status = _run("git", "status", "--porcelain", "--untracked-files=no")
    return {
        "cpu": _cpu_name(),
        "cores_physical": physical,
        "cores_logical": psutil.cpu_count(logical=True) or physical,
        "ram_gb": round(psutil.virtual_memory().total / 2**30, 1),
        "os": platform.platform(),
        "threads": server_threads(physical),
        "compiler": _rustc_version(),
        "commit": _run("git", "rev-parse", "HEAD"),
        # Tracked changes only: generated, untracked bench output does not count.
        "dirty": None if status is None else bool(status),
    }


# --- Result schema (A9) ------------------------------------------------------------------------


class _Errors(list):
    def add(self, path, msg):
        self.append(f"{path}: {msg}")


def _type(kind, nullable=False):
    def check(errors, value, path):
        if value is None:
            if not nullable:
                errors.add(path, "must not be null")
            return
        is_number = isinstance(value, (int, float)) and not isinstance(value, bool)
        ok = {
            "int": isinstance(value, int) and not isinstance(value, bool),
            "num": is_number and math.isfinite(value),
            "str": isinstance(value, str),
            "bool": isinstance(value, bool),
            "dict": isinstance(value, dict),
        }[kind]
        if not ok:
            errors.add(path, f"expected {kind}, got {type(value).__name__} {value!r}")

    return check


def _enum(*values):
    def check(errors, value, path):
        if value not in values:
            errors.add(path, f"expected one of {list(values)}, got {value!r}")

    return check


def _iso_time(errors, value, path):
    try:
        datetime.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        errors.add(path, f"expected an ISO 8601 timestamp, got {value!r}")


def _pair(errors, value, path):
    """Per-server values: [server0, server1], or null if the field does not apply."""
    if value is None:
        return
    if not isinstance(value, list) or len(value) != 2:
        errors.add(path, f"expected [server0, server1], got {value!r}")
        return
    for i, v in enumerate(value):
        _type("num")(errors, v, f"{path}[{i}]")


def _list(item):
    def check(errors, value, path):
        if not isinstance(value, list):
            errors.add(path, f"expected a list, got {type(value).__name__}")
            return
        for i, v in enumerate(value):
            item(errors, v, f"{path}[{i}]")

    return check


def _object(fields, optional=None):
    """An object with exactly `fields`, plus any of `optional` (which may be left out)."""
    optional = optional or {}

    def check(errors, value, path):
        if not isinstance(value, dict):
            errors.add(path, f"expected an object, got {type(value).__name__}")
            return
        for key in fields.keys() - value.keys():
            errors.add(f"{path}.{key}", "missing")
        for key in value.keys() - fields.keys() - optional.keys():
            errors.add(f"{path}.{key}", "unexpected field")
        for key, field in {**fields, **optional}.items():
            if key in value:
                field(errors, value[key], f"{path}.{key}")

    return check


_INT, _NUM, _STR, _BOOL = _type("int"), _type("num"), _type("str"), _type("bool")
_INT_OR_NULL, _NUM_OR_NULL = _type("int", True), _type("num", True)


def _num_or_pair(errors, value, path):
    """server_to_server (A9, spec v3.1): one number (Mastic: 0), [sent by server0, sent by
    server1], or null."""
    if isinstance(value, list):
        _pair(errors, value, path)
    else:
        _NUM_OR_NULL(errors, value, path)


_OUTPUT_FIELDS = _object({
    "label": _type("str", True),
    # Spec v3.1: null for outputs that are not real cells (Mosaic's H3 coordinate triples) ...
    "lat": _NUM_OR_NULL,
    "lon": _NUM_OR_NULL,
    # ... and null when the system does not reveal counts (Mosaic).
    "count": _INT_OR_NULL,
})


def _output(errors, value, path):
    """One reported location: lat and lon are both numbers or both null."""
    _OUTPUT_FIELDS(errors, value, path)
    if isinstance(value, dict) and "lat" in value and "lon" in value \
            and (value["lat"] is None) != (value["lon"] is None):
        errors.add(path, "lat and lon must both be numbers or both be null")

_HEADER = {
    "schema_version": _enum(SCHEMA_VERSION),
    "system": _enum("mastic", "mosaic"),
    "encoding": _enum(*ENCODINGS),
    "dataset": _STR,
    "subset": _enum(*SUBSETS),
    "n_clients": _INT,
    "rep": _INT,
    "warmup": _BOOL,
    "timestamp": _iso_time,
    "params": _object({
        "data_bits": _INT_OR_NULL,
        "threshold_frac": _NUM,
        "threshold_abs": _INT,
        "delta": _INT_OR_NULL,
        "encoding_params": _type("dict"),
    }),
    "env": _object({
        "cpu": _STR,
        "cores_physical": _INT,
        "cores_logical": _INT,
        "ram_gb": _NUM,
        "os": _STR,
        "threads": _INT,
        "compiler": _type("str", True),
        "commit": _type("str", True),
        "dirty": _type("bool", True),
    }),
}

_RUN = _object({
    **_HEADER,
    "phases": _object({
        "server_offline_ms": _NUM_OR_NULL,
        "upload_ms": _NUM_OR_NULL,
        "online_ms": _NUM_OR_NULL,
        "online_steps": _list(_object({"name": _STR, "ms": _NUM_OR_NULL})),
    }),
    "rounds": _object({"upload": _INT_OR_NULL, "online": _INT_OR_NULL, "extra": _INT_OR_NULL}),
    "bytes": _object({
        "upload": _object({"client_to_server": _pair}),
        "online": _object({
            "collector_to_server": _pair,
            "server_to_collector": _pair,
            "server_to_server": _num_or_pair,
        }),
    }),
    "leakage": _object({
        "revealed_aggregates_total": _INT_OR_NULL,
        "revealed_aggregates_per_level": _list(_INT),
        "revealed_kind": _type("str", True),
        "client_validity_bits": _INT_OR_NULL,
        "frontier_per_level": _list(_INT),
    }),
    "output": _list(_output),
}, optional={
    # Spec v3.1: system-specific values without a field of their own; only required to be an
    # object. evaluate.py shows them in tables.md, not in the summary columns.
    "extra": _type("dict"),
})

_CLIENT = _object({
    **_HEADER,
    "client": _object({
        "k": _INT,
        "keygen_ms_mean": _NUM_OR_NULL,
        "keygen_ms_median": _NUM_OR_NULL,
        "keygen_ms_p95": _NUM_OR_NULL,
        "report_bytes_per_server": _pair,
        "upload_bytes_per_client": _NUM_OR_NULL,
    }),
})


def validate_result(obj, kind="run"):
    """Problems with a result line ("run": results/raw/<system>.jsonl, "client":
    results/raw/<system>_client.jsonl). An empty list means the line is valid."""
    errors = _Errors()
    {"run": _RUN, "client": _CLIENT}[kind](errors, obj, "$")
    return sorted(errors)


def append_jsonl(path, obj, kind="run"):
    """Check a result line against A9, then append it to a JSONL file."""
    errors = validate_result(obj, kind)
    if errors:
        raise ValueError("result line does not match the A9 schema:\n  " + "\n  ".join(errors))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(obj, separators=(",", ":")) + "\n")


# --- Self-check (A5) ---------------------------------------------------------------------------


def self_check(output, truth):
    """A5 self-check of one run's output against the plaintext answer truth["cells"] (same system,
    encoding, subset and tau). Outputs are matched to the answer's cells by label.

    - precision / recall: share of reported labels in the answer / of the answer's labels reported;
    - exact: every count equals the answer's. None when the run reveals no counts (every output
      count null, A9 spec v3.1): then only the sets of reported locations are compared;
    - centres: equal where both sides have coordinates. An output without coordinates (lat/lon
      null, not a real cell, A9 spec v3.1) must have null coordinates in the answer too;
    - problems: labels that are null or appear twice, counts that are only partly null, and
      coordinates on one side only.
    ok = precision 1, recall 1, exact not False, centres equal and no problems.
    """
    problems = []
    labels = [c["label"] for c in output]
    if any(label is None for label in labels) or len(set(labels)) != len(labels):
        problems.append("output labels must be unique and non-null")
    want = {c["label"]: c for c in truth["cells"]}
    got = {c["label"]: c for c in output}
    matched = want.keys() & got.keys()
    precision = len(matched) / len(got) if got else float(not want)
    recall = len(matched) / len(want) if want else 1.0
    counts = [c["count"] for c in output]
    if output and all(v is None for v in counts):
        exact = None  # the system reveals no counts: locations only
    else:
        if any(v is None for v in counts):
            problems.append("some output counts are null and others are not")
        exact = matched == want.keys() == got.keys() and all(
            got[k]["count"] == want[k]["count"] for k in matched)

    def located(c):
        return c.get("lat") is not None and c.get("lon") is not None

    if any(located(got[k]) != located(want[k]) for k in matched):
        problems.append("an output has coordinates where the plaintext answer has none, or the "
                        "reverse")
    both = [k for k in matched if located(got[k]) and located(want[k])]
    centre = max((max(abs(got[k]["lat"] - want[k]["lat"]), abs(got[k]["lon"] - want[k]["lon"]))
                  for k in both), default=0.0)
    return {"precision": precision, "recall": recall, "exact": exact,
            "cells": len(got), "truth_cells": len(want), "max_centre_diff_deg": centre,
            "problems": problems,
            "ok": precision == 1.0 and recall == 1.0 and exact is not False and centre < 1e-6
            and not problems}


# --- Self-test and CLI -------------------------------------------------------------------------

# The examples of A9.
EXAMPLE_RUN = {
    "schema_version": 1,
    "system": "mastic",
    "encoding": "geohash",
    "dataset": "day",
    "subset": "1of3",
    "n_clients": 7194,
    "rep": 2,
    "warmup": False,
    "timestamp": "2026-11-10T14:03:11+01:00",
    "params": {"data_bits": 20, "threshold_frac": 0.005, "threshold_abs": 35, "delta": None,
               "encoding_params": {"prefix": "9v6", "bits": 20}},
    "env": {"cpu": "...", "cores_physical": 8, "cores_logical": 16, "ram_gb": 16, "os": "...",
            "threads": 4, "compiler": "rustc 1.xx", "commit": "abc123", "dirty": False},
    "phases": {"server_offline_ms": 0, "upload_ms": 812.4, "online_ms": 4210.7,
               "online_steps": [{"name": "flp", "ms": 640.2}, {"name": "level_0", "ms": 151.3}]},
    "rounds": {"upload": 1, "online": 44, "extra": 0},
    "bytes": {"upload": {"client_to_server": [0, 0]},
              "online": {"collector_to_server": [0, 0], "server_to_collector": [0, 0],
                         "server_to_server": 0}},
    "leakage": {"revealed_aggregates_total": 694, "revealed_aggregates_per_level": [2, 4],
                "revealed_kind": "count", "client_validity_bits": 7194,
                "frontier_per_level": [1, 2]},
    "output": [{"label": "9v6s0j9", "lat": 30.265, "lon": -97.732, "count": 120}],
}
EXAMPLE_CLIENT = {
    **{k: v for k, v in EXAMPLE_RUN.items() if k in _HEADER},
    "client": {"k": 2000, "keygen_ms_mean": 0.41, "keygen_ms_median": 0.39,
               "keygen_ms_p95": 0.52, "report_bytes_per_server": [2210, 2210],
               "upload_bytes_per_client": 4420},
}


def self_test():
    """The A9 examples must pass and each broken copy must fail."""

    def broken(base, change):
        obj = copy.deepcopy(base)
        change(obj)
        return obj

    cases = [
        ("A9 run example", EXAMPLE_RUN, "run", True),
        ("A9 client example", EXAMPLE_CLIENT, "client", True),
        ("missing rounds", broken(EXAMPLE_RUN, lambda o: o.pop("rounds")), "run", False),
        ("per-server list of 1", broken(
            EXAMPLE_RUN, lambda o: o["bytes"]["online"].update(collector_to_server=[5])),
         "run", False),
        ("warmup as a string", broken(EXAMPLE_RUN, lambda o: o.update(warmup="no")), "run", False),
        ("unexpected field", broken(EXAMPLE_RUN, lambda o: o.update(extra_field=1)), "run", False),
        ("fractional count", broken(EXAMPLE_RUN, lambda o: o["output"][0].update(count=1.5)),
         "run", False),
        ("unknown encoding", broken(EXAMPLE_RUN, lambda o: o.update(encoding="s2")), "run", False),
        ("client without client", broken(EXAMPLE_CLIENT, lambda o: o.pop("client")), "client",
         False),
        # Spec v3.1 additions (Mosaic).
        ("v3.1: server_to_server per server", broken(
            EXAMPLE_RUN, lambda o: o["bytes"]["online"].update(server_to_server=[120, 80])),
         "run", True),
        ("v3.1: count null", broken(EXAMPLE_RUN, lambda o: o["output"][0].update(count=None)),
         "run", True),
        ("v3.1: lat/lon null", broken(
            EXAMPLE_RUN, lambda o: o["output"].append(
                {"label": "triple (1, 2, -3)", "lat": None, "lon": None, "count": None})),
         "run", True),
        ("v3.1: extra object", broken(
            EXAMPLE_RUN, lambda o: o.update(extra={"h3_noncell_outputs": 4573,
                                                   "dealer_bytes": 120})),
         "run", True),
        ("v3.1: server_to_server with 3 values", broken(
            EXAMPLE_RUN, lambda o: o["bytes"]["online"].update(server_to_server=[1, 2, 3])),
         "run", False),
        ("v3.1: lat null, lon a number", broken(
            EXAMPLE_RUN, lambda o: o["output"][0].update(lat=None)), "run", False),
        ("v3.1: extra as a list", broken(EXAMPLE_RUN, lambda o: o.update(extra=[1])), "run",
         False),
        ("v3.1: extra null", broken(EXAMPLE_RUN, lambda o: o.update(extra=None)), "run", False),
        ("v3.1: extra on a client line", broken(EXAMPLE_CLIENT, lambda o: o.update(extra={})),
         "client", False),
    ]
    ok = True
    for name, obj, kind, should_pass in cases:
        errors = validate_result(obj, kind)
        passed = not errors
        good = passed == should_pass
        ok &= good
        detail = "valid" if passed else f"rejected: {errors[0]}"
        print(f"  {'PASS' if good else 'FAIL'}  {name}: {detail}")
    return ok


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--self-test", action="store_true", help="test the schema check")
    args = parser.parse_args()

    if args.self_test:
        print("Schema check self-test:")
        raise SystemExit(0 if self_test() else 1)

    print("Environment (A8):")
    print(json.dumps(collect_env(), indent=1))
    print(f"\nHarness parameters: dataset {DATASET}, seed {SEED}, threshold tau per system "
          f"{THRESHOLDS} (this codebase: {THRESHOLD_FRAC}), delta {DELTA}; reference at tau "
          f"{REFERENCE_TAU}, delta {REFERENCE_DELTA}")
    for name, spec in ENCODINGS.items():
        print(f"  {name:12} n = {spec['data_bits']:2}  {spec['params']}")
    print("\nsrc/configs/ride-austin-day-* against the harness parameters:")
    for result in compare_configs():
        status = "equal" if not result["differences"] else "; ".join(result["differences"])
        print(f"  {result['file']}: {status}")


if __name__ == "__main__":
    main()
