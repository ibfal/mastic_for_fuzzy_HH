#!/usr/bin/env python3
"""Index the saved crawls for viz/index.html and build a single-file copy to share.

    python3 scripts/build_viz.py

Reads every viz/results/<run>.json written by the driver's --export (scripts/run_encodings.sh
does this for you) and the benchmark results in bench/results/ (bench/scripts/evaluate.py), and
writes
    viz/results/manifest.json   the runs index.html offers when served over http
    viz/results/bench.json      the benchmark summary for the page's benchmark section
    viz/dist/mastic-crawl.html  index.html with every run and the benchmark inlined
"""

import csv
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VIZ = ROOT / "viz"
RESULTS = VIZ / "results"
INLINE_TAG = re.compile(
    r'(<script id="inline-runs" type="application/json">)(.*?)(</script>)', re.S
)
INLINE_BENCH_TAG = re.compile(
    r'(<script id="inline-bench" type="application/json">)(.*?)(</script>)', re.S
)
BENCH = ROOT / "bench" / "results"


def number(text):
    """A CSV cell as int, float, bool or str."""
    if text in ("true", "false"):
        return text == "true"
    for kind in (int, float):
        try:
            return kind(text)
        except ValueError:
            pass
    return text


def steps(text):
    """"flp=42.2;level_0=6.0;..." as {"flp": 42.2, "level_0": 6.0, ...}."""
    return {k: float(v) for k, v in (part.split("=") for part in text.split(";") if part)}


def load_bench():
    """The benchmark summary (one row per encoding x subset), the per-repetition online times,
    and the run environment; None if bench/results/ has no summary."""
    summary_path = BENCH / "summary_mastic.csv"
    if not summary_path.exists():
        return None
    rows = []
    with open(summary_path, newline="") as f:
        for raw in csv.DictReader(f):
            row = {k: number(v) for k, v in raw.items()}
            for key in ("online_steps_median_ms", "online_steps_min_ms", "online_steps_max_ms"):
                row[key] = steps(raw[key])
            for key in ("revealed_per_level", "frontier_per_level"):
                row[key] = [int(x) for x in raw[key].split(";") if x]
            rows.append(row)
    runs, env = [], None
    raw_path = BENCH / "raw" / "mastic.jsonl"
    if raw_path.exists():
        for line in raw_path.read_text().splitlines():
            if not line.strip():
                continue
            run = json.loads(line)
            env = env or run.get("env")
            runs.append({
                "encoding": run["encoding"],
                "subset": run["subset"],
                "rep": run["rep"],
                "warmup": run["warmup"],
                "online_ms": run["phases"]["online_ms"],
            })
    return {"source": "bench/results/summary_mastic.csv", "rows": rows, "runs": runs, "env": env}


def encoding_kind(encoding):
    """"mosaic_grid", or the single key of {"geohash": {...}} and the like."""
    if isinstance(encoding, str):
        return encoding
    if isinstance(encoding, dict) and len(encoding) == 1:
        return next(iter(encoding))
    return "unknown"


def main():
    runs = {}
    for path in sorted(RESULTS.glob("*.json")):
        if path.name == "manifest.json":
            continue
        with open(path) as f:
            run = json.load(f)
        if run.get("format") != "mastic-crawl/1":
            print(f"skipping {path.name}: not a driver --export file")
            continue
        runs[path.stem] = run

    manifest = {
        "runs": [
            {
                "name": name,
                "file": f"{name}.json",
                "dataset": run.get("dataset"),
                "encoding": encoding_kind(run.get("encoding")),
            }
            for name, run in runs.items()
        ]
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    with open(RESULTS / "manifest.json", "w") as f:
        json.dump(manifest, f, indent=1)
    bench = load_bench()
    with open(RESULTS / "bench.json", "w") as f:
        json.dump(bench, f)

    page = (VIZ / "index.html").read_text()
    # "</" inside a script element would end it early.
    data = json.dumps(runs, separators=(",", ":")).replace("</", "<\\/")
    page, n = INLINE_TAG.subn(lambda m: m.group(1) + data + m.group(3), page)
    if n != 1:
        raise SystemExit('viz/index.html has no <script id="inline-runs"> element')
    bench_data = json.dumps(bench, separators=(",", ":")).replace("</", "<\\/")
    page, n = INLINE_BENCH_TAG.subn(lambda m: m.group(1) + bench_data + m.group(3), page)
    if n != 1:
        raise SystemExit('viz/index.html has no <script id="inline-bench"> element')
    out = VIZ / "dist" / "mastic-crawl.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page)

    print(f"{len(runs)} runs: {', '.join(runs) or 'none'}")
    print(f"benchmark: {len(bench['rows'])} summary rows, {len(bench['runs'])} runs"
          if bench else "benchmark: none (no bench/results/summary_mastic.csv)")
    print(f"wrote {RESULTS.relative_to(ROOT)}/manifest.json and {out.relative_to(ROOT)}"
          f" ({out.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
