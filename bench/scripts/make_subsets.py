#!/usr/bin/env python3
"""Make the benchmark's data subsets (A1): seeded shuffle, nested thirds, manifest with SHA-256.

    .\\.venv\\Scripts\\python.exe bench\\scripts\\make_subsets.py --source data\\mosaic\\sample_busiest_day.csv --dataset day --seed 20261107

Writes bench/data/<dataset>_1of3.csv, _2of3.csv, _3of3.csv (original header and columns, rows
unchanged, LF line endings) and bench/data/manifest.json. The subsets are generated once, here,
and copied to the Mosaic codebase, which checks them against the manifest.
"""

import argparse
import csv
import hashlib
import io
import json
import math
import platform
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COORD_COLUMNS = ("start_location_lat", "start_location_long")
PARTS = (("1of3", 1), ("2of3", 2), ("3of3", 3))


def relpath(path):
    path = Path(path).resolve()
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def has_coordinates(row, columns):
    """Both coordinates present and finite numbers."""
    try:
        return all(math.isfinite(float(row[i])) for i in columns)
    except (IndexError, ValueError):
        return False


def to_csv(header, rows):
    """Header and rows as CSV bytes with LF line endings."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", required=True, help="ride CSV, e.g. data/mosaic/sample_busiest_day.csv")
    parser.add_argument("--dataset", required=True, help="name used in the file names, e.g. day")
    parser.add_argument("--seed", required=True, type=int, help="shuffle seed, e.g. 20261107")
    parser.add_argument("--out-dir", default=str(ROOT / "bench" / "data"))
    args = parser.parse_args()

    # 1. Load the source.
    source = Path(args.source).resolve()
    raw = source.read_bytes()
    text = raw.decode("utf-8")
    if text.startswith("﻿"):
        raise SystemExit(f"{source}: starts with a byte-order mark; remove it first")
    reader = csv.reader(io.StringIO(text, newline=""))
    header = next(reader)
    missing = [c for c in COORD_COLUMNS if c not in header]
    if missing:
        raise SystemExit(f"{source}: no column {', '.join(missing)}")
    columns = [header.index(c) for c in COORD_COLUMNS]

    # 2. Drop rows with missing or non-numeric coordinates.
    rows, dropped = [], 0
    for row in reader:
        if has_coordinates(row, columns):
            rows.append(row)
        else:
            dropped += 1

    # The rows must be written back unchanged; only the line endings become LF. This fails if the
    # CSV writer would quote or reformat anything.
    source_lf = raw.replace(b"\r\n", b"\n")
    if not source_lf.endswith(b"\n"):
        source_lf += b"\n"
    if dropped == 0:
        if to_csv(header, rows) != source_lf:
            raise SystemExit("re-serialising the rows changes them; check the CSV's quoting")
        roundtrip = "rows re-serialise to the LF-normalised source byte for byte"
    else:
        roundtrip = "skipped: rows were dropped"

    # 3. Shuffle all rows.
    random.Random(args.seed).shuffle(rows)

    # 4.-5. Take the first round(N/3), round(2N/3) and N rows and write them.
    n = len(rows)
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    source_info = {"path": relpath(source), "sha256": sha256(raw), "sha256_lf": sha256(source_lf)}
    files, previous = [], b""
    for name, thirds in PARTS:
        size = round(n * thirds / 3)
        data = to_csv(header, rows[:size])
        body = data.split(b"\n", 1)[1]
        if not body.startswith(previous):
            raise SystemExit(f"{name} does not contain the smaller subset")
        previous = body
        path = out_dir / f"{args.dataset}_{name}.csv"
        path.write_bytes(data)
        files.append({
            "subset": name,
            "path": relpath(path),
            "rows": size,
            "sha256": sha256(data),
            "seed": args.seed,
            "source": source_info,
        })

    # 6. Manifest.
    manifest = {
        "dataset": args.dataset,
        "seed": args.seed,
        "method": "drop rows without finite coordinates; random.Random(seed).shuffle over the "
                  "remaining rows in file order; first round(N/3), round(2N/3) and N rows",
        "python": platform.python_version(),
        "line_endings": "LF",
        "source": {**source_info, "rows": n + dropped, "rows_kept": n, "rows_dropped": dropped},
        "files": files,
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")

    print(f"Source {relpath(source)}: {n + dropped} rows, {dropped} dropped, {n} kept")
    print(f"Round trip: {roundtrip}")
    print("Subsets are nested (each contains the smaller ones)")
    for f in files:
        print(f"  {f['path']}: {f['rows']} rows, sha256 {f['sha256']}")
    print(f"Wrote {relpath(manifest_path)}:")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
