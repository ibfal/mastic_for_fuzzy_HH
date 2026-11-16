#!/usr/bin/env python3
"""Run Mastic on the Melbourne parking app set and check the result (apps/parking/PARKING_PLAN.md, P3).

For each encoding (Morton, geohash, H3; tau = 1 %, 24 hour-of-day buckets):

1. build the release binaries once (`cargo build --release --bin server --bin driver`);
2. write the driver config (parking_common.config_dict, with free ports, never 8000) to
   apps/parking/.run/configs/;
3. start both servers, run the driver on apps/parking/data/parking_day_snapshot.csv with
   --export apps/parking/results/mastic_<encoding>.json, and stop the servers (also on failure and
   Ctrl+C);
4. check the export against the plaintext ground truth apps/parking/data/ground_truth/mastic_app.json:
   N and T, the same heavy cells (bits and label), each cell's count and all 24 hour counts, its
   centre, and that the driver printed one line per heavy cell.

Writes apps/parking/results/check.json (one entry per encoding; entries of encodings not run are
kept). Logs and configs go to apps/parking/.run/ (ignored by git). Exit code 1 if any check fails.

    .\\.venv\\Scripts\\python.exe -I apps\\parking\\scripts\\run_app.py
    .\\.venv\\Scripts\\python.exe -I apps\\parking\\scripts\\run_app.py --only-encoding morton --no-build
    .\\.venv\\Scripts\\python.exe -I apps\\parking\\scripts\\run_app.py --check-only   # re-check the saved results
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # -I drops the script's folder
import parking_common as pc  # noqa: E402  (also puts bench/scripts on sys.path)
import run_bench as rb  # noqa: E402  process helpers: find_cargo, port_busy, wait_ports_open, stop

bc = pc.bc
RESULTS = pc.APP / "results"
RUN_DIR = pc.APP / ".run"
TRUTH = pc.GT / "mastic_app.json"
APP_CSV = pc.report_path("snapshot")
DEFAULT_PORT_BASE = 18100
CENTRE_TOLERANCE = 1e-6


class AppError(RuntimeError):
    pass


def export_path(encoding):
    return RESULTS / f"mastic_{encoding}.json"


def free_ports(base):
    """The first pair (p, p + 1) from `base` on that is free and not reserved."""
    for p in range(base, 65534):
        pair = (p, p + 1)
        if not set(pair) & rb.RESERVED_PORTS and not any(rb.port_busy(x) for x in pair):
            return pair
    raise AppError(f"no free port pair from {base}")


def build():
    cmd = [rb.find_cargo(), "build", "--release", "--bin", "server", "--bin", "driver"]
    print("Building:", " ".join(cmd[1:]), flush=True)
    proc = subprocess.run(cmd, cwd=pc.REPO, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AppError(f"cargo build failed:\n{proc.stdout}{proc.stderr}")


def run_encoding(encoding, rows, threads, port_base):
    """Servers + driver for one encoding; returns run facts (wall time, peak memory, log)."""
    ports = free_ports(port_base)
    servers_addr = tuple(f"127.0.0.1:{p}" for p in ports)
    config = RUN_DIR / "configs" / f"parking-day-{encoding}.toml"
    pc.write_config(config, pc.config_dict(encoding, csv=APP_CSV.resolve().as_posix(),
                                           servers=servers_addr),
                    header="# Written by apps/parking/scripts/run_app.py for one run.")
    logs = RUN_DIR / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    server_logs = [logs / f"{encoding}.server-{i}.log" for i in (0, 1)]
    driver_log = logs / f"{encoding}.driver.log"
    out = export_path(encoding)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)  # never check a stale export

    server_env = {**os.environ, "RAYON_NUM_THREADS": str(threads)}
    driver_env = {k: v for k, v in os.environ.items() if k != "RAYON_NUM_THREADS"}
    servers, handles, driver = [], [], None
    t0 = time.perf_counter()
    try:
        for i, log in enumerate(server_logs):
            h = open(log, "wb")
            handles.append(h)
            servers.append(subprocess.Popen(
                [str(rb.TARGET / f"server{rb.EXE}"), "--config", str(config), "--server_id", str(i)],
                cwd=pc.REPO, env=server_env, stdout=h, stderr=subprocess.STDOUT))
        rb.wait_ports_open(servers, list(ports), server_logs)

        h = open(driver_log, "wb")
        handles.append(h)
        cmd = [str(rb.TARGET / f"driver{rb.EXE}"), "--config", str(config),
               "--num_clients", str(rows), "--export", str(out)]
        driver = subprocess.Popen(cmd, cwd=pc.REPO, env=driver_env, stdout=h,
                                  stderr=subprocess.STDOUT)
        peak = [0.0, 0.0, 0.0]  # server 0, server 1, driver (MiB)
        while True:
            try:
                driver.wait(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                pass
            for i, p in enumerate(servers + [driver]):
                peak[i] = max(peak[i], rb.peak_mb(p) or 0.0)
            for i, s in enumerate(servers):
                if s.poll() is not None:
                    driver.kill()
                    raise AppError(f"server {i} exited during the run:\n{rb.tail(server_logs[i])}")
        for i, s in enumerate(servers):
            peak[i] = max(peak[i], rb.peak_mb(s) or 0.0)  # peak working set, read before stopping
        if driver.returncode != 0:
            raise AppError(f"driver exited with code {driver.returncode}:\n{rb.tail(driver_log)}")
    finally:
        rb.stop(servers + ([driver] if driver else []))
        for h in handles:
            h.close()
    if not out.exists():
        raise AppError(f"the driver did not write {pc.relpath(out)}")
    return {"wall_s": round(time.perf_counter() - t0, 2), "ports": list(ports), "threads": threads,
            "peak_mb": {"server_0": round(peak[0]), "server_1": round(peak[1]),
                        "driver": round(peak[2])},
            "driver_log": pc.relpath(driver_log)}


def printed_cells(driver_log):
    """Lines the driver printed for the final cells ("Value (<bits>) ..."), or None without a log."""
    path = Path(driver_log) if driver_log else None
    if not path or not path.exists():
        return None
    text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return sum(1 for line in text if line.startswith("Value ("))


def check(encoding, export, truth, driver_log=None):
    """Compare one export with the plaintext ground truth of the same encoding."""
    problems = []
    p = export.get("params", {})
    if export.get("format") != "mastic-crawl/1":
        problems.append(f"format {export.get('format')!r}")
    for key, want in (("encoding", encoding), ("N", truth["N"]), ("T", truth["T"]),
                      ("tau", pc.TAU_MASTIC), ("hist_buckets", pc.HIST_BUCKETS),
                      ("data_bits", truth["data_bits"])):
        if p.get(key) != want:
            problems.append(f"params.{key} = {p.get(key)!r}, expected {want!r}")
    if p.get("encoding_params") != truth["params"]:
        problems.append(f"params.encoding_params {p.get('encoding_params')} != {truth['params']}")
    rides = export.get("rides") or {}
    if rides.get("dropped") != 0:
        problems.append(f"rides dropped: {rides.get('dropped')}")

    got = {h["bits"]: h for h in export.get("heavy_hitters", [])}
    want = {c["bits"]: c for c in truth["cells"]}
    missing, extra = sorted(want.keys() - got.keys()), sorted(got.keys() - want.keys())
    if missing:
        problems.append(f"{len(missing)} heavy cells missing: {[want[b]['label'] for b in missing]}")
    if extra:
        problems.append(f"{len(extra)} cells not in the ground truth: {[got[b]['label'] for b in extra]}")
    both = sorted(want.keys() & got.keys())
    labels_ok = all(got[b]["label"] == want[b]["label"] for b in both)
    counts_ok = all(got[b]["count"] == want[b]["count"] for b in both)
    buckets_ok = all(got[b].get("buckets") == want[b]["hours"] for b in both)
    sums_ok = all(sum(got[b].get("buckets") or []) == got[b]["count"] for b in got)
    centre = max((max(abs(got[b]["center"][0] - want[b]["lat"]),
                      abs(got[b]["center"][1] - want[b]["lon"])) for b in both), default=0.0)
    for name, ok in (("labels", labels_ok), ("counts", counts_ok), ("24 hour counts", buckets_ok),
                     ("bucket sums = counts", sums_ok)):
        if not ok:
            problems.append(f"{name} differ")
    if centre >= CENTRE_TOLERANCE:
        problems.append(f"centres differ by up to {centre:g} degrees")
    printed = printed_cells(driver_log)
    if printed is not None and printed != len(got):
        problems.append(f"driver printed {printed} cells, export has {len(got)} (E7)")
    zero_last = sum(1 for h in got.values() if (h.get("buckets") or [1])[-1] == 0)
    return {
        "encoding": encoding, "N": p.get("N"), "T": p.get("T"), "cells": len(got),
        "truth_cells": len(want), "exact_cells": not missing and not extra,
        "exact_counts": counts_ok and not missing and not extra,
        "exact_hours": buckets_ok and not missing and not extra,
        "max_centre_diff_deg": centre, "printed_cells": printed,
        "cells_with_zero_last_hour": zero_last,
        "export_bytes": export_path(encoding).stat().st_size if export_path(encoding).exists() else None,
        "problems": problems, "ok": not problems,
    }


def parse_args():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--only-encoding", action="append", choices=list(pc.ENCODINGS),
                   help="run only this encoding (repeatable)")
    p.add_argument("--no-build", action="store_true", help="use the existing release binaries")
    p.add_argument("--check-only", action="store_true",
                   help="do not run; check the saved results/mastic_<encoding>.json")
    p.add_argument("--threads", type=int,
                   help="RAYON_NUM_THREADS per server (default: floor(physical cores / 2))")
    p.add_argument("--port-base", type=int, default=DEFAULT_PORT_BASE,
                   help=f"first port to try (default {DEFAULT_PORT_BASE}; 8000 is never used)")
    args = p.parse_args()
    if not 1024 <= args.port_base <= 65533:
        p.error(f"--port-base {args.port_base}: out of range")
    return args


def main():
    pc.configure_stdout()
    args = parse_args()
    encodings = args.only_encoding or list(pc.ENCODINGS)
    truth_all = json.loads(TRUTH.read_text(encoding="utf-8"))
    if truth_all["source"]["sha256"] != pc.sha256_file(APP_CSV):
        print(f"ERROR: {pc.relpath(APP_CSV)} does not match the SHA-256 in {pc.relpath(TRUTH)}",
              file=sys.stderr)
        return 1
    rows = len(pc.read_reports(APP_CSV))
    env = bc.collect_env()
    threads = args.threads or env["threads"]

    check_file = RESULTS / "check.json"
    previous = json.loads(check_file.read_text(encoding="utf-8")) if check_file.exists() else {}
    entries = previous.get("encodings", {})
    all_ok = True
    try:
        if not args.check_only and not args.no_build:
            build()
        for enc in encodings:
            truth = truth_all["encodings"][enc]
            run = None
            if args.check_only:
                print(f"{enc}: checking {pc.relpath(export_path(enc))}", flush=True)
                run = entries.get(enc, {}).get("run")
            else:
                print(f"{enc}: running on {pc.relpath(APP_CSV)} ({rows} reports, tau "
                      f"{pc.TAU_MASTIC:g}, {pc.HIST_BUCKETS} buckets, {threads} threads per server)",
                      flush=True)
                run = run_encoding(enc, rows, threads, args.port_base)
            if not export_path(enc).exists():
                raise AppError(f"{pc.relpath(export_path(enc))} is missing")
            export = json.loads(export_path(enc).read_text(encoding="utf-8"))
            result = check(enc, export, truth, run and run.get("driver_log"))
            entries[enc] = {**result, "run": run, "export": pc.relpath(export_path(enc))}
            all_ok &= result["ok"]
            extra = (f"; {run['wall_s']:.1f} s, peak {run['peak_mb']['server_0']} / "
                     f"{run['peak_mb']['server_1']} / {run['peak_mb']['driver']} MiB "
                     f"(server 0 / 1 / driver)") if run else ""
            print(f"  {'PASS' if result['ok'] else 'FAIL'}: {result['cells']} heavy cells "
                  f"(truth {result['truth_cells']}), N {result['N']}, T {result['T']}, counts "
                  f"{'exact' if result['exact_counts'] else 'NOT exact'}, 24 hour counts "
                  f"{'exact' if result['exact_hours'] else 'NOT exact'}, printed "
                  f"{result['printed_cells']}, {result['cells_with_zero_last_hour']} with 0 at "
                  f"hour 23{extra}", flush=True)
            for problem in result["problems"]:
                print(f"    - {problem}")
    except KeyboardInterrupt:
        print("\nInterrupted; servers stopped.", file=sys.stderr)
        return 130
    except (AppError, rb.RunError) as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 1

    if not args.check_only:
        bc.write_json(check_file, {
            "truth": pc.relpath(TRUTH), "app_set": pc.relpath(APP_CSV),
            "commit": env["commit"], "dirty": env["dirty"], "updated": bc.timestamp(),
            "encodings": {k: entries[k] for k in pc.ENCODINGS if k in entries},
        })
        print(f"Wrote {pc.relpath(check_file)}")
    print("All checks pass." if all_ok else "SOME CHECKS FAILED.")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
