#!/usr/bin/env python3
"""Record the SHA-256 of the raw City of Melbourne downloads in apps/parking/raw_manifest.json.

The raw files are git-ignored (too large); the manifest is committed so later steps can check
that they run on exactly these bytes. Files listed in SOURCES but missing from raw/ are reported
and left out of the manifest.

    .\\.venv\\Scripts\\python.exe -I apps\\parking\\scripts\\hash_raw.py
    .\\.venv\\Scripts\\python.exe -I apps\\parking\\scripts\\hash_raw.py --check
"""

import argparse
import datetime
import hashlib
import json
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
RAW = APP / "raw"
MANIFEST = APP / "raw_manifest.json"

PORTAL = "https://data.melbourne.vic.gov.au/explore/dataset/"
SOURCES = {
    "On-street_Car_Parking_Sensor_Data_-_2019.csv": {
        "dataset": "On-street Car Parking Sensor Data - 2019",
        "url": PORTAL + "on-street-car-parking-sensor-data-2019/",
        "role": "parking events (sensor status intervals)",
    },
    "on-street-parking-bays.csv": {
        "dataset": "On-street Parking Bays",
        "url": PORTAL + "on-street-parking-bays/",
        "role": "bay locations and road segments",
    },
    "on-street-parking-bay-sensors.csv": {
        "dataset": "On-street Parking Bay Sensors",
        "url": PORTAL + "on-street-parking-bay-sensors/",
        "role": "current sensors: kerbside ID with coordinates (join candidate)",
    },
}
LICENSE = "CC BY 4.0, City of Melbourne"


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def build():
    files = {}
    for name, meta in SOURCES.items():
        path = RAW / name
        if not path.exists():
            print(f"missing (skipped): {path.relative_to(APP.parents[1])}  <- {meta['url']}")
            continue
        print(f"hashing {name} ({path.stat().st_size:,} bytes) ...", flush=True)
        files[name] = {
            **meta,
            "license": LICENSE,
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
            "file_mtime": datetime.datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
        }
        print(f"  {files[name]['sha256']}")
    return files


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="verify raw/ against the manifest instead of writing it")
    args = ap.parse_args()

    if args.check:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        bad = 0
        for name, entry in manifest["files"].items():
            got = sha256_file(RAW / name) if (RAW / name).exists() else None
            ok = got == entry["sha256"]
            bad += not ok
            print(f"{'OK  ' if ok else 'FAIL'} {name}")
        sys.exit(1 if bad else 0)

    manifest = {
        "description": "SHA-256 of the raw downloads in apps/parking/raw/ (git-ignored).",
        "created": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "files": build(),
    }
    MANIFEST.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {MANIFEST.relative_to(APP.parents[1])}")


if __name__ == "__main__":
    main()
