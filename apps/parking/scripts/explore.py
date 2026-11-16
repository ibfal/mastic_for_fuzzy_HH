#!/usr/bin/env python3
"""P1 exploration of the City of Melbourne 2019 parking data (apps/parking/PARKING_PLAN.md).

Exploration only: no pipeline output. Every number in reports/01-exploration.md comes from this
script; its raw tables go to stdout and to reports/01-exploration-output.md.

The 7.45 GB sensor CSV is scanned once into a git-ignored Parquet cache (raw/_cache/), and every
step queries that cache with DuckDB.

    .\\.venv\\Scripts\\python.exe -I apps\\parking\\scripts\\explore.py --step all
    .\\.venv\\Scripts\\python.exe -I apps\\parking\\scripts\\explore.py --step joins

Steps: cache, profile, coverage, faults, joins, days, dist, encodings (all = in this order).
"""

import argparse
import datetime as dt
import math
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import duckdb
import h3

REPO = Path(__file__).resolve().parents[3]
APP = REPO / "apps" / "parking"
RAW = APP / "raw"
CACHE = RAW / "_cache"
SENSOR_CSV = RAW / "On-street_Car_Parking_Sensor_Data_-_2019.csv"
BAYS_CSV = RAW / "on-street-parking-bays.csv"
BAY_SENSORS_CSV = RAW / "on-street-parking-bay-sensors.csv"
BAY_SENSORS_URL = ("https://data.melbourne.vic.gov.au/api/explore/v2.1/catalog/datasets/"
                   "on-street-parking-bay-sensors/exports/csv")
SENSOR_PQ = CACHE / "sensor_2019.parquet"

OUTPUT_MD = APP / "reports" / "01-exploration-output.md"

# The bench checker encodes exactly as src/geo.rs does; reuse its geohash and Morton code.
sys.path.insert(0, str(REPO / "scripts"))
import check_heavy_hitters as chh  # noqa: E402

TS_FMT = "%m/%d/%Y %I:%M:%S %p"

# Victorian public holidays 2019 (Public Holidays Act 1993; AFL Grand Final Friday and Melbourne
# Cup Day apply in metropolitan Melbourne). Easter Saturday/Sunday fall on weekends anyway.
HOLIDAYS_2019 = {
    dt.date(2019, 1, 1): "New Year's Day",
    dt.date(2019, 1, 28): "Australia Day (observed)",
    dt.date(2019, 3, 11): "Labour Day",
    dt.date(2019, 4, 19): "Good Friday",
    dt.date(2019, 4, 22): "Easter Monday",
    dt.date(2019, 4, 25): "Anzac Day",
    dt.date(2019, 6, 10): "Queen's Birthday",
    dt.date(2019, 9, 27): "AFL Grand Final Friday",
    dt.date(2019, 11, 5): "Melbourne Cup Day",
    dt.date(2019, 12, 25): "Christmas Day",
    dt.date(2019, 12, 26): "Boxing Day",
}

# Large city events, from memory (UNVERIFIED; the report says so). Inclusive date ranges.
KNOWN_EVENTS_2019 = [
    (dt.date(2019, 1, 14), dt.date(2019, 1, 27), "Australian Open"),
    (dt.date(2019, 3, 8), dt.date(2019, 3, 11), "Moomba"),
    (dt.date(2019, 3, 14), dt.date(2019, 3, 17), "Australian Grand Prix"),
    (dt.date(2019, 9, 6), dt.date(2019, 9, 28), "AFL finals"),
    (dt.date(2019, 11, 2), dt.date(2019, 11, 9), "Melbourne Cup week"),
    (dt.date(2019, 12, 1), dt.date(2019, 12, 24), "Christmas shopping"),
]

# Fault rule for a (device, day): implausibly many stays, or mostly sensor blips.
FAULT_MAX_EVENTS = 40
FAULT_MIN_EVENTS_FOR_BLIP_SHARE = 10
FAULT_BLIP_SHARE = 0.5
# Normal coverage: active devices within this fraction of the regular-weekday median.
COVERAGE_TOL = 0.05
# Contiguous vehicle-present rows of a bay at most this far apart are one stay.
STITCH_GAP_S = 1
# Join used for the day statistics: exact segment match, or a partial match (one cross street
# differs) only when a single bay-file segment qualifies. 2b also averages ambiguous neighbours.
RECOMMENDED_JOIN = "2c"

TAUS = [0.0005, 0.001, 0.0025, 0.005, 0.01, 0.02, 0.03, 0.05, 0.10, 0.15, 0.20]
DELTAS = [3, 5]
WINDOWS = [(h, h + 4) for h in range(0, 24, 4)]
SCALE = 1000  # 0.001 degree cells

_out = []


def emit(text=""):
    print(text, flush=True)
    _out.append(text)


def heading(text):
    emit()
    emit(f"## {text}")
    emit()


def table(cols, rows, fmt=None):
    """Markdown table; numbers get thousands separators."""
    def cell(v):
        if isinstance(v, bool) or v is None:
            return str(v)
        if isinstance(v, int):
            return f"{v:,}"
        if isinstance(v, float):
            return f"{v:,.4f}" if abs(v) < 1000 else f"{v:,.1f}"
        return str(v).replace("|", "\\|")
    emit("| " + " | ".join(cols) + " |")
    emit("|" + "|".join("---" for _ in cols) + "|")
    for r in rows:
        emit("| " + " | ".join(cell(v) for v in r) + " |")
    emit()


def pct(a, b):
    return f"{100.0 * a / b:.2f}%" if b else "n/a"


def sql_path(p):
    return str(p).replace("\\", "/")


def connect():
    con = duckdb.connect()
    con.execute("SET preserve_insertion_order = false")
    return con


# --- cache -----------------------------------------------------------------------------------


def build_cache(con, force=False):
    """One scan of the CSV into Parquet. Times are parsed as naive TIMESTAMPs (the file carries no
    offset; P1 shows they are Melbourne wall-clock time). Raw strings are kept only where parsing
    failed, so failures can be counted and shown."""
    if SENSOR_PQ.exists() and not force:
        return
    CACHE.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    emit(f"building {SENSOR_PQ.relative_to(REPO)} from the CSV (one pass) ...")
    con.execute(f"""
        COPY (
          SELECT
            TRY_CAST(DeviceId AS INTEGER) AS DeviceId,
            try_strptime(ArrivalTime, '{TS_FMT}') AS ArrivalTime,
            try_strptime(DepartureTime, '{TS_FMT}') AS DepartureTime,
            CASE WHEN try_strptime(ArrivalTime, '{TS_FMT}') IS NULL THEN ArrivalTime END AS ArrivalTimeBad,
            CASE WHEN try_strptime(DepartureTime, '{TS_FMT}') IS NULL THEN DepartureTime END AS DepartureTimeBad,
            TRY_CAST(replace(DurationMinutes, ',', '') AS INTEGER) AS DurationMinutes,
            CASE WHEN TRY_CAST(replace(DurationMinutes, ',', '') AS INTEGER) IS NULL
                   OR contains(DurationMinutes, ',') THEN DurationMinutes END AS DurationMinutesRaw,
            StreetMarker, TRY_CAST(SignPlateID AS INTEGER) AS SignPlateID, Sign, AreaName,
            TRY_CAST(StreetId AS INTEGER) AS StreetId, StreetName,
            TRY_CAST(BetweenStreet1ID AS INTEGER) AS BetweenStreet1ID, BetweenStreet1,
            TRY_CAST(BetweenStreet2ID AS INTEGER) AS BetweenStreet2ID, BetweenStreet2,
            TRY_CAST(SideOfStreet AS INTEGER) AS SideOfStreet, SideOfStreetCode, SideName,
            TRY_CAST(BayId AS INTEGER) AS BayId,
            TRY_CAST(InViolation AS BOOLEAN) AS InViolation,
            TRY_CAST(VehiclePresent AS BOOLEAN) AS VehiclePresent
          FROM read_csv('{sql_path(SENSOR_CSV)}', all_varchar = true, header = true,
                        quote = '"', escape = '"', strict_mode = true)
        ) TO '{sql_path(SENSOR_PQ)}' (FORMAT parquet, COMPRESSION zstd)
    """)
    emit(f"cache built in {time.time() - t0:.0f} s, {SENSOR_PQ.stat().st_size / 1e6:,.0f} MB")




def register_views(con):
    """Views every step uses."""
    con.execute(f"CREATE OR REPLACE VIEW s AS SELECT * FROM read_parquet('{sql_path(SENSOR_PQ)}')")
    # The bay file is read as text: two kerbside IDs are street markers ("7568N").
    con.execute(f"""CREATE OR REPLACE TABLE bays AS
        SELECT TRY_CAST(roadsegmentid AS INTEGER) AS roadsegmentid, kerbsideid,
               roadsegmentdescription, CAST(latitude AS DOUBLE) AS latitude,
               CAST(longitude AS DOUBLE) AS longitude, latitude AS latitude_text,
               longitude AS longitude_text, CAST(lastupdated AS DATE) AS lastupdated
        FROM read_csv('{sql_path(BAYS_CSV)}', header = true, all_varchar = true)""")
    # Rows tile each bay's timeline, and the sensor system splits one occupancy into several
    # vehicle-present rows at midnight and at restriction boundaries (07:30, 18:30, ...). A real
    # departure always leaves a vehicle-absent row in between, so contiguous vehicle-present rows
    # of a bay (gap <= 1 s) are stitched into one stay. All event counts below are stays.
    con.execute(f"""CREATE OR REPLACE TABLE stays AS
        WITH r AS (
          SELECT StreetMarker, DeviceId, BayId, StreetName, BetweenStreet1, BetweenStreet2,
                 ArrivalTime, DepartureTime, VehiclePresent,
                 lag(DepartureTime) OVER w AS prev_dep, lag(VehiclePresent) OVER w AS prev_vp
          FROM s WINDOW w AS (PARTITION BY StreetMarker ORDER BY ArrivalTime, DepartureTime, VehiclePresent, DeviceId)),
        v AS (
          SELECT *, NOT coalesce(prev_vp AND date_diff('second', prev_dep, ArrivalTime) BETWEEN 0 AND {STITCH_GAP_S},
                                 false) AS new_stay
          FROM r WHERE VehiclePresent),
        g AS (
          SELECT *, sum(new_stay::INTEGER) OVER (PARTITION BY StreetMarker ORDER BY ArrivalTime, DepartureTime, VehiclePresent, DeviceId
                                                 ROWS UNBOUNDED PRECEDING) AS stay
          FROM v)
        SELECT StreetMarker, stay, arg_min(DeviceId, ArrivalTime) AS DeviceId, any_value(BayId) AS BayId,
               any_value(StreetName) AS StreetName, any_value(BetweenStreet1) AS BetweenStreet1,
               any_value(BetweenStreet2) AS BetweenStreet2, min(ArrivalTime) AS ArrivalTime,
               max(DepartureTime) AS DepartureTime, count(*) AS n_rows
        FROM g GROUP BY StreetMarker, stay""")
    # Stays arriving in 2019, with the plan's cleaning rule as a flag (2 min <= duration <= 24 h).
    # key = the sensor's identity columns.
    con.execute("""CREATE OR REPLACE VIEW vp AS
        SELECT *, CAST(ArrivalTime AS DATE) AS day,
               (date_diff('second', ArrivalTime, DepartureTime) // 60)::INTEGER AS DurationMinutes,
               coalesce(date_diff('second', ArrivalTime, DepartureTime) BETWEEN 120 AND 86400, false) AS clean,
               hash(DeviceId, StreetMarker, BayId, StreetName, BetweenStreet1, BetweenStreet2) AS key
        FROM stays
        WHERE year(ArrivalTime) = 2019""")


class Ctx:
    """Shared state between steps; each dependency is computed once, on first use."""

    def __init__(self, con):
        self.con = con
        self._memo = {}

    def q(self, sql, params=None):
        return self.con.execute(sql, params or []).fetchall()

    def one(self, sql, params=None):
        return self.con.execute(sql, params or []).fetchone()

    def memo(self, name, fn):
        if name not in self._memo:
            self._memo[name] = fn(self)
        return self._memo[name]


def quantiles(ctx, expr, source, where="true"):
    qs = [0.01, 0.05, 0.25, 0.5, 0.75, 0.9, 0.95, 0.99]
    sel = ", ".join(f"quantile_disc({expr}, {p})" for p in qs)
    row = ctx.one(f"SELECT {sel}, min({expr}), max({expr}), avg({expr}) FROM {source} WHERE {where}")
    return dict(zip(["p1", "p5", "p25", "p50", "p75", "p90", "p95", "p99", "min", "max", "mean"], row))


def qsorted(vals, p):
    """Lower empirical quantile of a sorted list."""
    return vals[min(len(vals) - 1, int(p * len(vals)))]


# --- profile ---------------------------------------------------------------------------------


def step_profile(ctx):
    heading("1. Sensor file: columns and types")
    sniffed = dict(ctx.q(f"""SELECT column_name, column_type FROM (DESCRIBE SELECT * FROM
        read_csv('{sql_path(SENSOR_CSV)}', quote = '"', escape = '"'))"""))
    cached = dict(ctx.q("SELECT column_name, column_type FROM (DESCRIBE SELECT * FROM s)"))
    n = ctx.one("SELECT count(*) FROM s")[0]
    nulls = ctx.one("SELECT " + ", ".join(f'count(*) - count("{c}")' for c in sniffed) + " FROM s")
    rows = [(col, typ, cached.get(col, "-"), k, pct(k, n)) for (col, typ), k in zip(sniffed.items(), nulls)]
    table(["column (CSV header)", "DuckDB sniffed type", "type in cache", "missing", "missing %"], rows)
    emit(f"Rows: **{n:,}**. The header starts with a UTF-8 BOM; values are comma-separated and "
         f"quoted with `\"` only where needed.")
    raw_dur = ctx.one("SELECT count(*), count(DurationMinutes) FROM s WHERE DurationMinutesRaw IS NOT NULL")
    emit(f"`DurationMinutes` written with a thousands separator (e.g. \"1,440\") or unparseable: {raw_dur[0]:,} rows "
         f"({raw_dur[1]:,} parse after removing the comma).")
    bad = ctx.one("SELECT count(ArrivalTimeBad), count(DepartureTimeBad) FROM s")
    emit(f"Timestamps that fail `{TS_FMT}`: arrival {bad[0]:,}, departure {bad[1]:,}.")

    heading("2. Sensor file: time range, status columns, durations")
    r = ctx.one("""SELECT min(ArrivalTime), max(ArrivalTime), min(DepartureTime), max(DepartureTime),
        count(*) FILTER (WHERE year(ArrivalTime) <> 2019), count(*) FILTER (WHERE year(DepartureTime) <> 2019),
        count(*) FILTER (WHERE DepartureTime < ArrivalTime), count(*) FILTER (WHERE DepartureTime = ArrivalTime)
        FROM s""")
    table(["min arrival", "max arrival", "min departure", "max departure", "arrival not 2019",
           "departure not 2019", "departure < arrival", "departure = arrival"], [r])
    rows = ctx.q("SELECT VehiclePresent, InViolation, count(*) FROM s GROUP BY ALL ORDER BY ALL")
    table(["VehiclePresent", "InViolation", "rows"], rows)

    rows = []
    for label, where in [("all rows", "true"), ("VehiclePresent = true", "VehiclePresent"),
                         ("VehiclePresent = false", "NOT VehiclePresent")]:
        d = quantiles(ctx, "DurationMinutes", "s", where)
        cnt = ctx.one(f"""SELECT count(*), count(*) FILTER (WHERE DurationMinutes < 2),
            count(*) FILTER (WHERE DurationMinutes > 1440) FROM s WHERE {where}""")
        rows.append((label, cnt[0], d["p1"], d["p5"], d["p25"], d["p50"], d["p75"], d["p95"], d["p99"],
                     d["max"], round(d["mean"], 1), pct(cnt[1], cnt[0]), pct(cnt[2], cnt[0])))
    emit("`DurationMinutes`:")
    table(["rows", "n", "p1", "p5", "p25", "p50", "p75", "p95", "p99", "max", "mean", "< 2 min", "> 24 h"], rows)
    rows = ctx.q("""SELECT CASE WHEN d BETWEEN -1 AND 1 THEN d::VARCHAR ELSE 'other' END AS k, count(*)
        FROM (SELECT DurationMinutes - floor(date_diff('second', ArrivalTime, DepartureTime) / 60)::INT AS d FROM s)
        GROUP BY 1 ORDER BY 1""")
    emit("`DurationMinutes` minus ⌊(departure − arrival) / 60 s⌋:")
    table(["difference", "rows"], rows)

    heading("3. Sensor file: identifiers")
    r = ctx.one("""SELECT count(DISTINCT DeviceId), count(DISTINCT StreetMarker), count(DISTINCT BayId),
        count(DISTINCT StreetId), count(DISTINCT (StreetName, BetweenStreet1, BetweenStreet2)),
        count(DISTINCT (StreetId, BetweenStreet1ID, BetweenStreet2ID)), count(DISTINCT AreaName),
        count(DISTINCT SignPlateID), min(DeviceId) || '–' || max(DeviceId), min(BayId) || '–' || max(BayId) FROM s""")
    table(["DeviceId", "StreetMarker", "BayId", "StreetId", "segments (names)", "segments (IDs)", "AreaName",
           "SignPlateID", "DeviceId range", "BayId range"], [r])
    r = ctx.one("""SELECT
        (SELECT count(*) FROM (SELECT StreetMarker FROM s GROUP BY 1 HAVING count(DISTINCT BayId) > 1)),
        (SELECT count(*) FROM (SELECT BayId FROM s GROUP BY 1 HAVING count(DISTINCT StreetMarker) > 1)),
        (SELECT count(*) FROM (SELECT StreetMarker FROM s GROUP BY 1 HAVING count(DISTINCT DeviceId) > 1)),
        (SELECT count(*) FROM (SELECT DeviceId FROM s GROUP BY 1 HAVING count(DISTINCT StreetMarker) > 1)),
        (SELECT count(*) FROM (SELECT StreetMarker FROM s GROUP BY 1
            HAVING count(DISTINCT (StreetName, BetweenStreet1, BetweenStreet2)) > 1))""")
    table(["markers with >1 BayId", "BayIds with >1 marker", "markers with >1 device",
           "devices with >1 marker", "markers with >1 segment"], [r])

    heading("3b. Split occupancies: rows → stays")
    rows = ctx.q("""SELECT CASE WHEN g IS NULL THEN 'first row of a bay' WHEN g < 0 THEN 'overlaps previous row'
            WHEN g = 0 THEN '0 s' WHEN g <= 1 THEN '1 s' WHEN g <= 60 THEN '2–60 s' ELSE '> 60 s' END b, count(*)
        FROM (SELECT date_diff('second', lag(DepartureTime) OVER w, ArrivalTime) g FROM s
              WINDOW w AS (PARTITION BY StreetMarker ORDER BY ArrivalTime, DepartureTime, VehiclePresent, DeviceId))
        GROUP BY 1 ORDER BY 2 DESC""")
    emit("Gap between a row's arrival and the previous row's departure, same StreetMarker:")
    table(["gap", "rows"], rows)
    n_vp_rows = ctx.one("SELECT count(*) FROM s WHERE VehiclePresent")[0]
    st = ctx.one("SELECT count(*), sum(n_rows), count(*) FILTER (WHERE n_rows > 1), max(n_rows) FROM stays")
    emit(f"Vehicle-present rows: {n_vp_rows:,}. Stitched (gap ≤ {STITCH_GAP_S} s after a vehicle-present row of "
         f"the same bay) into **{st[0]:,} stays**; {n_vp_rows - st[0]:,} rows ({pct(n_vp_rows - st[0], n_vp_rows)}) "
         f"continue a stay; {st[2]:,} stays span more than one row (max {st[3]}).")
    rows = ctx.q(f"""SELECT strftime(ArrivalTime, '%H:%M:%S') t, count(*) n FROM (
        SELECT ArrivalTime, VehiclePresent, lag(VehiclePresent) OVER w pv, lag(DepartureTime) OVER w pd FROM s
        WINDOW w AS (PARTITION BY StreetMarker ORDER BY ArrivalTime, DepartureTime, VehiclePresent, DeviceId))
        WHERE VehiclePresent AND pv AND date_diff('second', pd, ArrivalTime) BETWEEN 0 AND {STITCH_GAP_S}
        GROUP BY 1 ORDER BY n DESC LIMIT 8""")
    emit("Most common split times (arrival of a continuation row):")
    table(["time", "continuation rows"], rows)
    vp = ctx.one("SELECT count(*), count(*) FILTER (WHERE clean) FROM vp")
    d = quantiles(ctx, "DurationMinutes", "vp")
    emit(f"Stays arriving in 2019: **{vp[0]:,}**; after the cleaning rule (2 min ≤ duration ≤ 24 h): "
         f"**{vp[1]:,}** ({pct(vp[1], vp[0])}). Stay duration (min): p5 {d['p5']}, p25 {d['p25']}, p50 {d['p50']}, "
         f"p75 {d['p75']}, p95 {d['p95']}, p99 {d['p99']}, max {d['max']:,}; "
         f"< 2 min {pct(ctx.one('SELECT count(*) FROM vp WHERE DurationMinutes < 2')[0], vp[0])}, "
         f"> 24 h {pct(ctx.one('SELECT count(*) FROM vp WHERE DurationMinutes > 1440')[0], vp[0])}.")

    heading("4. Sensor file: timezone of the timestamps")
    gap = ctx.one("""SELECT count(*) FILTER (WHERE ArrivalTime >= '2019-10-06 02:00' AND ArrivalTime < '2019-10-06 03:00'),
                            count(*) FILTER (WHERE DepartureTime >= '2019-10-06 02:00' AND DepartureTime < '2019-10-06 03:00')
                     FROM s""")
    emit(f"Rows stamped in 2019-10-06 02:00–02:59, a wall-clock hour that does not exist in Melbourne "
         f"(DST starts, 02:00 → 03:00): arrivals {gap[0]:,}, departures {gap[1]:,}.")
    rows = ctx.q("""SELECT CAST(ArrivalTime AS DATE) d, count(*) FROM s
        WHERE dayofweek(ArrivalTime) = 0 AND hour(ArrivalTime) = 2
          AND ((ArrivalTime BETWEEN '2019-03-17' AND '2019-04-29') OR (ArrivalTime BETWEEN '2019-09-22' AND '2019-10-21'))
        GROUP BY 1 ORDER BY 1""")
    emit("Rows stamped 02:00–02:59 on the Sundays around both DST changes (2019-04-07: that hour occurs "
         "twice; 2019-10-06: it does not exist):")
    table(["Sunday", "arrivals 02:xx"], rows)
    rows = ctx.q("""SELECT label, count(*), avg(DurationMinutes - date_diff('minute', ArrivalTime, DepartureTime))
        FROM (SELECT *, CASE
            WHEN ArrivalTime < '2019-04-07 02:00' AND DepartureTime > '2019-04-07 03:00' THEN 'spans 2019-04-07 02:00-03:00 (DST ends)'
            WHEN ArrivalTime < '2019-10-06 02:00' AND DepartureTime > '2019-10-06 03:00' THEN 'spans 2019-10-06 02:00-03:00 (DST starts)'
            END AS label FROM s) WHERE label IS NOT NULL GROUP BY 1""")
    emit("Mean of `DurationMinutes` − wall-clock difference for stays across a DST change (0: the duration "
         "comes from the same wall-clock strings; ±60: it uses elapsed time):")
    table(["stays", "rows", "mean difference (min)"], rows)
    rows = ctx.q("""SELECT strftime(ArrivalTime, '%H:%M') hm, count(*) n FROM s
        WHERE second(ArrivalTime) = 0 AND minute(ArrivalTime) IN (0, 30)
        GROUP BY 1 ORDER BY n DESC LIMIT 8""")
    emit("Most common arrival times on :00/:30 with zero seconds. Sensor intervals restart at restriction "
         "boundaries, which are local times on the signs (e.g. 07:30, 18:30, 20:30):")
    table(["arrival hh:mm:00", "rows"], rows)
    rows = ctx.q("SELECT hour(ArrivalTime) h, count(*) FROM vp GROUP BY 1 ORDER BY 1")
    emit("Stay arrivals (2019) by hour of the timestamp:")
    table(["hour"] + [str(h) for h, _ in rows], [["arrivals"] + [c for _, c in rows]])

    heading("5. Bay file (On-street Parking Bays)")
    cols = ctx.q(f"""SELECT column_name, column_type FROM (DESCRIBE SELECT * FROM
        read_csv('{sql_path(BAYS_CSV)}', header = true, types = {{'kerbsideid': 'VARCHAR'}}))""")
    emit("Columns (sniffed types): " + ", ".join(f"`{c}` {t}" for c, t in cols))
    emit()
    r = ctx.one("""SELECT count(*), count(DISTINCT roadsegmentid), count(DISTINCT roadsegmentdescription),
        count(kerbsideid), count(DISTINCT kerbsideid),
        count(*) FILTER (WHERE regexp_matches(kerbsideid, '[A-Za-z]')),
        min(TRY_CAST(kerbsideid AS INTEGER)) || '–' || max(TRY_CAST(kerbsideid AS INTEGER)),
        min(latitude), max(latitude), min(longitude), max(longitude),
        min(lastupdated) || ' – ' || max(lastupdated) FROM bays""")
    table(["rows", "roadsegmentid", "descriptions", "kerbsideid non-null", "kerbsideid distinct",
           "kerbsideid with letters", "numeric kerbsideid range", "lat min", "lat max", "lon min", "lon max",
           "lastupdated"], [r])
    letters = [x[0] for x in ctx.q("SELECT kerbsideid FROM bays WHERE regexp_matches(kerbsideid, '[A-Za-z]')")]
    emit(f"Kerbside IDs with letters (look like street markers): {letters}")
    emit()
    r = ctx.q("""SELECT CASE WHEN roadsegmentdescription LIKE '% between % and %' THEN 'X between Y and Z'
        WHEN roadsegmentdescription LIKE 'Intersection of %' THEN 'Intersection of X and Y'
        WHEN roadsegmentdescription LIKE '% from %' THEN 'X from Y' ELSE 'other' END k,
        count(*), count(DISTINCT roadsegmentid), count(kerbsideid) FROM bays GROUP BY 1 ORDER BY 2 DESC""")
    table(["description form", "bays", "segments", "bays with kerbsideid"], r)
    r = ctx.one("""SELECT (SELECT count(*) FROM (SELECT latitude, longitude FROM bays GROUP BY ALL HAVING count(*) > 1)),
        (SELECT count(*) FROM (SELECT kerbsideid FROM bays WHERE kerbsideid IS NOT NULL GROUP BY 1 HAVING count(*) > 1))""")
    emit(f"Duplicated coordinates: {r[0]:,}; kerbside IDs on more than one row: {r[1]:,}.")
    r = ctx.q("SELECT length(split_part(latitude_text, '.', 2)) d, count(*) FROM bays GROUP BY 1 ORDER BY 1")
    emit("Decimals in the latitude text: " + ", ".join(f"{d}: {c:,}" for d, c in r) + ".")


# --- coverage and faults ---------------------------------------------------------------------


def regular_weekdays():
    d, out = dt.date(2019, 1, 1), []
    while d.year == 2019:
        if d.weekday() < 5 and d not in HOLIDAYS_2019:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def known_event(d):
    return ", ".join(name for a, b, name in KNOWN_EVENTS_2019 if a <= d <= b)


def coverage(ctx):
    rows = ctx.q("""SELECT CAST(ArrivalTime AS DATE) d, count(DISTINCT DeviceId), count(DISTINCT StreetMarker)
                    FROM s WHERE year(ArrivalTime) = 2019 GROUP BY 1 ORDER BY 1""")
    active = {d: (dev, mk) for d, dev, mk in rows}
    reg = regular_weekdays()
    med = sorted(active[d][0] for d in reg if d in active)[len(reg) // 2]
    normal = {d for d in reg if d in active and abs(active[d][0] / med - 1) <= COVERAGE_TOL}
    return {"active": active, "median_weekday": med, "normal": normal}


def step_coverage(ctx):
    cov = ctx.memo("coverage", coverage)
    active, med = cov["active"], cov["median_weekday"]
    heading("6. Coverage: active sensors per day")
    devs = sorted(v[0] for v in active.values())
    emit(f"Days with any row: {len(active)}. Active DeviceIds per day (any status): min {devs[0]:,}, "
         f"median {qsorted(devs, 0.5):,}, max {devs[-1]:,}. Median over the "
         f"{len(regular_weekdays())} regular weekdays: **{med:,}**.")
    emit()
    rows = ctx.q("""SELECT month(d) m, median(dev)::INT, min(dev), max(dev), median(mk)::INT FROM (
        SELECT CAST(ArrivalTime AS DATE) d, count(DISTINCT DeviceId) dev, count(DISTINCT StreetMarker) mk
        FROM s WHERE year(ArrivalTime) = 2019 GROUP BY 1) GROUP BY 1 ORDER BY 1""")
    table(["month", "median active devices", "min", "max", "median active markers"], rows)
    days = sorted(active)
    low = []
    for i, d in enumerate(days):
        rm = qsorted(sorted(active[x][0] for x in days[max(0, i - 14):i + 14]), 0.5)
        if active[d][0] < 0.9 * rm:
            low.append((d.isoformat(), d.strftime("%a"), active[d][0], rm, HOLIDAYS_2019.get(d, "")))
    emit(f"Days below 90% of the rolling 28-day median of active devices: {len(low)}")
    if low:
        table(["day", "weekday", "active devices", "rolling median", "holiday"], low[:40])
    reg = regular_weekdays()
    emit(f"Regular weekdays with normal coverage (active devices within ±{COVERAGE_TOL:.0%} of {med:,}): "
         f"**{len(cov['normal'])} of {len(reg)}**.")
    outside = [(d.isoformat(), d.strftime("%a"), active.get(d, (0,))[0], f"{active.get(d, (0,))[0] / med:.3f}")
               for d in reg if d not in cov["normal"]]
    if outside:
        emit()
        emit("Regular weekdays outside the band:")
        table(["day", "weekday", "active devices", "ratio to median"], outside)


def faults(ctx):
    ctx.con.execute(f"""CREATE OR REPLACE TABLE bay_day AS
        SELECT StreetMarker, day, count(DISTINCT DeviceId) n_dev, count(*) n,
               count(*) FILTER (WHERE DurationMinutes < 2) n_blip,
               count(*) FILTER (WHERE clean) n_clean,
               (count(*) > {FAULT_MAX_EVENTS}) OR (count(*) >= {FAULT_MIN_EVENTS_FOR_BLIP_SHARE}
                 AND count(*) FILTER (WHERE DurationMinutes < 2) > {FAULT_BLIP_SHARE} * count(*)) AS flagged
        FROM vp GROUP BY ALL""")
    return True


def step_faults(ctx):
    ctx.memo("faults", faults)
    heading("7. Sensor faults: stays per (bay, day)")
    d = quantiles(ctx, "n", "bay_day")
    table(["bay-days", "p50", "p90", "p99", "max", "mean"],
          [(ctx.one("SELECT count(*) FROM bay_day")[0], d["p50"], d["p90"], d["p99"], d["max"], round(d["mean"], 2))])
    rows = ctx.q("""SELECT CASE WHEN n <= 5 THEN '1-5' WHEN n <= 10 THEN '6-10' WHEN n <= 20 THEN '11-20'
        WHEN n <= 40 THEN '21-40' WHEN n <= 80 THEN '41-80' ELSE '>80' END b, count(*), sum(n)
        FROM bay_day GROUP BY 1 ORDER BY min(n)""")
    table(["stays per bay-day", "bay-days", "stays"], rows)
    r = ctx.one(f"""SELECT count(*) FILTER (WHERE flagged), count(DISTINCT StreetMarker) FILTER (WHERE flagged),
        sum(n) FILTER (WHERE flagged), sum(n), sum(n_clean) FILTER (WHERE flagged), sum(n_clean),
        count(*) FILTER (WHERE n > {FAULT_MAX_EVENTS}), count(*) FILTER (WHERE flagged AND n <= {FAULT_MAX_EVENTS})
        FROM bay_day""")
    emit(f"Flag rule: more than {FAULT_MAX_EVENTS} stays in a bay in a day, or at least "
         f"{FAULT_MIN_EVENTS_FOR_BLIP_SHARE} of which more than {FAULT_BLIP_SHARE:.0%} last under 2 minutes.")
    emit()
    table(["flagged bay-days", "bays", "stays in them", "share of stays",
           "clean stays in them", "share of clean stays", "flagged by count", "flagged by blips only"],
          [(r[0], r[1], r[2], pct(r[2], r[3]), r[4], pct(r[4], r[5]), r[6], r[7])])
    rows = []
    for mx in (40, 60, 80, 120):
        r = ctx.one(f"""SELECT count(*) FILTER (WHERE n > {mx}), sum(n) FILTER (WHERE n > {mx}),
            sum(n_clean) FILTER (WHERE n > {mx}), sum(n), sum(n_clean) FROM bay_day""")
        rows.append((f"> {mx} stays", r[0], r[1], pct(r[1], r[3]), r[2], pct(r[2], r[4])))
    r = ctx.one(f"""SELECT count(*), sum(n), sum(n_clean), (SELECT sum(n) FROM bay_day), (SELECT sum(n_clean) FROM bay_day)
        FROM bay_day WHERE n >= {FAULT_MIN_EVENTS_FOR_BLIP_SHARE} AND n_blip > {FAULT_BLIP_SHARE} * n""")
    rows.append((f"blips only (≥ {FAULT_MIN_EVENTS_FOR_BLIP_SHARE} stays, > {FAULT_BLIP_SHARE:.0%} under 2 min)",
                 r[0], r[1], pct(r[1], r[3]), r[2], pct(r[2], r[4])))
    emit("Sensitivity of each rule on its own:")
    table(["rule", "bay-days", "stays", "share", "clean stays", "share"], rows)
    # Short-stay bays (10-minute, quarter-hour, loading zones) legitimately turn over fast.
    ctx.con.execute("""CREATE OR REPLACE TEMP TABLE bay_sign AS
        SELECT StreetMarker, mode(Sign) AS sign,
               coalesce(regexp_matches(upper(mode(Sign)), '(^P10|^P5|1/4P|^LZ|LOADING|^P/5|^P/10|15MIN|^QP)'), false) AS short_stay
        FROM s GROUP BY 1""")
    rows = ctx.q("""SELECT CASE WHEN b.short_stay THEN 'short-stay sign (P10, 1/4P, loading zone, ...)'
                                WHEN b.sign IS NULL THEN 'no sign' ELSE 'other signs' END k,
        count(DISTINCT d.StreetMarker), count(*) FILTER (WHERE d.flagged), sum(d.n) FILTER (WHERE d.flagged),
        round(avg(d.n), 1), round(sum(d.n_blip) * 100.0 / sum(d.n), 1)
        FROM bay_day d JOIN bay_sign b USING (StreetMarker) GROUP BY 1 ORDER BY 1""")
    emit("By the bay's most common sign:")
    table(["bays", "count", "flagged bay-days", "stays on them", "mean stays per bay-day", "% stays under 2 min"], rows)
    rows = ctx.q("""SELECT StreetMarker, count(*) FILTER (WHERE flagged) fd, sum(n) FILTER (WHERE flagged),
        max(n), round(avg(n), 1), round(sum(n_blip) * 100.0 / sum(n), 1), any_value(b.sign)
        FROM bay_day JOIN bay_sign b USING (StreetMarker) GROUP BY 1 ORDER BY fd DESC, 3 DESC LIMIT 10""")
    emit("Bays with the most flagged days:")
    table(["StreetMarker", "flagged days", "stays on flagged days", "max stays in a day", "mean stays per day",
           "% stays under 2 min", "most common sign"], rows)


# --- joins -----------------------------------------------------------------------------------

ABBREV = {"RD": "ROAD", "AVE": "AVENUE", "AV": "AVENUE", "PDE": "PARADE", "LA": "LANE", "LN": "LANE",
          "PL": "PLACE", "CT": "COURT", "CRES": "CRESCENT", "DR": "DRIVE", "HWY": "HIGHWAY",
          "BVD": "BOULEVARD", "BLVD": "BOULEVARD", "SQ": "SQUARE", "TCE": "TERRACE", "GR": "GROVE",
          "CL": "CLOSE", "ESP": "ESPLANADE", "WY": "WAY", "ALY": "ALLEY"}


def norm_street(name):
    """Upper case, no punctuation, single spaces, common abbreviations spelt out. A leading "ST" is
    Saint (ST KILDA ROAD); a trailing one is Street."""
    if name is None:
        return None
    toks = re.sub(r"[^A-Z0-9 ]", " ", name.upper().replace("'", "")).split()
    if not toks:
        return None
    toks = [ABBREV.get(t, t) for t in toks]
    if toks[-1] == "ST":
        toks[-1] = "STREET"
    if toks[0] == "ST" and len(toks) > 1:
        toks[0] = "SAINT"
    return " ".join(toks)


def parse_segment(desc):
    """(street, frozenset of cross streets) for 'X between Y and Z' and 'X from Y'; None otherwise."""
    desc = desc or ""
    m = re.match(r"^(.*?) between (.*) and (.*)$", desc)
    if m:
        return norm_street(m[1]), frozenset({norm_street(m[2]), norm_street(m[3])})
    m = re.match(r"^(.*?) from (.*)$", desc)
    if m and not desc.startswith("Intersection"):
        return norm_street(m[1]), frozenset({norm_street(m[2])})
    return None


def haversine_m(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6371000 * math.asin(math.sqrt(a))


def build_keys(ctx):
    """Distinct sensor identities with their event counts. Every join maps these, not the 22.6 M
    events, and is then weighted by the counts."""
    ctx.con.execute("""CREATE OR REPLACE TABLE keys AS
        SELECT key, DeviceId, StreetMarker, BayId, StreetName, BetweenStreet1, BetweenStreet2,
               count(*) AS n, count(*) FILTER (WHERE clean) AS n_clean
        FROM vp GROUP BY ALL""")
    return ctx.q("""SELECT key, DeviceId, StreetMarker, BayId, StreetName, BetweenStreet1, BetweenStreet2,
                    n, n_clean FROM keys""")


def bay_tables(ctx):
    bays = ctx.q("SELECT roadsegmentid, kerbsideid, roadsegmentdescription, latitude, longitude FROM bays")
    by_kerb = defaultdict(list)
    seg_pts = defaultdict(list)  # (street, cross set) -> [(lat, lon, roadsegmentid)]
    street_of_segment = {}
    for seg, kerb, desc, lat, lon in bays:
        if kerb is not None:
            by_kerb[kerb.strip()].append((lat, lon, seg))
        parsed = parse_segment(desc)
        if parsed:
            seg_pts[parsed].append((lat, lon, seg))
            street_of_segment[seg] = parsed[0]
        else:
            m = re.match(r"^Intersection of (.*) and (.*)$", desc or "")
            street_of_segment[seg] = {norm_street(m[1]), norm_street(m[2])} if m else None
    kerb_loc = {k: (sum(p[0] for p in v) / len(v), sum(p[1] for p in v) / len(v), v[0][2]) for k, v in by_kerb.items()}
    seg_loc = {k: (sum(p[0] for p in v) / len(v), sum(p[1] for p in v) / len(v),
                   sorted({p[2] for p in v}), len(v)) for k, v in seg_pts.items()}
    return {"kerb": kerb_loc, "seg": seg_loc, "street_of_segment": street_of_segment, "n_bays": len(bays)}


def bay_sensor_table():
    """kerbsideid -> (lat, lon) from the optional On-street Parking Bay Sensors download."""
    if not BAY_SENSORS_CSV.exists():
        return None
    con = duckdb.connect()
    src = f"read_csv('{sql_path(BAY_SENSORS_CSV)}', all_varchar = true)"
    cols = [c[0] for c in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()]
    low = {c.lower(): c for c in cols}
    kcol = next((low[c] for c in ("kerbsideid", "kerbside_id") if c in low), None)
    if kcol is None:
        raise SystemExit(f"{BAY_SENSORS_CSV.name}: no kerbsideid column in {cols}")
    if "lat" in low or "latitude" in low:
        la, lo = low.get("lat", low.get("latitude")), low.get("lon", low.get("longitude"))
        sel = f'"{kcol}", TRY_CAST("{la}" AS DOUBLE), TRY_CAST("{lo}" AS DOUBLE)'
    else:
        loc = low["location"]
        sel = (f'"{kcol}", TRY_CAST(trim(split_part("{loc}", \',\', 1)) AS DOUBLE), '
               f'TRY_CAST(trim(split_part("{loc}", \',\', 2)) AS DOUBLE)')
    rows = con.execute(f"SELECT {sel} FROM {src}").fetchall()
    out = defaultdict(list)
    for k, la, lo in rows:
        if k is not None and la is not None:
            out[k.strip()].append((la, lo))
    return {"columns": cols, "rows": len(rows),
            "loc": {k: (sum(p[0] for p in v) / len(v), sum(p[1] for p in v) / len(v), None) for k, v in out.items()}}


def joins(ctx):
    keys = ctx.memo("keys", build_keys)
    bt = ctx.memo("bay_tables", bay_tables)
    bs = bay_sensor_table()
    n_all = sum(k[7] for k in keys)
    n_clean = sum(k[8] for k in keys)

    # Segment join first: it is also the plausibility reference for the ID joins.
    seg_by_street = defaultdict(list)
    for (street, cross), v in bt["seg"].items():
        seg_by_street[street].append((cross, v))
    seg_match, seg_partial, partial_info = {}, {}, {}
    for key, _dev, _mk, _bay, st, b1, b2, _n, _nc in keys:
        street, cross = norm_street(st), frozenset({norm_street(b1), norm_street(b2)})
        if (street, cross) in bt["seg"]:
            lat, lon, segs, _nb = bt["seg"][(street, cross)]
            seg_match[key] = (lat, lon, "seg:" + "+".join(map(str, segs)))
        else:
            # Partial: same street and at least one shared cross street; bay-weighted mean.
            cands = [v for c, v in seg_by_street.get(street, []) if c & cross]
            if cands:
                w = sum(v[3] for v in cands)
                lat = sum(v[0] * v[3] for v in cands) / w
                lon = sum(v[1] * v[3] for v in cands) / w
                segs = sorted({s for v in cands for s in v[2]})
                seg_partial[key] = (lat, lon, "seg:" + "+".join(map(str, segs)))
                partial_info[key] = (len(cands), max(haversine_m(lat, lon, v[0], v[1]) for v in cands))

    methods = {}

    def id_join(name, lookup, field_idx, prefix, street_check=True):
        m = {}
        plaus = {"street": [0, 0], "dist": [0, 0]}
        for k in keys:
            v = k[field_idx]
            if v is None:
                continue
            hit = lookup.get(str(v).strip())
            if hit is None:
                continue
            lat, lon, seg = hit
            m[k[0]] = (lat, lon, f"{prefix}:{str(v).strip()}")
            if street_check and seg is not None:
                s_of = bt["street_of_segment"].get(seg)
                ok = norm_street(k[4]) in s_of if isinstance(s_of, set) else norm_street(k[4]) == s_of
                plaus["street"][0] += k[7] * ok
                plaus["street"][1] += k[7]
            ref = seg_match.get(k[0])
            if ref:
                plaus["dist"][0] += k[7] * (haversine_m(lat, lon, ref[0], ref[1]) <= 300)
                plaus["dist"][1] += k[7]
        methods[name] = {"map": m, "plaus": plaus}

    id_join("1a BayId = bays.kerbsideid", bt["kerb"], 3, "kerb")
    id_join("1b StreetMarker = bays.kerbsideid", bt["kerb"], 2, "kerb")
    id_join("1c DeviceId = bays.kerbsideid", bt["kerb"], 1, "kerb")
    if bs:
        id_join("1d BayId = sensors.kerbsideid", bs["loc"], 3, "sensor", street_check=False)
        id_join("1e StreetMarker = sensors.kerbsideid", bs["loc"], 2, "sensor", street_check=False)
        id_join("1f DeviceId = sensors.kerbsideid", bs["loc"], 1, "sensor", street_check=False)
    methods["2a segment, exact (street + both cross streets)"] = {"map": seg_match, "plaus": None}
    unique = {k: v for k, v in seg_partial.items() if partial_info[k][0] == 1}
    methods["2b segment, exact or partial (one cross street)"] = {"map": {**seg_partial, **seg_match}, "plaus": None}
    methods["2c segment, exact or partial with a single candidate"] = {"map": {**unique, **seg_match}, "plaus": None}
    return {"keys": keys, "methods": methods, "n_all": n_all, "n_clean": n_clean, "bt": bt, "bs": bs,
            "partial_info": partial_info}


def method_stats(keys, m):
    loc_n, cells = Counter(), Counter()
    a = c = 0
    for key, *_rest, n, nc in keys:
        hit = m.get(key)
        if hit:
            a += n
            c += nc
            loc_n[hit[2]] += nc
            cells[(math.floor(hit[0] * SCALE + 0.5), math.floor(hit[1] * SCALE + 0.5))] += nc
    return a, c, loc_n, cells


def plausible(spec):
    p = spec["plaus"]
    if p is None:
        return True
    measured = [v for v in p.values() if v[1]]
    return bool(measured) and all(v[0] / v[1] >= 0.9 for v in measured)


def step_joins(ctx):
    J = ctx.memo("joins", joins)
    keys, n_all, n_clean = J["keys"], J["n_all"], J["n_clean"]
    heading("8. Location joins")
    emit(f"Base: {n_all:,} stays (stitched vehicle-present intervals) arriving in 2019 ({n_clean:,} clean), "
         f"{len(keys):,} distinct sensor identities (device, marker, bay, street segment).")
    if J["bs"] is None:
        emit()
        emit(f"**On-street Parking Bay Sensors not found** at `{BAY_SENSORS_CSV.relative_to(REPO)}`; "
             f"joins 1d–1f skipped. Download: {BAY_SENSORS_URL}")
    else:
        emit(f"On-street Parking Bay Sensors: {J['bs']['rows']:,} rows, {len(J['bs']['loc']):,} kerbside IDs "
             f"with coordinates; columns {J['bs']['columns']}.")
    emit()
    rows = []
    stats = {}
    for name, spec in J["methods"].items():
        a, c, loc_n, cells = stats[name] = method_stats(keys, spec["map"])
        p = spec["plaus"]
        street = pct(*p["street"]) if p and p["street"][1] else "-"
        dist = pct(*p["dist"]) if p and p["dist"][1] else "-"
        rows.append((name, a, pct(a, n_all), c, pct(c, n_clean), len(loc_n), len(cells), street, dist))
    bay_cells = ctx.one(f"""SELECT count(DISTINCT (floor(latitude * {SCALE} + 0.5), floor(longitude * {SCALE} + 0.5)))
                            FROM bays""")[0]
    rows.append(("3 fallback: synthetic on real bays", n_all, "100% (by construction)", n_clean, "100%",
                 J["bt"]["n_bays"], bay_cells, "-", "-"))
    table(["method", "stays located", "share", "clean stays located", "share", "distinct locations",
           "distinct 0.001° cells", "matched bay on the sensor's street (stays)",
           "within 300 m of the segment location (stays)"], rows)
    emit("The last two columns test whether an ID match is real: a coincidental numeric match lands on a "
         "bay in another street, far from the sensor's own street segment.")

    for name, spec in J["methods"].items():
        a, c, loc_n, cells = stats[name]
        if c == 0:
            continue
        miss = sorted((k for k in keys if k[0] not in spec["map"]), key=lambda k: -k[7])
        emit()
        emit(f"**{name}** — unmatched sensor identities by stays (top 10 of {len(miss):,}, "
             f"{sum(k[7] for k in miss):,} stays):")
        emit()
        table(["DeviceId", "StreetMarker", "BayId", "StreetName", "BetweenStreet1", "BetweenStreet2", "stays"],
              [k[1:8] for k in miss[:10]])
        if name.startswith("2") or plausible(spec):
            vals = sorted(loc_n.values())
            cv = sorted(cells.values())
            emit(f"Concentration: clean stays per distinct location p50 {qsorted(vals, 0.5):,}, "
                 f"p90 {qsorted(vals, 0.9):,}, max {vals[-1]:,} ({len(loc_n):,} locations); "
                 f"{len(cells):,} distinct 0.001° cells, clean stays per cell p50 {qsorted(cv, 0.5):,}, "
                 f"max {cv[-1]:,}.")
            emit()
            emit("Top 10 locations by clean stays:")
            table(["location", "clean stays", "share"], [(k, v, pct(v, c)) for k, v in loc_n.most_common(10)])

    pi = J["partial_info"]
    by_cands = Counter()
    far = 0
    for k in keys:
        if k[0] in pi:
            by_cands["1 segment" if pi[k[0]][0] == 1 else "2 segments" if pi[k[0]][0] == 2 else "3+ segments"] += k[7]
            far += k[7] * (pi[k[0]][1] > 150)
    emit()
    emit("Partial segment matches (2b only): number of bay-file segments that share the street and one cross "
         "street, by stays. With one candidate, the other cross street was renamed or is missing; with two, "
         "the location is the mean of the segments on both sides of the shared intersection.")
    table(["candidates", "stays"], sorted(by_cands.items()))
    emit(f"Partial-match stays whose location is more than 150 m from one of its candidate segments: {far:,}.")
    multi = sum(1 for v in J["bt"]["seg"].values() if len(v[2]) > 1)
    emit(f"Bay-file segment keys (street + cross streets) covering more than one roadsegmentid: {multi:,} "
         f"of {len(J['bt']['seg']):,} (their bays are averaged together).")
    best = ctx.memo("best", best_method)
    emit()
    emit(f"Join used for the following steps: **{best}** (exact location for every stay it locates; "
         f"2b locates more but averages up to several neighbouring segments).")


def best_method(ctx):
    """RECOMMENDED_JOIN if it exists and is plausible; otherwise the real (non-synthetic) join that
    locates the most clean stays among those whose matches pass the plausibility checks."""
    J = ctx.memo("joins", joins)
    for name, spec in J["methods"].items():
        if name.startswith(RECOMMENDED_JOIN + " ") and plausible(spec):
            return name
    best = None
    for name, spec in J["methods"].items():
        if not plausible(spec):
            continue
        c = method_stats(J["keys"], spec["map"])[1]
        if best is None or c > best[1]:
            best = (name, c)
    return best[0]


def located(ctx):
    """Clean, located vehicle-present events (best join) as the DuckDB table `loc`."""
    J = ctx.memo("joins", joins)
    name = ctx.memo("best", best_method)
    m = J["methods"][name]["map"]
    ctx.con.execute("CREATE OR REPLACE TABLE keymap (key UBIGINT, lat DOUBLE, lon DOUBLE, loc VARCHAR)")
    ctx.con.executemany("INSERT INTO keymap VALUES (?, ?, ?, ?)", [(k, *v) for k, v in m.items()])
    ctx.con.execute("""CREATE OR REPLACE TABLE loc AS
        SELECT v.DeviceId, v.ArrivalTime, v.DepartureTime, v.DurationMinutes, v.day, km.lat, km.lon, km.loc
        FROM vp v JOIN keymap km USING (key) WHERE v.clean""")
    return name


# --- days ------------------------------------------------------------------------------------


def step_days(ctx):
    name = ctx.memo("located", located)
    cov = ctx.memo("coverage", coverage)
    ctx.memo("faults", faults)
    heading("9. Day candidates")
    emit(f"Join used: **{name}**. Counts are clean, located stays by arrival date.")
    emit()
    counts = dict(ctx.q("SELECT day, count(*) FROM loc GROUP BY 1"))
    raw = dict(ctx.q("SELECT day, count(*) FROM vp GROUP BY 1"))
    flagged = dict(ctx.q("SELECT day, sum(n_clean) FROM bay_day WHERE flagged GROUP BY 1"))
    reg = regular_weekdays()
    med = cov["median_weekday"]

    def row(d):
        notes = []
        if d not in cov["normal"]:
            notes.append("coverage outside band")
        if known_event(d):
            notes.append("event: " + known_event(d))
        return (d.isoformat(), d.strftime("%a"), counts.get(d, 0), raw.get(d, 0), cov["active"][d][0],
                f"{cov['active'][d][0] / med:.3f}", f"{counts.get(d, 0) / cov['active'][d][0]:.2f}",
                flagged.get(d, 0), "; ".join(notes) or "eligible")

    cols = ["day", "weekday", "clean located stays", "all stays", "active devices", "ratio to weekday median",
            "clean located stays per active device", "clean stays on flagged bay-days", "notes"]
    by_count = sorted(reg, key=lambda d: -counts.get(d, 0))
    emit("Top 5 regular weekdays by count:")
    table(cols, [row(d) for d in by_count[:5]])
    eligible = [d for d in by_count if d in cov["normal"] and not known_event(d)]
    emit("Top 5 eligible regular weekdays (normal coverage, no known event):")
    table(cols, [row(d) for d in eligible[:5]])
    per_dev = sorted(reg, key=lambda d: -counts.get(d, 0) / cov["active"][d][0])
    emit("Top 5 regular weekdays by clean located stays per active device (removes the network's growth):")
    table(cols, [row(d) for d in per_dev[:5]])
    wk = sorted(counts.get(d, 0) for d in reg)
    emit(f"Regular weekdays: median {qsorted(wk, 0.5):,} clean located stays, max {wk[-1]:,}.")
    day = eligible[0]
    ctx._memo["day"] = day
    emit()
    emit(f"Chosen day: **{day.isoformat()} ({day.strftime('%A')})**.")
    emit()

    # Design A and B for the chosen day.
    D, D1 = day.isoformat(), (day + dt.timedelta(days=1)).isoformat()
    ctx.con.execute(f"""CREATE OR REPLACE TABLE reports_b AS
        SELECT lat, lon, loc, DeviceId, ArrivalTime, hour(h) AS hour,
               CAST(ArrivalTime AS DATE) = DATE '{D}' AS arrived_today
        FROM (SELECT *, unnest(generate_series(
                greatest(date_trunc('hour', ArrivalTime), TIMESTAMP '{D}'),
                least(date_trunc('hour', DepartureTime - INTERVAL 1 MICROSECOND), TIMESTAMP '{D}' + INTERVAL 23 HOUR),
                INTERVAL 1 HOUR)) AS h
              FROM loc WHERE ArrivalTime < TIMESTAMP '{D1}' AND DepartureTime > TIMESTAMP '{D}')""")
    a = ctx.one(f"SELECT count(*) FROM loc WHERE day = DATE '{D}'")[0]
    b, b_prev = ctx.one("SELECT count(*), count(*) FILTER (WHERE NOT arrived_today) FROM reports_b")
    unclipped = ctx.one(f"""SELECT sum(date_diff('hour', date_trunc('hour', ArrivalTime),
        date_trunc('hour', DepartureTime - INTERVAL 1 MICROSECOND)) + 1) FROM loc WHERE day = DATE '{D}'""")[0]
    flagged_today = ctx.one(f"SELECT sum(n_clean) FROM bay_day WHERE flagged AND day = DATE '{D}'")[0] or 0
    table(["design", "reports", "note"], [
        ("A: one per arrival", a, "clean located stays arriving on the day"),
        ("B: one per occupied hour (default)", b,
         f"hours on the day of every stay overlapping it; {b_prev:,} from stays that began the day before"),
        ("B, unclipped variant", unclipped, "all occupied hours of the stays arriving on the day, incl. after midnight"),
    ])
    emit(f"Clean stays on flagged bay-days that day: {flagged_today:,} ({pct(flagged_today, a)} of design A).")
    emit()
    rows = ctx.q("""SELECT c, count(*) FROM (SELECT DeviceId, ArrivalTime, count(*) c FROM reports_b
        WHERE arrived_today GROUP BY ALL) GROUP BY 1 ORDER BY 1""")
    head, tail = rows[:12], rows[12:]
    emit("Design B reports per stay arriving on the day:")
    table(["reports per event"] + [str(r[0]) for r in head] + ([f">{head[-1][0]}"] if tail else []),
          [["stays"] + [r[1] for r in head] + ([sum(r[1] for r in tail)] if tail else [])])
    rows = ctx.q("SELECT hour, count(*) FROM reports_b GROUP BY 1 ORDER BY 1")
    emit("Design B reports (cars present) per hour:")
    table(["hour"] + [str(h) for h, _ in rows], [["reports"] + [c for _, c in rows]])
    bb = ctx.one(f"SELECT min(lat), max(lat), min(lon), max(lon), count(DISTINCT loc) FROM loc WHERE day = DATE '{D}'")
    bby = ctx.one("SELECT min(lat), max(lat), min(lon), max(lon), count(DISTINCT loc) FROM loc")
    emit("Bounding box of the located stays:")
    table(["stays", "min lat", "max lat", "min lon", "max lon", "distinct locations"],
          [("chosen day",) + tuple(bb), ("all of 2019",) + tuple(bby)])
    w = (bb[3] - bb[2]) * 111.32 * math.cos(math.radians((bb[0] + bb[1]) / 2))
    h = (bb[1] - bb[0]) * 110.57
    emit(f"Chosen-day box: about {w:.2f} km (E–W) × {h:.2f} km (N–S).")


# --- distributions ---------------------------------------------------------------------------


def components(points):
    """Number of 8-connected components of a set of grid points."""
    seen, n = set(), 0
    for p in points:
        if p in seen:
            continue
        n += 1
        stack = [p]
        seen.add(p)
        while stack:
            i, j = stack.pop()
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    q = (i + di, j + dj)
                    if q in points and q not in seen:
                        seen.add(q)
                        stack.append(q)
    return n


def step_dist(ctx):
    if "day" not in ctx._memo:
        step_days(ctx)
    day = ctx._memo["day"]
    heading(f"10. Count distributions on {day.isoformat()} (design B)")
    rows = ctx.q(f"""SELECT floor(lat * {SCALE} + 0.5)::INT, floor(lon * {SCALE} + 0.5)::INT, hour, count(*)
                     FROM reports_b GROUP BY ALL""")
    cell = Counter()
    win = [Counter() for _ in WINDOWS]
    for la, lo, hr, c in rows:
        cell[(la, lo)] += c
        win[hr // 4][(la, lo)] += c
    N = sum(cell.values())
    vals = sorted(cell.values(), reverse=True)
    asc = vals[::-1]
    emit(f"**Mastic (cells, whole day).** N = {N:,} reports in {len(cell):,} non-empty 0.001° cells "
         f"(cell = point rounded to 0.001°). Reports per cell: p50 {qsorted(asc, 0.5):,}, p90 {qsorted(asc, 0.9):,}, "
         f"p99 {qsorted(asc, 0.99):,}, max {vals[0]:,} ({pct(vals[0], N)} of N). "
         f"Top 10: {', '.join(f'{v:,}' for v in vals[:10])}.")
    emit()
    rows = []
    for tau in TAUS:
        T = max(1, int(tau * N))
        heavy = [v for v in vals if v >= T]
        rows.append((f"{tau * 100:g}%", T, len(heavy), sum(heavy), pct(sum(heavy), N)))
    table(["τ", "T = max(1, ⌊τN⌋)", "heavy cells (count ≥ T)", "reports in heavy cells", "share of N"], rows)
    rows = []
    for k in (10, 20, 30):
        if len(vals) > k:
            # floor(tau N) <= vals[k-1] keeps the k-th cell; floor(tau N) > vals[k] drops the next.
            lo, hi = (vals[k] + 1) / N, (vals[k - 1] + 1) / N
            rows.append((k, vals[k - 1], vals[k], f"{lo * 100:.3f}% ≤ τ < {hi * 100:.3f}%" if lo < hi else "tie"))
    emit("τ giving exactly k heavy cells:")
    table(["k", "count of k-th cell", "count of (k+1)-th", "τ range"], rows)

    lat_lo, lat_hi = min(la for la, _ in cell), max(la for la, _ in cell)
    lon_lo, lon_hi = min(lo for _, lo in cell), max(lo for _, lo in cell)
    for delta in DELTAS:
        side = 2 * delta + 1
        emit()
        emit(f"**Mosaic, δ = {delta} ({side}×{side} cells), per 4-hour window.** Centres: every grid point "
             f"within δ of the data extent; count = reports in the {side}×{side} square around it; "
             f"T_w = max(1, ⌊τ·N_w⌋), pass if count ≥ T_w. Table cells: passing centres / connected areas.")
        emit()
        g_lat0, g_lon0 = lat_lo - delta, lon_lo - delta
        H, W = lat_hi - lat_lo + 1 + 2 * delta, lon_hi - lon_lo + 1 + 2 * delta
        cols = ["τ"]
        res = {tau: [] for tau in TAUS}
        info = []
        for (a, b), wc in zip(WINDOWS, win):
            Nw = sum(wc.values())
            P = [[0] * (W + 1) for _ in range(H + 1)]
            for (la, lo), c in wc.items():
                P[la - g_lat0 + 1][lo - g_lon0 + 1] += c
            for i in range(1, H + 1):
                for j in range(1, W + 1):
                    P[i][j] += P[i - 1][j] + P[i][j - 1] - P[i - 1][j - 1]

            def square(i, j):
                i0, i1 = max(0, i - delta), min(H, i + delta + 1)
                j0, j1 = max(0, j - delta), min(W, j + delta + 1)
                return P[i1][j1] - P[i0][j1] - P[i1][j0] + P[i0][j0]

            nb = {(i, j): square(i, j) for i in range(H) for j in range(W)}
            nv = sorted(nb.values())
            cols.append(f"{a:02d}–{b:02d}")
            info.append((f"{a:02d}–{b:02d}", Nw, len(wc), nv[-1], pct(nv[-1], Nw), qsorted(nv, 0.99)))
            for tau in TAUS:
                T = max(1, int(tau * Nw))
                passing = {p for p, v in nb.items() if v >= T}
                res[tau].append(f"{len(passing):,} / {components(passing)}" if Nw else "-")
        table(["window", "N_w reports", "non-empty cells", f"max {side}×{side} count", "max / N_w",
               "p99 square count"], info)
        table(cols, [[f"{tau * 100:g}%"] + res[tau] for tau in TAUS])


# --- encodings -------------------------------------------------------------------------------


def common_prefix_len(bitlists):
    first, n = bitlists[0], len(bitlists[0])
    for b in bitlists[1:]:
        i = 0
        while i < n and b[i] == first[i]:
            i += 1
        n = i
    return n


def geohash_size(total_bits, lat):
    lon_bits, lat_bits = (total_bits + 1) // 2, total_bits // 2
    w = 360 / 2 ** lon_bits * 111.32 * math.cos(math.radians(lat))
    h = 180 / 2 ** lat_bits * 110.57
    return w, h, w * h


def morton_box(points):
    """0.001° cells with edges half a cell off the 3-decimal grid, so cell i holds the points that
    round to i/1000 (Mosaic's rounding); 2^k cells per axis, anchored south-west or centred."""
    li = [math.floor(la * SCALE + 0.5) for la, _ in points]
    oi = [math.floor(lo * SCALE + 0.5) for _, lo in points]
    lat_span, lon_span = max(li) - min(li) + 1, max(oi) - min(oi) + 1
    k = max(1, math.ceil(math.log2(max(lat_span, lon_span))))
    cells = 2 ** k
    out = {"k": k, "n": 2 * k, "lat_span": lat_span, "lon_span": lon_span}
    for mode in ("anchored", "centred"):
        lat0 = min(li) - (0 if mode == "anchored" else (cells - lat_span) // 2)
        lon0 = min(oi) - (0 if mode == "anchored" else (cells - lon_span) // 2)
        box = (round((lat0 - 0.5) / SCALE, 4), round((lat0 - 0.5 + cells) / SCALE, 4),
               round((lon0 - 0.5) / SCALE, 4), round((lon0 - 0.5 + cells) / SCALE, 4))
        enc = chh.Morton(box, k)
        dropped = mismatch = 0
        for (la, lo), a, b in zip(points, li, oi):
            bits = enc.encode(la, lo)
            if bits is None:
                dropped += 1
            elif chh.bits_to_grid(bits) != (a - lat0, b - lon0):
                mismatch += 1
        out[mode] = {"box": box, "dropped": dropped, "mismatch": mismatch}
    return out


def step_encodings(ctx):
    name = ctx.memo("located", located)
    heading("11. Encodings for Melbourne")
    sets = {"located event locations, 2019 (join " + name.split(" ")[0] + ")": ctx.q("SELECT DISTINCT lat, lon FROM loc")}
    if "day" in ctx._memo:
        sets["located locations on the chosen day"] = ctx.q(
            f"SELECT DISTINCT lat, lon FROM loc WHERE day = DATE '{ctx._memo['day'].isoformat()}'")
    sets["all bays (On-street Parking Bays)"] = ctx.q("SELECT latitude, longitude FROM bays")
    for label, pts in sets.items():
        emit()
        emit(f"**{label}** — {len(pts):,} points")
        emit()
        gh = [chh.geohash_bits(la, lo, 60) for la, lo in pts]
        cp = common_prefix_len(gh)
        chars = cp // 5
        prefix = chh.bits_to_base32(gh[0][:chars * 5])
        emit(f"- Geohash: common prefix {cp} bits = `{chh.bits_to_base32(gh[0][:cp])}`; whole base32 "
             f"characters: `{prefix}` ({chars * 5} bits).")
        mid = sum(la for la, _ in pts) / len(pts)
        cand = []
        for tb in range(32, 40):
            w, h, a = geohash_size(tb, mid)
            cand.append((tb, tb - chars * 5, f"{w * 1000:.0f} m × {h * 1000:.0f} m", round(a, 4)))
        best = min(cand, key=lambda c: abs(math.log(c[3] / 0.01)))
        emit()
        table(["total geohash bits", f"bits after `{prefix}`", "cell size (E–W × N–S)", "area km²"], cand)
        emit(f"  Closest to 0.01 km²: {best[0]} total bits → `prefix = \"{prefix}\"`, `bits = {best[1]}` "
             f"(data_bits = {best[1]}).")
        emit()
        rows = []
        for r in range(2, 8):
            parents = Counter(h3.cell_to_parent(h3.latlng_to_cell(la, lo, 10), r) for la, lo in pts)
            common = next(iter(parents)) if len(parents) == 1 else "-"
            rows.append((r, len(parents), common, 3 * (10 - r) if common != "-" else "-",
                         ", ".join(f"{p} ({c:,})" for p, c in parents.most_common(3))))
        emit("- H3 (cells at resolution 10):")
        emit()
        table(["parent resolution", "distinct parents", "common parent", "data_bits at res 10", "largest parents"], rows)
        mb = morton_box(pts)
        emit(f"- Morton (0.001° cells): extent {mb['lat_span']} × {mb['lon_span']} cells (lat × lon) → "
             f"**k = {mb['k']}** ({2 ** mb['k']} cells per axis, {2 ** mb['k'] / SCALE:.3f}°), **n = {mb['n']}** levels.")
        for mode in ("anchored", "centred"):
            b = mb[mode]["box"]
            emit(f"  - {mode}: `encoding.morton = {{ min_lat = {b[0]}, max_lat = {b[1]}, min_lon = {b[2]}, "
                 f"max_lon = {b[3]}, axis_bits = {mb['k']} }}` — points dropped: {mb[mode]['dropped']}, "
                 f"Morton cell ≠ rounded grid point: {mb[mode]['mismatch']}")
    ev_pts = next(iter(sets.values()))
    bays = sets["all bays (On-street Parking Bays)"]
    mb = morton_box(ev_pts)
    emit()
    for mode in ("anchored", "centred"):
        enc = chh.Morton(mb[mode]["box"], mb["k"])
        inside = sum(enc.encode(la, lo) is not None for la, lo in bays)
        emit(f"- Bays inside the {mode} 2019 event box: {inside:,} of {len(bays):,} ({pct(inside, len(bays))}).")


STEPS = {
    "profile": step_profile,
    "coverage": step_coverage,
    "faults": step_faults,
    "joins": step_joins,
    "days": step_days,
    "dist": step_dist,
    "encodings": step_encodings,
}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--step", default="all", choices=["all", "cache", *STEPS])
    ap.add_argument("--rebuild-cache", action="store_true", help="rescan the CSV into the Parquet cache")
    args = ap.parse_args()

    t0 = time.time()
    con = connect()
    build_cache(con, force=args.rebuild_cache)
    if args.step == "cache":
        return
    register_views(con)
    ctx = Ctx(con)
    emit(f"# P1 exploration output ({dt.datetime.now().isoformat(timespec='seconds')})")
    emit()
    emit(f"Generated by `apps/parking/scripts/explore.py --step {args.step}`: the raw tables behind "
         f"reports/01-exploration.md. Inputs and their SHA-256: apps/parking/raw_manifest.json.")
    for name, fn in STEPS.items():
        if args.step in ("all", name):
            fn(ctx)
    emit()
    emit(f"_Run time: {time.time() - t0:.0f} s._")
    if args.step == "all":
        OUTPUT_MD.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_MD.write_text("\n".join(_out) + "\n", encoding="utf-8")
        print(f"wrote {OUTPUT_MD.relative_to(REPO)}")


if __name__ == "__main__":
    main()
