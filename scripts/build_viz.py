#!/usr/bin/env python3
"""Index the saved crawls for viz/index.html and build a single-file copy to share.

    python3 scripts/build_viz.py

Reads every viz/results/<run>.json written by the driver's --export (scripts/run_encodings.sh
does this for you) and writes
    viz/results/manifest.json   the runs index.html offers when served over http
    viz/dist/mastic-crawl.html  index.html with every run inlined; opens straight from disk
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VIZ = ROOT / "viz"
RESULTS = VIZ / "results"
INLINE_TAG = re.compile(
    r'(<script id="inline-runs" type="application/json">)(.*?)(</script>)', re.S
)


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

    page = (VIZ / "index.html").read_text()
    # "</" inside a script element would end it early.
    data = json.dumps(runs, separators=(",", ":")).replace("</", "<\\/")
    page, n = INLINE_TAG.subn(lambda m: m.group(1) + data + m.group(3), page)
    if n != 1:
        raise SystemExit('viz/index.html has no <script id="inline-runs"> element')
    out = VIZ / "dist" / "mastic-crawl.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page)

    print(f"{len(runs)} runs: {', '.join(runs) or 'none'}")
    print(f"wrote {RESULTS.relative_to(ROOT)}/manifest.json and {out.relative_to(ROOT)}"
          f" ({out.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
