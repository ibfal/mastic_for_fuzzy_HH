#!/usr/bin/env python3
"""Melbourne parking application: all parameters in one place (apps/parking/PARKING_PLAN.md §4–§6).

Reuses the benchmark's helpers (bench/scripts/bench_common.py) and plaintext encoders
(scripts/check_heavy_hitters.py, identical to src/geo.rs) by import only; their Austin settings are
not touched.

    .\\.venv\\Scripts\\python.exe -I apps\\parking\\scripts\\parking_common.py   # print the parameters
"""

import csv
import datetime as dt
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
APP = REPO / "apps" / "parking"
SCRIPTS = APP / "scripts"
DATA = APP / "data"
GT = DATA / "ground_truth"
REPORTS = APP / "reports"

sys.dont_write_bytecode = True
for p in (REPO / "bench" / "scripts", REPO / "scripts", SCRIPTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import bench_common as bc  # noqa: E402
import check_heavy_hitters as chh  # noqa: E402

# Helpers reused from the benchmark (their behaviour is dataset independent).
threshold_abs = bc.threshold_abs  # T = max(1, floor(tau * N))
sha256_file = bc.sha256_file
relpath = bc.relpath
write_json = bc.write_json
MAKE_SUBSETS = REPO / "bench" / "scripts" / "make_subsets.py"

# --- Data and day (PARKING_PLAN.md §4) -------------------------------------------------------

DAY = dt.date(2019, 10, 11)  # Friday; busiest eligible regular weekday (P1, D6)
DATASET = "parking_day"
SEED = 20261107
BENCH_N = 21581  # same N as the ride benchmark
SUBSETS = ("1of3", "2of3", "3of3")
DEFAULT_JOIN = "2c"
JOINS = {"2c": "2c segment, exact or partial with a single candidate",
         "2b": "2b segment, exact or partial (one cross street)"}
CLEAN_MIN_S, CLEAN_MAX_S = 120, 86400  # 2 min <= duration <= 24 h
STITCH_GAP_S = 1
# Fault flag of a bay-day (StreetMarker x arrival date), D7: kept, flagged.
FAULT_MAX_STAYS = 80
FAULT_MIN_STAYS_FOR_BLIP_SHARE = 10
FAULT_BLIP_SHARE = 0.5
SNAPSHOT_MINUTE = 30  # design B snapshot: the car is parked at h:30
HOURS = 24
WINDOWS = [(h, h + 4) for h in range(0, 24, 4)]  # Mosaic runs one window at a time

REPORT_COLUMNS = ["start_location_lat", "start_location_long", "hour", "stay_id", "street_marker",
                  "segment", "join_method", "fault_flag", "arrival", "departure"]

# --- Thresholds (§6) -------------------------------------------------------------------------

TAU_MASTIC = 0.01
DELTA = 3  # Mosaic radius in 0.001-degree cells (7x7 square)
TAU_MOSAIC = 0.10  # per 4-hour window (D3)
TAU_MOSAIC_BENCH = 0.10  # whole-day benchmark set, no windows: Mastic-vs-Mosaic comparison (D2)
SCALE = 1000  # 0.001-degree grid

# --- Encodings (§5) --------------------------------------------------------------------------

ENCODINGS = {
    "morton": {
        "data_bits": 12,
        "params": {"min_lat": -37.8445, "max_lat": -37.7805, "min_lon": 144.9315,
                   "max_lon": 144.9955, "axis_bits": 6},
    },
    "geohash": {"data_bits": 21, "params": {"prefix": "r1r", "bits": 21}},
    "h3": {"data_bits": 15, "params": {"prefix": "85be6357fffffff", "resolution": 10}},
}

# P1 reference counts (reports/01-exploration.md), checked by check_preparation.py.
P1_EXPECTED = {
    "sensor rows": 42_672_743,
    "vehicle-present rows": 22_591_812,
    "stays": 17_695_077,
    "stays arriving in 2019": 17_695_077,
    "clean stays": 12_601_568,
    "clean stays joined (2c)": 11_188_093,
    "design A reports (day)": 38_400,
    "design B overlap reports (day)": 79_355,
}


def encoder(name):
    """Plaintext encoder for one of ENCODINGS (bit strings, as src/geo.rs)."""
    p = ENCODINGS[name]["params"]
    if name == "geohash":
        return chh.make_encoder("geohash", prefix=p["prefix"], bits=p["bits"])
    if name == "h3":
        return chh.make_encoder("h3", prefix=p["prefix"], resolution=p["resolution"])
    if name == "morton":
        bbox = (p["min_lat"], p["max_lat"], p["min_lon"], p["max_lon"])
        return chh.make_encoder("morton", bbox=bbox, axis_bits=p["axis_bits"])
    raise ValueError(f"unknown encoding {name!r}")


def grid_index(v):
    """0.001-degree grid point of a coordinate (Mosaic's rounding; equals the Morton cell index
    offset with the half-off box)."""
    import math
    return math.floor(v * SCALE + 0.5)


def window_name(a, b):
    return f"w{a:02d}-{b:02d}"


def report_path(kind, join=DEFAULT_JOIN, window=None):
    """kind: snapshot | overlap | arrivals."""
    base = DATA if join == DEFAULT_JOIN else DATA / f"join{join}"
    suffix = f"_{window_name(*window)}" if window else ""
    return base / f"{DATASET}_{kind}{suffix}.csv"


def subset_path(subset):
    return DATA / f"{DATASET}_{subset}.csv"


def read_reports(path):
    """[(lat, lon, hour)] of a report CSV."""
    with open(path, newline="", encoding="utf-8") as f:
        return [(float(r["start_location_lat"]), float(r["start_location_long"]), int(r["hour"]))
                for r in csv.DictReader(f)]


def configure_stdout():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")


if __name__ == "__main__":
    configure_stdout()
    print(json.dumps({"day": DAY.isoformat(), "seed": SEED, "bench_n": BENCH_N, "join": DEFAULT_JOIN,
                      "tau_mastic": TAU_MASTIC, "delta": DELTA, "tau_mosaic": TAU_MOSAIC,
                      "windows": WINDOWS, "encodings": ENCODINGS}, indent=1))
