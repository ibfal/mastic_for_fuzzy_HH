#!/usr/bin/env python3
"""Run the Mastic benchmark (bench/BENCHMARK_MASTIC.md: A2, A6, A8, A9, B7).

    .\\.venv\\Scripts\\python.exe bench\\scripts\\run_bench.py --smoke     # smoke runs, ten_locations.csv
    .\\.venv\\Scripts\\python.exe bench\\scripts\\run_bench.py             # the full benchmark (72 runs)
    .\\.venv\\Scripts\\python.exe bench\\scripts\\run_bench.py --only-encoding geohash --only-subset 3of3 --reps 1 --no-warmup --results-dir bench\\.run\\check
    .\\.venv\\Scripts\\python.exe bench\\scripts\\run_bench.py --tau 0.02 --dry-run   # another tau (needs ground_truth.py --tau 0.02)

Steps: build the release binaries once; write one driver config per encoding x subset into
bench/.run/configs/ (encoding parameters from bench_common.ENCODINGS, threshold tau from A2 or
--tau; the driver records it in params.threshold_frac); run
keygen_bench once per encoding (A6); then, repetition loop outermost (A8), for every encoding
and subset: start both servers with RAYON_NUM_THREADS set, wait for their ports, run the driver
with -n = the subset's rows and --bench-out, check the result line against the A9 schema and the
ground truth, append it to <results-dir>/mastic.jsonl, and stop the servers (also on errors and
Ctrl+C). Never passes --malicious.

Logs, configs and a per-run record (wall-clock time, peak memory) go to bench/.run/ (ignored by
Git).
"""

import argparse
import json
import math
import os
import shutil
import socket
import subprocess
import sys
import time
import tomllib
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench_common as bc  # noqa: E402

RUN_DIR = bc.BENCH / ".run"
RESULTS_DIR = bc.BENCH / "results" / "raw"
EXE = ".exe" if sys.platform == "win32" else ""
TARGET = bc.ROOT / "target" / "release"

# Server ports that must never be used (8000: Splunk on the benchmark laptop).
RESERVED_PORTS = {8000}
DEFAULT_PORT_BASE = 18100

# The driver settings that are not encoding parameters (bc.BASE_CONFIG), the smoke subset's name
# (bc.SMOKE_DATASET, bc.SMOKE_SUBSET) and the self-check (bc.self_check) live in bench_common.py,
# so that evaluate.py does not need this module.
SMOKE_CSV = bc.ROOT / "data" / "synthetic" / "ten_locations.csv"
SMOKE_THRESHOLD = 0.1
SMOKE_TRUTH = bc.GROUND_TRUTH_DIR / "smoke_ten_locations.json"

KEYGEN_CLIENTS = 2000  # A6


class RunError(RuntimeError):
    pass


# --- Subsets and configs -----------------------------------------------------------------------


def subset_specs(args):
    """What each run reads: {subset: {csv, rows, truth, dataset, threshold}}."""
    if args.smoke:
        rows = len(bc.read_points(SMOKE_CSV))
        return {bc.SMOKE_SUBSET: {"csv": SMOKE_CSV, "rows": rows, "truth": SMOKE_TRUTH,
                                  "dataset": bc.SMOKE_DATASET, "threshold": SMOKE_THRESHOLD}}
    manifest = {f["subset"]: f for f in bc.load_manifest()["files"]}
    specs = {}
    for subset in args.only_subset or bc.SUBSETS:
        entry = manifest[subset]
        csv = bc.ROOT / entry["path"]
        if bc.sha256_file(csv) != entry["sha256"]:
            raise RunError(f"{entry['path']} does not match the SHA-256 in bench/data/manifest.json")
        truth = bc.ground_truth_path(bc.DATASET, subset, args.tau)
        if not truth.exists():
            raise RunError(f"{bc.relpath(truth)} is missing: run ground_truth.py --tau {args.tau} "
                           f"first (the self-check needs the plaintext answer at this tau)")
        specs[subset] = {"csv": csv, "rows": entry["rows"], "truth": truth,
                         "dataset": bc.DATASET, "threshold": args.tau}
    return specs


def config_name(encoding, subset, tau):
    """Config file stem; configs at a tau other than Mastic's get a suffix, so they never replace
    the configs of the main runs."""
    suffix = "" if tau == bc.THRESHOLD_FRAC else f"-tau{bc.tau_tag(tau)}"
    return f"{encoding}-{subset}{suffix}"


def toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v)  # a valid TOML basic string
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{k} = {toml_value(x)}" for k, x in v.items()) + " }"
    raise TypeError(f"no TOML for {v!r}")


def config_dict(encoding, csv, threshold, port_base):
    return {
        "mode": {"weighted_heavy_hitters": {"threshold": threshold}},
        "data_bits": bc.ENCODINGS[encoding]["data_bits"],
        **bc.BASE_CONFIG,
        "server_0": f"127.0.0.1:{port_base}",
        "server_1": f"127.0.0.1:{port_base + 1}",
        "dataset": {"path": Path(csv).resolve().as_posix(),
                    "encoding": bc.config_encoding(encoding), "weight": "constant"},
    }


def write_config(path, cfg):
    """Write a driver config and check that it reads back as `cfg`."""
    lines = ["# Written by bench/scripts/run_bench.py for one benchmark configuration.",
             f"mode.weighted_heavy_hitters.threshold = {toml_value(cfg['mode']['weighted_heavy_hitters']['threshold'])}"]
    lines += [f"{k} = {toml_value(v)}" for k, v in cfg.items() if k not in ("mode", "dataset")]
    lines += ["", "[dataset]"] + [f"{k} = {toml_value(v)}" for k, v in cfg["dataset"].items()]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    if tomllib.loads(path.read_text(encoding="utf-8")) != cfg:
        raise RunError(f"{path} does not read back as intended")


def check_base_config():
    """Warn if src/configs differ from bc.BASE_CONFIG (the settings run_bench writes itself)."""
    for name, spec in bc.ENCODINGS.items():
        path = bc.ROOT / "src" / "configs" / f"ride-austin-day-{spec['config']}.toml"
        if not path.exists():
            continue
        cfg = tomllib.loads(path.read_text(encoding="utf-8"))
        diffs = {k: cfg.get(k) for k, v in bc.BASE_CONFIG.items() if cfg.get(k) != v}
        if diffs:
            print(f"WARNING: {bc.relpath(path)} has {diffs}; the benchmark uses {bc.BASE_CONFIG}")


# --- Processes ---------------------------------------------------------------------------------


def find_cargo():
    for candidate in (shutil.which("cargo"), str(Path.home() / ".cargo" / "bin" / f"cargo{EXE}")):
        if candidate and Path(candidate).exists():
            return candidate
    raise RunError("cargo not found")


def build():
    cmd = [find_cargo(), "build", "--release", "--bin", "server", "--bin", "driver",
           "--bin", "keygen_bench"]
    print("Building:", " ".join(cmd[1:]), flush=True)
    proc = subprocess.run(cmd, cwd=bc.ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RunError(f"cargo build failed:\n{proc.stdout}{proc.stderr}")
    print("  " + (proc.stderr.strip().splitlines() or ["done"])[-1].strip())


def port_busy(port):
    """True if something accepts connections on the port or holds it."""
    with socket.socket() as s:
        s.settimeout(0.3)
        if s.connect_ex(("127.0.0.1", port)) == 0:
            return True
    with socket.socket() as s:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def wait_ports_free(ports, timeout=15.0):
    deadline = time.monotonic() + timeout
    while True:
        busy = [p for p in ports if port_busy(p)]
        if not busy:
            return
        if time.monotonic() > deadline:
            raise RunError(f"port(s) {busy} are in use; pick other ports with --port-base")
        time.sleep(0.25)


def tail(path, lines=25):
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "(no log)"
    return "\n".join("    " + line for line in text[-lines:])


def wait_ports_open(servers, ports, logs, timeout=60.0):
    deadline = time.monotonic() + timeout
    pending = list(ports)
    while pending:
        for proc, log in zip(servers, logs):
            if proc.poll() is not None:
                raise RunError(f"server exited with code {proc.returncode}:\n{tail(log)}")
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", pending[0])) == 0:
                pending.pop(0)
                continue
        if time.monotonic() > deadline:
            raise RunError(f"servers did not open port(s) {pending} within {timeout:.0f} s")
        time.sleep(0.05)


def peak_mb(proc):
    """Peak working set (Windows) or current RSS, in MiB; None once the process is gone."""
    import psutil

    try:
        info = psutil.Process(proc.pid).memory_info()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None
    return getattr(info, "peak_wset", info.rss) / 2**20


def stop(procs):
    for p in procs:
        if p.poll() is None:
            p.terminate()
    for p in procs:
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()


# --- One run -----------------------------------------------------------------------------------


def write_meta(path, dataset, subset, rep, warmup, env):
    meta = {"dataset": dataset, "subset": subset, "rep": rep, "warmup": warmup,
            "timestamp": bc.timestamp(), "env": env}
    path.write_text(json.dumps(meta), encoding="utf-8")


def read_line(path):
    lines = Path(path).read_text(encoding="utf-8").splitlines() if Path(path).exists() else []
    if len(lines) != 1:
        raise RunError(f"expected one result line in {path}, found {len(lines)}")
    return json.loads(lines[0])


def schema_errors(line, kind="run"):
    """bench_common.validate_result; smoke lines carry subset "smoke", which the schema's subset
    list (the benchmark subsets) does not include, so that one field is checked here instead."""
    if line.get("subset") == bc.SMOKE_SUBSET:
        return bc.validate_result({**line, "subset": bc.SUBSETS[0]}, kind)
    return bc.validate_result(line, kind)


def append_line(path, line, kind="run"):
    """bench_common.append_jsonl, which checks the schema first; smoke lines were checked by
    schema_errors and are written directly."""
    if line.get("subset") != bc.SMOKE_SUBSET:
        bc.append_jsonl(path, line, kind)
        return
    if schema_errors(line, kind):
        raise RunError("smoke line does not match the A9 schema")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(line, separators=(",", ":")) + "\n")


def run_once(args, run, spec, config, env, results_file, truth):
    enc, subset, rep, warmup = run["encoding"], run["subset"], run["rep"], run["warmup"]
    name = f"{config_name(enc, subset, args.tau)}-rep{rep}"
    logs_dir = RUN_DIR / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    line_path, meta_path = RUN_DIR / "line.jsonl", RUN_DIR / "meta.json"
    line_path.unlink(missing_ok=True)
    ports = [args.port_base, args.port_base + 1]
    wait_ports_free(ports)

    server_env = {**os.environ, "RAYON_NUM_THREADS": str(args.threads)}
    driver_env = {k: v for k, v in os.environ.items() if k != "RAYON_NUM_THREADS"}
    server_logs = [logs_dir / f"{name}.server-{i}.log" for i in (0, 1)]
    driver_log = logs_dir / f"{name}.driver.log"
    servers, handles, driver = [], [], None
    t_start = time.perf_counter()
    try:
        for i, log in enumerate(server_logs):
            h = open(log, "wb")
            handles.append(h)
            servers.append(subprocess.Popen(
                [str(TARGET / f"server{EXE}"), "--config", str(config), "--server_id", str(i)],
                cwd=bc.ROOT, env=server_env, stdout=h, stderr=subprocess.STDOUT))
        wait_ports_open(servers, ports, server_logs)

        write_meta(meta_path, spec["dataset"], subset, rep, warmup, env)
        cmd = [str(TARGET / f"driver{EXE}"), "--config", str(config),
               "--num_clients", str(spec["rows"]),
               "--bench-out", str(line_path), "--bench-meta", f"@{meta_path}"]
        h = open(driver_log, "wb")
        handles.append(h)
        t_driver = time.perf_counter()
        driver = subprocess.Popen(cmd, cwd=bc.ROOT, env=driver_env, stdout=h,
                                  stderr=subprocess.STDOUT)
        servers_peak, driver_peak = [0.0, 0.0], 0.0
        while True:
            try:
                driver.wait(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                pass
            driver_peak = max(driver_peak, peak_mb(driver) or 0.0)
            for i, s in enumerate(servers):
                if s.poll() is not None:
                    driver.kill()
                    raise RunError(f"server {i} exited during the run:\n{tail(server_logs[i])}")
        driver_wall = time.perf_counter() - t_driver
        servers_peak = [peak_mb(s) or 0.0 for s in servers]
        if driver.returncode != 0:
            raise RunError(f"driver exited with code {driver.returncode}:\n{tail(driver_log)}")
    finally:
        stop(servers + ([driver] if driver else []))
        for h in handles:
            h.close()
    total = time.perf_counter() - t_start

    line = read_line(line_path)
    errors = schema_errors(line)
    if errors:
        raise RunError("result line does not match the A9 schema:\n  " + "\n  ".join(errors))
    if line["n_clients"] != spec["rows"] or line["n_clients"] != truth["N"]:
        raise RunError(f"n_clients {line['n_clients']}, rows {spec['rows']}, truth N {truth['N']}")
    if line["params"]["threshold_frac"] != spec["threshold"]:
        raise RunError(f"params.threshold_frac {line['params']['threshold_frac']} but the run's "
                       f"tau is {spec['threshold']}")
    if line["params"]["threshold_abs"] != truth["T"]:
        raise RunError(f"threshold {line['params']['threshold_abs']} but ground truth T = {truth['T']}")
    check = bc.self_check(line["output"], truth)
    record = {
        "run": name, "encoding": enc, "subset": subset, "rep": rep, "warmup": warmup,
        "threshold_frac": spec["threshold"],
        "timestamp": line["timestamp"], "n_clients": line["n_clients"],
        "threads": args.threads, "driver_wall_s": round(driver_wall, 3),
        "run_total_s": round(total, 3), "online_ms": line["phases"]["online_ms"],
        "upload_ms": line["phases"]["upload_ms"], "rounds": line["rounds"],
        "peak_mb": {"server_0": round(servers_peak[0], 1), "server_1": round(servers_peak[1], 1),
                    "driver": round(driver_peak, 1)},
        "self_check": check, "results_file": bc.relpath(results_file),
    }
    with open(RUN_DIR / "runner_log.jsonl", "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(record) + "\n")
    if not check["ok"]:
        failed = RUN_DIR / "failed" / f"{name}.jsonl"
        failed.parent.mkdir(parents=True, exist_ok=True)
        failed.write_text(json.dumps(line) + "\n", encoding="utf-8")
        raise RunError(f"self-check failed {check}; line kept in {bc.relpath(failed)}")
    append_line(results_file, line)
    return record


def run_keygen(encoding, config, env, results_dir):
    out = RUN_DIR / "client_line.jsonl"
    meta = RUN_DIR / "client_meta.json"
    out.unlink(missing_ok=True)
    write_meta(meta, bc.DATASET, "3of3", 0, False, {**env, "threads": 1})
    csv = bc.DATA_DIR / f"{bc.DATASET}_3of3.csv"
    cmd = [str(TARGET / f"keygen_bench{EXE}"), "--config", str(config), "--csv", str(csv),
           "--clients", str(KEYGEN_CLIENTS), "--out", str(out), "--bench-meta", f"@{meta}"]
    proc = subprocess.run(cmd, cwd=bc.ROOT, env={**os.environ, "RAYON_NUM_THREADS": "1"},
                          capture_output=True, text=True)
    print("\n".join("  " + l for l in proc.stdout.splitlines() if not l.startswith("Appended")))
    if proc.returncode != 0:
        raise RunError(f"keygen_bench failed for {encoding}:\n{proc.stdout}{proc.stderr}")
    line = read_line(out)
    errors = schema_errors(line, "client")
    if errors:
        raise RunError("client line does not match the A9 schema:\n  " + "\n  ".join(errors))
    bc.append_jsonl(results_dir / f"{bc.SYSTEM}_client.jsonl", line, "client")
    return line


# --- Main --------------------------------------------------------------------------------------


def fmt_time(seconds):
    if seconds is None:
        return "?"
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"


def estimate_left(plan, i, durations, specs):
    """Seconds left after run i: the last time of each configuration, or, for configurations not
    run yet, the observed time per (client x bit) scaled to that configuration."""
    cost = lambda r: specs[r["subset"]]["rows"] * bc.ENCODINGS[r["encoding"]]["data_bits"]
    if not durations:
        return None
    rate = sum(durations.values()) / sum(cost({"encoding": e, "subset": s}) for e, s in durations)
    return sum(durations.get((r["encoding"], r["subset"]), rate * cost(r)) for r in plan[i:])


def parse_args():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--reps", type=int, default=5, help="measured repetitions (default 5)")
    p.add_argument("--no-warmup", action="store_true",
                   help="skip the warm-up repetition (rep 0, discarded by the evaluation)")
    p.add_argument("--smoke", action="store_true",
                   help="data/synthetic/ten_locations.csv, threshold 0.1, 1 rep, no warm-up")
    p.add_argument("--tau", type=float,
                   help=f"threshold fraction tau (default {bc.THRESHOLD_FRAC}, Mastic's tau, A2); "
                        f"recorded in params.threshold_frac. Needs "
                        f"bench/ground_truth/<dataset>_<subset>_tau<tau>.json (ground_truth.py --tau)")
    p.add_argument("--only-encoding", action="append", choices=list(bc.ENCODINGS),
                   help="run only this encoding (repeatable)")
    p.add_argument("--only-subset", action="append", choices=list(bc.SUBSETS),
                   help="run only this subset (repeatable)")
    p.add_argument("--threads", type=int, help="RAYON_NUM_THREADS per server "
                   "(default: floor(physical cores / 2), A8)")
    p.add_argument("--port-base", type=int, default=DEFAULT_PORT_BASE,
                   help=f"server 0 listens on this port, server 1 on the next (default {DEFAULT_PORT_BASE})")
    p.add_argument("--results-dir", type=Path, help="where mastic.jsonl and mastic_client.jsonl "
                   "go (default bench/results/raw, or bench/.run/smoke with --smoke)")
    p.add_argument("--skip-keygen", action="store_true", help="do not run keygen_bench")
    p.add_argument("--no-build", action="store_true", help="use the existing release binaries")
    p.add_argument("--dry-run", action="store_true", help="write the configs, print the plan, stop")
    args = p.parse_args()
    if args.smoke:
        args.reps, args.no_warmup = 1, True
        if args.only_subset:
            p.error("--only-subset does not apply to --smoke")
        if args.tau is not None:
            p.error(f"--tau does not apply to --smoke (threshold {SMOKE_THRESHOLD})")
    if args.tau is None:
        args.tau = bc.THRESHOLD_FRAC
    elif not 0 < args.tau <= 1:
        p.error(f"--tau {args.tau}: must be in (0, 1]")
    if args.reps < 0 or (args.reps == 0 and args.no_warmup):
        p.error("nothing to run")
    ports = {args.port_base, args.port_base + 1}
    if ports & RESERVED_PORTS or not 1024 <= args.port_base <= 65534:
        p.error(f"--port-base {args.port_base}: ports {sorted(ports)} are reserved or invalid")
    return args


def main():
    args = parse_args()
    env = bc.collect_env()
    args.threads = args.threads or env["threads"]
    env["threads"] = args.threads
    results_dir = args.results_dir or (RUN_DIR / "smoke" if args.smoke else RESULTS_DIR)
    results_dir = results_dir if results_dir.is_absolute() else (bc.ROOT / results_dir)
    results_file = results_dir / f"{bc.SYSTEM}.jsonl"
    encodings = args.only_encoding or list(bc.ENCODINGS)
    try:
        specs = subset_specs(args)
    except RunError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    check_base_config()

    # Configs, one per encoding x subset, plus one per encoding for keygen_bench (day_3of3, A6).
    configs = {}
    for enc in encodings:
        for subset, spec in specs.items():
            path = RUN_DIR / "configs" / f"{config_name(enc, subset, args.tau)}.toml"
            write_config(path, config_dict(enc, spec["csv"], spec["threshold"], args.port_base))
            configs[enc, subset] = path
        # The client micro-benchmark does not depend on tau; its line records the run's tau.
        path = RUN_DIR / "configs" / f"{config_name('keygen', enc, args.tau)}.toml"
        write_config(path, config_dict(enc, bc.DATA_DIR / f"{bc.DATASET}_3of3.csv",
                                       args.tau, args.port_base))
        configs[enc, "keygen"] = path

    reps = ([] if args.no_warmup else [0]) + list(range(1, args.reps + 1))
    plan = [{"encoding": enc, "subset": subset, "rep": rep, "warmup": rep == 0}
            for rep in reps for enc in encodings for subset in specs]
    truths = {subset: json.loads(Path(spec["truth"]).read_text(encoding="utf-8"))["encodings"]
              for subset, spec in specs.items()}

    taus = sorted({spec["threshold"] for spec in specs.values()})
    print(f"Mastic benchmark: {len(plan)} runs ({len(encodings)} encodings x {len(specs)} subsets "
          f"x {len(reps)} reps{'' if args.no_warmup else ', rep 0 = warm-up'}), "
          f"tau = {'/'.join(map(str, taus))} (T = "
          f"{' / '.join(str(bc.threshold_abs(s['rows'], s['threshold'])) for s in specs.values())}), "
          f"{args.threads} threads per server, ports {args.port_base}/{args.port_base + 1}")
    print(f"  results: {bc.relpath(results_file)}; configs and logs: {bc.relpath(RUN_DIR)}")
    print(f"  commit {env['commit']}{' (dirty)' if env['dirty'] else ''}, {env['cpu']}")
    if args.dry_run:
        for i, r in enumerate(plan, 1):
            print(f"  {i:3}. {r['encoding']:12} {r['subset']:5} rep {r['rep']}"
                  f"{' (warm-up)' if r['warmup'] else ''}  -n {specs[r['subset']]['rows']}")
        return 0

    t0 = time.perf_counter()
    try:
        wait_ports_free([args.port_base, args.port_base + 1], timeout=0)
        if not args.no_build:
            build()
        if not args.skip_keygen:
            print("\nClient micro-benchmark (A6):", flush=True)
            for enc in encodings:
                run_keygen(enc, configs[enc, "keygen"], env, results_dir)
        print(flush=True)
        durations = {}
        for i, run in enumerate(plan):
            spec = specs[run["subset"]]
            elapsed = time.perf_counter() - t0
            left = estimate_left(plan, i, durations, specs)
            print(f"[run {i + 1} of {len(plan)}] {run['encoding']} {run['subset']} rep {run['rep']}"
                  f"{' (warm-up)' if run['warmup'] else ''}, n = {spec['rows']}; "
                  f"elapsed {fmt_time(elapsed)}, left ~{fmt_time(left)}", flush=True)
            truth = truths[run["subset"]][run["encoding"]]
            rec = run_once(args, run, spec, configs[run["encoding"], run["subset"]], env,
                           results_file, truth)
            durations[run["encoding"], run["subset"]] = rec["run_total_s"]
            sc, pk = rec["self_check"], rec["peak_mb"]
            print(f"    online {rec['online_ms'] / 1e3:.2f} s, upload {rec['upload_ms'] / 1e3:.2f} s, "
                  f"driver {rec['driver_wall_s']:.1f} s, run {rec['run_total_s']:.1f} s; "
                  f"rounds {rec['rounds']['online']} online / {rec['rounds']['upload']} upload / "
                  f"{rec['rounds']['extra']} extra; peak memory {pk['server_0']:.0f} / "
                  f"{pk['server_1']:.0f} / {pk['driver']:.0f} MiB (server 0 / 1 / driver); "
                  f"self-check P {sc['precision']:.3f} R {sc['recall']:.3f} "
                  f"{'exact' if sc['exact'] else 'NOT exact'} ({sc['cells']} cells)", flush=True)
    except KeyboardInterrupt:
        print("\nInterrupted; servers stopped.", file=sys.stderr)
        return 130
    except RunError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 1
    print(f"\nDone: {len(plan)} runs in {fmt_time(time.perf_counter() - t0)}; "
          f"results in {bc.relpath(results_file)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
