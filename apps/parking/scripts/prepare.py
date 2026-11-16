#!/usr/bin/env python3
"""P2 preparation pipeline (apps/parking/PARKING_PLAN.md §4): Parquet cache -> stays -> cleaning ->
location join -> chosen day -> report files, benchmark set and application set.

Stitching, cleaning and the join are the P1 code (explore.py), so every count matches
reports/01-exploration.md.

    .\\.venv\\Scripts\\python.exe -I apps\\parking\\scripts\\prepare.py              # join 2c, all outputs
    .\\.venv\\Scripts\\python.exe -I apps\\parking\\scripts\\prepare.py --join 2b    # report files in data/join2b/

Outputs (apps/parking/data/, LF line endings, no BOM):
  parking_day_snapshot.csv            design B, snapshot (default) = application set
  parking_day_snapshot_wHH-HH.csv     the same, one file per 4-hour window (Mosaic)
  parking_day_overlap.csv             design B, overlap
  parking_day_arrivals.csv            design A
  parking_day_sample.csv              seeded sample of 21,581 snapshot reports (source of the subsets)
  parking_day_{1of3,2of3,3of3}.csv    nested thirds, via bench/scripts/make_subsets.py
  manifest.json                       make_subsets.py's manifest of the thirds
  prepare_manifest.json               step counts; rows and SHA-256 of every file written here
"""

import argparse
import csv
import datetime as dt
import io
import random
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # -I drops the script's folder
import parking_common as pc  # noqa: E402
import explore  # noqa: E402

# The rows of the stays table that the reports are built from (clean, located).
REP_STAYS_SQL = """
CREATE OR REPLACE TABLE rep_stays AS
SELECT v.StreetMarker AS street_marker, printf('%s-%05d', v.StreetMarker, v.stay) AS stay_id,
       km.lat, km.lon, km.segment, km.join_method,
       coalesce(bd.flagged, false) AS fault_flag, v.ArrivalTime AS arrival, v.DepartureTime AS departure,
       v.day
FROM vp v
JOIN keymap km USING (key)
LEFT JOIN bay_day bd ON bd.StreetMarker = v.StreetMarker AND bd.day = v.day
WHERE v.clean
"""


def build_keymap(con, ctx, join):
    """Location of every sensor identity under the chosen join, rounded to 7 decimals so that every
    later step (reports, capacity, occupancy) uses the identical coordinates."""
    J = ctx.memo("joins", explore.joins)
    methods = J["methods"]
    m = methods[pc.JOINS[join]]["map"]
    exact = methods["2a segment, exact (street + both cross streets)"]["map"]
    rows = [(k, round(lat, 7), round(lon, 7), seg.removeprefix("seg:"),
             "2a-exact" if k in exact else f"{join}-partial") for k, (lat, lon, seg) in m.items()]
    con.execute("CREATE OR REPLACE TABLE keymap (key UBIGINT, lat DOUBLE, lon DOUBLE, segment VARCHAR, "
                "join_method VARCHAR)")
    con.executemany("INSERT INTO keymap VALUES (?, ?, ?, ?, ?)", rows)
    return J


def build_bay_day(con):
    con.execute(f"""CREATE OR REPLACE TABLE bay_day AS
        SELECT StreetMarker, day, count(*) AS n, count(*) FILTER (WHERE DurationMinutes < 2) AS n_blip,
               count(*) > {pc.FAULT_MAX_STAYS}
                 OR (count(*) >= {pc.FAULT_MIN_STAYS_FOR_BLIP_SHARE}
                     AND count(*) FILTER (WHERE DurationMinutes < 2) > {pc.FAULT_BLIP_SHARE} * count(*)) AS flagged
        FROM vp GROUP BY ALL""")


def report_queries(day):
    D = day.isoformat()
    D1 = (day + dt.timedelta(days=1)).isoformat()
    cols = """lat, lon, hour, stay_id, street_marker, segment, join_method, fault_flag::INTEGER,
              strftime(arrival, '%Y-%m-%d %H:%M:%S'), strftime(departure, '%Y-%m-%d %H:%M:%S')"""
    overlapping = f"arrival < TIMESTAMP '{D1}' AND departure > TIMESTAMP '{D}'"
    return {
        # Design B, snapshot: hour h if arrival <= D h:30 < departure; at most one report per
        # (bay, hour) (D1): where overlapping sensor intervals give a bay two stays covering h:30,
        # the stay that arrived first is kept.
        "snapshot": f"""SELECT {cols} FROM (
            SELECT r.*, h.hour FROM rep_stays r, (SELECT range AS hour FROM range({pc.HOURS})) h
            WHERE {overlapping}
              AND r.arrival <= TIMESTAMP '{D}' + to_hours(h.hour) + to_minutes({pc.SNAPSHOT_MINUTE})
              AND r.departure > TIMESTAMP '{D}' + to_hours(h.hour) + to_minutes({pc.SNAPSHOT_MINUTE})
            QUALIFY row_number() OVER (PARTITION BY r.street_marker, h.hour ORDER BY r.arrival, r.stay_id) = 1)
            ORDER BY stay_id, hour""",
        # The same before deduplication, only counted (prepare_manifest.json).
        "snapshot_raw_count": f"""SELECT count(*) FROM rep_stays r, (SELECT range AS hour FROM range({pc.HOURS})) h
            WHERE {overlapping}
              AND r.arrival <= TIMESTAMP '{D}' + to_hours(h.hour) + to_minutes({pc.SNAPSHOT_MINUTE})
              AND r.departure > TIMESTAMP '{D}' + to_hours(h.hour) + to_minutes({pc.SNAPSHOT_MINUTE})""",
        # Design B, overlap: every hour of D the stay overlaps (P1's definition).
        "overlap": f"""SELECT {cols} FROM (
            SELECT *, hour(h) AS hour FROM (
              SELECT *, unnest(generate_series(
                  greatest(date_trunc('hour', arrival), TIMESTAMP '{D}'),
                  least(date_trunc('hour', departure - INTERVAL 1 MICROSECOND), TIMESTAMP '{D}' + INTERVAL 23 HOUR),
                  INTERVAL 1 HOUR)) AS h
              FROM rep_stays WHERE {overlapping}))
            ORDER BY stay_id, hour""",
        # Design A: one report per arrival on D, hour = arrival hour.
        "arrivals": f"""SELECT {cols} FROM (SELECT *, hour(arrival) AS hour FROM rep_stays WHERE day = DATE '{D}')
            ORDER BY stay_id, hour""",
    }


def to_csv_bytes(header, rows):
    """Same serialisation as bench/scripts/make_subsets.py (csv.writer, LF)."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def write_csv(path, rows, files):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = to_csv_bytes(pc.REPORT_COLUMNS, rows)
    path.write_bytes(data)
    files[pc.relpath(path)] = {"rows": len(rows), "sha256": pc.sha256_file(path)}
    print(f"  {pc.relpath(path)}: {len(rows):,} rows")
    return data


def main():
    pc.configure_stdout()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--join", choices=sorted(pc.JOINS), default=pc.DEFAULT_JOIN)
    args = ap.parse_args()
    t0 = time.time()

    con = explore.connect()
    explore.build_cache(con)
    explore.register_views(con)
    ctx = explore.Ctx(con)
    build_keymap(con, ctx, args.join)
    build_bay_day(con)
    con.execute(REP_STAYS_SQL)

    one = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
    D = pc.DAY.isoformat()
    steps = {
        "sensor rows": one("SELECT count(*) FROM s"),
        "vehicle-present rows": one("SELECT count(*) FROM s WHERE VehiclePresent"),
        "stays": one("SELECT count(*) FROM stays"),
        "stays arriving in 2019": one("SELECT count(*) FROM vp"),
        "clean stays": one("SELECT count(*) FROM vp WHERE clean"),
        f"clean stays joined ({args.join})": one("SELECT count(*) FROM rep_stays"),
        "clean joined stays arriving on the day": one(f"SELECT count(*) FROM rep_stays WHERE day = DATE '{D}'"),
        "clean joined stays overlapping the day": one(
            f"""SELECT count(*) FROM rep_stays WHERE arrival < TIMESTAMP '{D}' + INTERVAL 1 DAY
                AND departure > TIMESTAMP '{D}'"""),
        "fault-flagged clean joined stays overlapping the day": one(
            f"""SELECT count(*) FROM rep_stays WHERE fault_flag AND arrival < TIMESTAMP '{D}' + INTERVAL 1 DAY
                AND departure > TIMESTAMP '{D}'"""),
    }

    out_dir = pc.DATA if args.join == pc.DEFAULT_JOIN else pc.DATA / f"join{args.join}"
    files = {}
    print(f"Writing report files for {D} (join {args.join}):")
    reports = {}
    queries = report_queries(pc.DAY)
    raw_snapshot = con.execute(queries.pop("snapshot_raw_count")).fetchone()[0]
    for kind, sql in queries.items():
        rows = [list(r) for r in con.execute(sql).fetchall()]
        reports[kind] = rows
        write_csv(pc.report_path(kind, args.join), rows, files)
    steps["design B snapshot reports before deduplication (day)"] = raw_snapshot
    steps["second reports of a bay in the same hour removed (D1)"] = raw_snapshot - len(reports["snapshot"])
    steps["design B snapshot reports (day)"] = len(reports["snapshot"])
    steps["design B overlap reports (day)"] = len(reports["overlap"])
    steps["design A reports (day)"] = len(reports["arrivals"])

    for a, b in pc.WINDOWS:
        rows = [r for r in reports["snapshot"] if a <= r[2] < b]
        write_csv(pc.report_path("snapshot", args.join, (a, b)), rows, files)

    bench = None
    if args.join == pc.DEFAULT_JOIN:
        # Benchmark set: seeded sample of exactly BENCH_N snapshot reports, in file order, then the
        # benchmark's own subset procedure (shuffle with the same seed, nested thirds, manifest).
        snap = reports["snapshot"]
        idx = sorted(random.Random(pc.SEED).sample(range(len(snap)), pc.BENCH_N))
        sample_path = pc.DATA / f"{pc.DATASET}_sample.csv"
        write_csv(sample_path, [snap[i] for i in idx], files)
        cmd = [sys.executable, "-I", str(pc.MAKE_SUBSETS), "--source", str(sample_path),
               "--dataset", pc.DATASET, "--seed", str(pc.SEED), "--out-dir", str(pc.DATA)]
        print("Running", " ".join(pc.relpath(c) if c.endswith(".py") else c for c in cmd[2:]))
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        if res.returncode != 0:
            print(res.stdout, res.stderr)
            raise SystemExit("make_subsets.py failed")
        print("\n".join(line for line in res.stdout.splitlines()[:7]))
        for s in pc.SUBSETS:
            p = pc.subset_path(s)
            with open(p, newline="", encoding="utf-8") as f:
                n = sum(1 for _ in f) - 1
            files[pc.relpath(p)] = {"rows": n, "sha256": pc.sha256_file(p)}
        bench = {"source": pc.relpath(sample_path), "n": pc.BENCH_N, "seed": pc.SEED,
                 "sample": "random.Random(seed).sample(range(N_snapshot), n), kept in file order",
                 "subsets": "bench/scripts/make_subsets.py (unchanged)", "manifest": pc.relpath(pc.DATA / "manifest.json")}

    manifest = {
        "description": "P2 preparation outputs (apps/parking/scripts/prepare.py)",
        "day": D,
        "join": pc.JOINS[args.join],
        "cleaning": f"{pc.CLEAN_MIN_S // 60} min <= duration <= {pc.CLEAN_MAX_S // 3600} h",
        "stitching": f"contiguous vehicle-present rows of a StreetMarker, gap <= {pc.STITCH_GAP_S} s",
        "fault_flag": f"bay-day (StreetMarker x arrival date) with > {pc.FAULT_MAX_STAYS} stays, or >= "
                      f"{pc.FAULT_MIN_STAYS_FOR_BLIP_SHARE} stays of which > {pc.FAULT_BLIP_SHARE:.0%} under 2 min",
        "designs": {
            "snapshot": f"one report per hour h if arrival <= {D} h:{pc.SNAPSHOT_MINUTE} < departure; at most one "
                        "per (bay, hour), the stay that arrived first (D1)",
            "overlap": "one report per hour of the day the stay overlaps",
            "arrivals": "one report per stay arriving on the day, hour = arrival hour",
        },
        "columns": pc.REPORT_COLUMNS,
        "steps": steps,
        "benchmark": bench,
        "files": files,
    }
    pc.write_json(out_dir / "prepare_manifest.json", manifest)
    print("Steps:")
    for k, v in steps.items():
        print(f"  {k:55} {v:>12,}")
    print(f"wrote {pc.relpath(out_dir / 'prepare_manifest.json')} in {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
