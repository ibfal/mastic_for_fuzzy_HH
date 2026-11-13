"""Tests for bench/scripts/evaluate.py (and the tau helpers of bench_common.py).

- the network model (A4) on hand-computed numbers;
- the accuracy against the reference (A5): a hand-made reference with two areas and known area
  recall / precision / F1, the delta boundary, the diagonal-touching rule for areas, and the
  strict A5 file format;
- the self-check, the leakage totals, the summary statistics, the summary CSV, and rows keyed by
  system x encoding x subset x tau (including --combine with several taus).

    .\\.venv\\Scripts\\python.exe -m pytest bench\\tests -p no:cacheprovider
"""

import copy
import json
import sys
from pathlib import Path

import pytest

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bench_common as bc  # noqa: E402
import evaluate as ev  # noqa: E402

approx = pytest.approx


def example_line(**changes):
    """A valid A9 run line (bench_common's example), with top-level changes."""
    line = copy.deepcopy(bc.EXAMPLE_RUN)
    line.update(changes)
    assert not bc.validate_result(line)
    return line


# --- Network model (A4) ------------------------------------------------------------------------


def test_transfer_ms():
    # 1,250,000 B = 10^7 bit: 10 ms at 1 Gbit/s, 100 ms at 100 Mbit/s, 1 s at 10 Mbit/s.
    assert ev.transfer_ms(1_250_000, 1e9) == approx(10.0)
    assert ev.transfer_ms(1_250_000, 100e6) == approx(100.0)
    assert ev.transfer_ms(1_250_000, 10e6) == approx(1000.0)


def test_profiles_are_the_spec_values():
    assert (ev.LAN.rtt_ms, ev.LAN.bandwidth_bps) == (0.5, 1e9)
    assert (ev.WAN.rtt_ms, ev.WAN.bandwidth_bps) == (50.0, 100e6)
    assert ev.CLIENT_UPLINK_BPS == 10e6


def test_comm_ms_uses_rounds_and_the_busiest_link():
    links = [1_000, 1_250_000, 400_000, 0]
    # LAN: 10 x 0.5 ms + 10^7 bit / 10^9 bit/s = 5 + 10 = 15 ms.
    assert ev.comm_ms(10, links, ev.LAN) == approx(15.0)
    # WAN: 10 x 50 ms + 10^7 bit / 10^8 bit/s = 500 + 100 = 600 ms.
    assert ev.comm_ms(10, links, ev.WAN) == approx(600.0)
    assert ev.comm_ms(0, [], ev.LAN) == 0


def test_online_est_adds_the_measured_online_time():
    assert ev.online_est_ms(1000.0, 10, [1_250_000], ev.LAN) == approx(1015.0)
    assert ev.online_est_ms(1000.0, 10, [1_250_000], ev.WAN) == approx(1600.0)


def test_model_on_the_numbers_of_a_measured_run():
    # mosaic_grid, day_3of3: 49 online rounds; busiest link server -> collector, 2,595,282 B.
    # LAN: 49 x 0.5 = 24.5 ms; 2,595,282 x 8 / 10^9 s = 20.762256 ms; sum 45.262256 ms.
    # WAN: 49 x 50 = 2,450 ms; 2,595,282 x 8 / 10^8 s = 207.62256 ms; sum 2,657.62256 ms.
    links = [22_423, 22_423, 2_595_282, 2_595_282, 0]
    assert ev.comm_ms(49, links, ev.LAN) == approx(45.262256)
    assert ev.comm_ms(49, links, ev.WAN) == approx(2657.62256)


def test_client_upload_is_one_round_plus_bytes_over_the_uplink():
    # geohash: 3,274 B per client; 3,274 x 8 / 10^7 s = 2.6192 ms.
    assert ev.client_upload_ms(3274, ev.LAN) == approx(0.5 + 2.6192)
    assert ev.client_upload_ms(3274, ev.WAN) == approx(50 + 2.6192)


def test_online_links_count_null_fields_as_zero():
    online = {"collector_to_server": [1, 2], "server_to_collector": None, "server_to_server": 7}
    assert ev.online_link_bytes(online) == [1, 2, 0, 0, 7]


def test_run_metrics_model_counts_extra_rounds_and_upload_per_client():
    line = example_line(n_clients=1000)
    line["phases"]["online_ms"] = 1000.0
    line["rounds"] = {"upload": 1, "online": 8, "extra": 2}
    line["bytes"] = {
        "upload": {"client_to_server": [1_637_000, 1_637_000]},
        "online": {"collector_to_server": [1_000, 1_000],
                   "server_to_collector": [1_250_000, 1_200_000], "server_to_server": 0},
    }
    m = ev.run_metrics(line)
    # (8 online + 2 extra) x 0.5 ms + 1,250,000 B at 1 Gbit/s = 5 + 10 = 15 ms.
    assert m["online_comm_lan_ms"] == approx(15.0)
    assert m["online_est_lan_ms"] == approx(1015.0)
    # 10 x 50 ms + 100 ms = 600 ms.
    assert m["online_comm_wan_ms"] == approx(600.0)
    assert m["online_est_wan_ms"] == approx(1600.0)
    assert m["bytes_upload_total"] == 3_274_000
    assert m["bytes_upload_per_client"] == approx(3274.0)
    assert m["client_upload_lan_ms"] == approx(3.1192)
    assert m["client_upload_wan_ms"] == approx(52.6192)
    assert m["bytes_online_max_link"] == 1_250_000
    assert m["bytes_online_total"] == 2_452_000
    assert m["rounds_upload"] == 1  # as implemented; the model does not use it
    assert all(m[k] is None for k in ("area_recall", "precision", "f1", "areas_found"))  # no ref


# --- Accuracy against the reference (A5) -------------------------------------------------------


def a5_reference(squares, T=431, delta=5, tau=0.02, dataset="day", subset="3of3"):
    """An A5 reference file (dict) from [(grid, count, area)]; each area's peak and size are
    derived from its squares (peak = highest count, first one on ties)."""
    areas = {}
    for grid, count, area in squares:
        a = areas.setdefault(area, {"id": area, "peak": list(grid), "peak_count": count, "size": 0})
        a["size"] += 1
        if count > a["peak_count"]:
            a["peak"], a["peak_count"] = list(grid), count
    return {"dataset": dataset, "subset": subset, "tau": tau, "T": T, "delta": delta,
            "squares": [{"grid": list(g), "count": c, "area": a} for g, c, a in squares],
            "areas": [areas[k] for k in sorted(areas)]}


# Hand-made reference, T = 431, delta = 5 (L-infinity, grid squares). Area 0 has three squares;
# (101, 102) touches (100, 101) only at a corner, so it belongs to area 0. Area 1 has two squares.
TWO_AREAS = a5_reference([
    ((100, 100), 500, 0), ((100, 101), 520, 0), ((101, 102), 450, 0),
    ((200, 200), 440, 1), ((201, 200), 460, 1),
])


def two_areas():
    return ev.parse_reference(copy.deepcopy(TWO_AREAS))


def test_reference_with_two_areas_hand_computed():
    ref = two_areas()
    assert len(ref["areas"]) == 2 and len(ref["squares"]) == 5
    assert ref["areas"][0]["peak"] == (100, 101) and ref["areas"][0]["size"] == 3
    reported = [
        (103, 104),  # L-inf 2 from (101, 102): area 0
        (102, 99),   # L-inf 2 from (100, 100): area 0
        (300, 300),  # far from every square
    ]
    m = ev.match_reference(reported, ref)
    # Area recall: area 0 is hit, area 1 is not -> 1/2.
    assert m["area_recall"] == approx(1 / 2)
    assert (m["areas_found"], m["reference_areas"], m["found"]) == (1, 2, [0])
    # Precision: 2 of the 3 reported locations are within 5 of a square in Q.
    assert m["precision"] == approx(2 / 3)
    # F1 = 2 * 2/3 * 1/2 / (2/3 + 1/2) = (2/3) / (7/6) = 4/7.
    assert m["f1"] == approx(4 / 7)
    assert (m["outputs"], m["reference_squares"]) == (3, 5)

    # One more location within 5 of area 1 (L-inf 5 from (201, 200)): both areas found.
    m = ev.match_reference(reported + [(206, 195)], ref)
    assert m["area_recall"] == 1.0 and m["areas_found"] == 2
    assert m["precision"] == approx(3 / 4)
    # F1 = 2 * 3/4 * 1 / (3/4 + 1) = 6/7.
    assert m["f1"] == approx(6 / 7)


def test_many_locations_in_one_area_count_once_for_recall():
    m = ev.match_reference([(100, 100), (100, 101), (101, 102), (99, 99)], two_areas())
    assert (m["area_recall"], m["precision"], m["areas_found"]) == (0.5, 1.0, 1)


def test_delta_boundary_is_inclusive_and_l_infinity():
    ref = ev.parse_reference(a5_reference([((0, 0), 500, 0)]))
    hit = lambda g, r=ref: ev.match_reference([g], r)["precision"]
    assert hit((5, -5)) == 1.0    # L-inf 5 (Euclidean 7.07): within
    assert hit((-5, 5)) == 1.0
    assert hit((5, 0)) == 1.0
    assert hit((6, 0)) == 0.0     # L-inf 6
    assert hit((0, -6)) == 0.0
    assert hit((6, 6)) == 0.0
    assert ev.match_reference([(5, -5)], ref)["area_recall"] == 1.0
    assert ev.match_reference([(6, 0)], ref)["area_recall"] == 0.0
    # The radius is the reference's delta.
    ref4 = ev.parse_reference(a5_reference([((0, 0), 500, 0)], delta=4))
    assert ev.match_reference([(5, -5)], ref4)["precision"] == 0.0
    assert ev.match_reference([(4, -4)], ref4)["precision"] == 1.0


def test_areas_touch_diagonally():
    groups = lambda squares: sorted(sorted(a) for a in ev.connected_areas(squares))
    # A corner is enough (8-neighbourhood) ...
    assert groups([(0, 0), (1, 1)]) == [[(0, 0), (1, 1)]]
    assert groups([(0, 0), (1, -1)]) == [[(0, 0), (1, -1)]]
    # ... a chain of corners is one area ...
    assert len(ev.connected_areas([(0, 0), (1, 1), (2, 2), (3, 1)])) == 1
    # ... a gap of one square is not.
    assert groups([(0, 0), (2, 0)]) == [[(0, 0)], [(2, 0)]]
    assert groups([(0, 0), (2, 2)]) == [[(0, 0)], [(2, 2)]]
    assert len(ev.connected_areas([(0, 0), (0, 1), (5, 5)])) == 2
    assert ev.connected_areas([]) == []


def test_reference_areas_must_follow_the_diagonal_rule():
    # Diagonal neighbours split into two areas: rejected.
    with pytest.raises(ev.ReferenceFormatError, match="connected groups"):
        ev.parse_reference(a5_reference([((0, 0), 500, 0), ((1, 1), 450, 1)]))
    # Squares two apart merged into one area: rejected.
    with pytest.raises(ev.ReferenceFormatError, match="connected groups"):
        ev.parse_reference(a5_reference([((0, 0), 500, 0), ((2, 0), 450, 0)]))
    # The same squares as one corner-touching area, or as two separate areas: accepted.
    assert len(ev.parse_reference(a5_reference([((0, 0), 500, 0), ((1, 1), 450, 0)]))["areas"]) == 1
    assert len(ev.parse_reference(a5_reference([((0, 0), 500, 0), ((2, 0), 450, 1)]))["areas"]) == 2


def test_reference_rejects_any_other_format(tmp_path):
    def load(obj):
        path = tmp_path / "day_3of3.json"
        path.write_text(json.dumps(obj), encoding="utf-8")
        return ev.load_reference(path, "day", "3of3")

    assert len(load(copy.deepcopy(TWO_AREAS))["areas"]) == 2

    def broken(change):
        obj = copy.deepcopy(TWO_AREAS)
        change(obj)
        return obj

    bad = {
        "a plain list (the old format)": [[1003, 993], [991, 1004, 63]],
        "the old hotspots object": {"T": 107, "delta": 5, "hotspots": [{"grid": [1, 2]}]},
        "missing areas": broken(lambda o: o.pop("areas")),
        "unexpected field": broken(lambda o: o.update(N=21581)),
        "square without area": broken(lambda o: o["squares"][0].pop("area")),
        "grid in degrees": broken(lambda o: o["squares"][0].update(grid=[30.27, -97.75])),
        "count as bool": broken(lambda o: o["squares"][0].update(count=True)),
        "count below T": broken(lambda o: o["squares"][0].update(count=430)),
        "square twice": broken(lambda o: o["squares"].append(dict(o["squares"][0]))),
        "unknown area id": broken(lambda o: o["squares"][0].update(area=7)),
        "wrong size": broken(lambda o: o["areas"][0].update(size=4)),
        "peak not the highest": broken(lambda o: o["areas"][0].update(peak=[100, 100],
                                                                      peak_count=500)),
        "other subset": broken(lambda o: o.update(subset="1of3")),
        "tau out of range": broken(lambda o: o.update(tau=2)),
    }
    for name, obj in bad.items():
        with pytest.raises(ev.ReferenceFormatError):
            load(obj)
            pytest.fail(f"accepted: {name}")
    (tmp_path / "day_3of3.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ev.ReferenceFormatError, match="cannot read JSON"):
        ev.load_reference(tmp_path / "day_3of3.json")


def test_match_empty_cases():
    m = ev.match_reference([], two_areas())
    assert (m["area_recall"], m["precision"], m["f1"], m["areas_found"]) == (0.0, None, None, 0)
    empty = ev.parse_reference(a5_reference([]))
    m = ev.match_reference([(100, 100)], empty)
    assert (m["area_recall"], m["precision"], m["f1"], m["reference_squares"]) == (None, 0.0,
                                                                                   None, 0)
    assert ev.f1_score(0.0, 0.0) == 0.0


def test_grid_conversion_is_mosaics_and_coordinate_less_outputs_do_not_count():
    # lat_grid = round((30.27 - 29.267) * 1000), lon_grid = round((-97.75 + 98.743) * 1000)
    assert ev.to_grid(30.27, -97.75) == (1003, 993)
    output = [{"label": "x", "lat": 30.27, "lon": -97.75, "count": 7},
              {"label": "triple", "lat": None, "lon": None, "count": None}]
    assert ev.reported_grid(output) == [(1003, 993)]


def test_run_metrics_with_a_reference():
    ref = ev.parse_reference(a5_reference([((1003, 993), 3238, 0), ((1003, 994), 3000, 0),
                                           ((935, 1076), 500, 1)]))
    line = example_line(output=[
        {"label": "a", "lat": 30.272, "lon": -97.748, "count": 120},  # grid (1005, 995): area 0
        {"label": "b", "lat": 30.4, "lon": -97.9, "count": 50},       # grid (1133, 843): far
    ])
    m = ev.run_metrics(line, ref)
    assert (m["outputs"], m["areas_found"]) == (2, 1)
    assert (m["area_recall"], m["precision"], m["f1"]) == (0.5, 0.5, 0.5)


# --- Self-check (A5) ---------------------------------------------------------------------------


def truth_and_line():
    cells = [
        {"label": "grid (1003, 993)", "lat": 30.27, "lon": -97.75, "grid": [1003, 993], "count": 4},
        {"label": "grid (991, 1004)", "lat": 30.258, "lon": -97.739, "grid": [991, 1004],
         "count": 2},
    ]
    truth = {"N": 7194, "T": 35, "cells": copy.deepcopy(cells)}
    line = example_line(output=[{k: c[k] for k in ("label", "lat", "lon", "count")} for c in cells])
    line["n_clients"], line["params"]["threshold_abs"] = 7194, 35
    return truth, line


def test_self_check_passes_on_the_exact_answer():
    truth, line = truth_and_line()
    c = ev.self_check(line, truth)
    assert (c["precision"], c["recall"], c["exact"], c["ok"]) == (1.0, 1.0, True, True)


def test_self_check_flags_a_missing_cell_a_wrong_count_and_a_wrong_setup():
    truth, line = truth_and_line()
    line["output"] = line["output"][:1]
    c = ev.self_check(line, truth)
    assert (c["precision"], c["recall"], c["ok"]) == (1.0, 0.5, False)

    truth, line = truth_and_line()
    line["output"][1]["count"] = 3
    c = ev.self_check(line, truth)
    assert (c["precision"], c["recall"], c["exact"], c["ok"]) == (1.0, 1.0, False, False)

    truth, line = truth_and_line()
    truth["cells"][0]["grid"] = [1003, 994]
    c = ev.self_check(line, truth)
    assert not c["ok"] and "grid coordinates differ" in c["problems"]

    truth, line = truth_and_line()
    line["n_clients"] = 7000
    assert not ev.self_check(line, truth)["ok"]
    assert not ev.self_check(line, None)["ok"]


# --- Leakage, statistics, summary --------------------------------------------------------------


def test_leakage_kept_is_the_frontier_and_pruned_the_rest():
    leakage = {"revealed_aggregates_total": 14, "revealed_aggregates_per_level": [2, 4, 8],
               "revealed_kind": "count", "client_validity_bits": 200,
               "frontier_per_level": [2, 4, 4]}
    m = ev.leakage_metrics(leakage)
    assert m == {"revealed_aggregates_total": 14, "revealed_kept_total": 10,
                 "revealed_pruned_total": 4, "frontier_final": 4, "client_validity_bits": 200}


def test_summarize():
    assert ev.summarize([5, 1, 3, None]) == (3, 1, 5)
    assert ev.summarize([1, 2, 3, 4]) == (2.5, 1, 4)
    assert ev.summarize([None]) == (None, None, None)


def runs_with_online(values_and_warmup):
    runs = []
    for rep, (online, warm) in enumerate(values_and_warmup):
        line = example_line(rep=rep, warmup=warm)
        line["phases"]["online_ms"] = online
        line["phases"]["online_steps"] = [{"name": "flp", "ms": online / 2},
                                          {"name": "level_0", "ms": online / 2}]
        runs.append({"line": line, "metrics": ev.run_metrics(line),
                     "check": {"precision": 1.0, "recall": 1.0, "exact": True, "ok": True}})
    return runs


def test_summary_row_excludes_warmups():
    runs = runs_with_online([(1000.0, True), (10.0, False), (30.0, False), (20.0, False)])
    row = ev.summary_row(runs)
    assert (row["online_ms_median"], row["online_ms_min"], row["online_ms_max"]) == (20.0, 10.0, 30.0)
    assert (row["reps_measured"], row["warmups_excluded"]) == (3, 1)
    assert row["online_steps_median_ms"] == "flp=10.0;level_0=10.0"
    assert row["selfcheck_exact"] is True and row["selfcheck_failed_runs"] == 0
    assert row["revealed_per_level"] == "2;4" and row["frontier_per_level"] == "1;2"
    assert row["area_recall_median"] is None and row["reference_areas"] is None
    assert row["reference_squares"] is None
    assert row["delta"] == bc.DELTA and row["threshold_frac"] == 0.005


def test_summary_row_with_a_reference():
    runs = runs_with_online([(10.0, False)])
    runs[0]["metrics"] = ev.run_metrics(runs[0]["line"], two_areas())
    row = ev.summary_row(runs, reference=two_areas())
    assert (row["reference_areas"], row["reference_squares"]) == (2, 5)
    # The example line reports one location far from both areas.
    assert (row["area_recall_median"], row["precision_median"], row["areas_found_median"]) == (
        0.0, 0.0, 0)


def test_summary_columns_are_unique_and_the_metrics_are_derived_from_them():
    assert len(ev.SUMMARY_COLUMNS) == len(set(ev.SUMMARY_COLUMNS))
    assert {"online_ms", "online_est_lan_ms", "online_est_wan_ms", "area_recall", "precision",
            "f1", "areas_found", "outputs", "revealed_pruned_total"} <= set(ev.RUN_METRICS)
    assert {"threshold_frac", "reference_areas", "reference_squares"} <= set(ev.SUMMARY_COLUMNS)
    assert not any(c.startswith(("count_error", "recall_", "reference_hotspots"))
                   for c in ev.SUMMARY_COLUMNS)
    assert "client_keygen_ms" not in ev.RUN_METRICS
    assert not any(m.startswith("online_steps") for m in ev.RUN_METRICS)


def test_summary_csv_round_trip(tmp_path):
    row = ev.summary_row(runs_with_online([(10.0, False), (30.0, False)]))
    path = tmp_path / "summary_mastic.csv"
    ev.write_summary([row], path)
    back = ev.read_summary(path)[0]
    assert tuple(back) == ev.SUMMARY_COLUMNS
    assert back["online_ms_median"] == 20.0 and back["area_recall_median"] is None
    assert back["encoding"] == "geohash" and back["selfcheck_exact"] is True
    assert back["n_clients"] == 7194.0 and back["threshold_frac"] == 0.005

    bad = tmp_path / "bad.csv"
    bad.write_text("system,encoding\nmosaic,h3\n", encoding="utf-8")
    with pytest.raises(ValueError):
        ev.read_summary(bad)


def test_mastic_checks_on_a_day_row():
    row = dict.fromkeys(ev.SUMMARY_COLUMNS)
    row.update(system="mastic", encoding="mosaic_grid", subset="3of3", n_clients=21581.0,
               data_bits=22.0, rounds_extra_max=0.0, rounds_online_min=49.0,
               rounds_online_max=49.0, rounds_upload_min=22.0, rounds_upload_max=22.0,
               client_validity_bits_min=43162.0, client_validity_bits_max=43162.0,
               client_upload_bytes_per_client=3412.0, bytes_upload_per_client_median=3412.003,
               bytes_online_server_to_server_max=0.0)
    checks = ev.mastic_checks([row])
    assert len(checks) == 5 and all(ok for *_, ok in checks)
    row["rounds_online_max"] = 50.0
    assert not all(ok for *_, ok in ev.mastic_checks([row]))


# --- tau per system (A2) and rows keyed by tau (A10) -------------------------------------------


def test_tau_is_per_system_and_explicit():
    assert bc.THRESHOLDS == {"mastic": 0.005, "mosaic": 0.02}
    assert bc.THRESHOLD_FRAC == 0.005 and bc.REFERENCE_TAU == 0.02 and bc.REFERENCE_DELTA == 5
    assert [bc.threshold_abs(n, 0.005) for n in (7194, 14387, 21581)] == [35, 71, 107]
    assert [bc.threshold_abs(n, 0.02) for n in (7194, 14387, 21581)] == [143, 287, 431]
    assert bc.threshold_abs(10, 0.005) == 1
    with pytest.raises(TypeError):
        bc.threshold_abs(21581)  # tau has no default any more


def test_ground_truth_file_names_per_tau():
    assert bc.ground_truth_path("day", "3of3", 0.005).name == "day_3of3.json"
    assert bc.ground_truth_path("day", "3of3", 0.02).name == "day_3of3_tau0.02.json"


def test_existing_result_lines_stay_valid():
    path = bc.BENCH / "results" / "raw" / "mastic.jsonl"
    if not path.exists():
        pytest.skip("no raw results")
    lines = [json.loads(t) for t in path.read_text(encoding="utf-8").splitlines() if t.strip()]
    assert lines and all(not bc.validate_result(x) for x in lines)
    assert all(x["params"]["threshold_abs"] == bc.threshold_abs(x["n_clients"],
                                                                x["params"]["threshold_frac"])
               for x in lines)


def test_evaluate_makes_one_row_per_tau(monkeypatch, tmp_path):
    truths = {}

    def fake_truth(dataset, subset, tau):
        t = bc.threshold_abs(7194, tau)
        cells = [] if tau == 0.02 else [{"label": "9v6s0j9", "lat": 30.265, "lon": -97.732,
                                         "grid": [998, 1011], "count": 120}]
        truths[tau] = {"path": tmp_path / f"{tau}.json", "threshold_frac": tau,
                       "encodings": {"geohash": {"N": 7194, "T": t, "cells": cells}}}
        return truths[tau]

    monkeypatch.setattr(ev, "load_ground_truth", fake_truth)
    raw_runs = []
    for tau in (0.005, 0.02):
        for rep in (0, 1, 2):
            line = example_line(rep=rep, warmup=rep == 0, n_clients=7194)
            line["params"]["threshold_frac"] = tau
            line["params"]["threshold_abs"] = bc.threshold_abs(7194, tau)
            if tau == 0.02:
                line["output"] = []
            raw_runs.append({"line": line, "where": f"test:{tau}:{rep}"})
    groups, rows, *_ = ev.evaluate({"runs": raw_runs, "clients": []}, tmp_path / "no-reference")
    assert sorted(k[-1] for k in groups) == [0.005, 0.02]
    by_tau = {r["threshold_frac"]: r for r in rows["mastic"]}
    assert set(by_tau) == {0.005, 0.02}
    assert (by_tau[0.005]["threshold_abs"], by_tau[0.02]["threshold_abs"]) == (35, 143)
    assert (by_tau[0.005]["outputs_median"], by_tau[0.02]["outputs_median"]) == (1, 0)
    assert all(r["selfcheck_failed_runs"] == 0 and r["reps_measured"] == 2 for r in by_tau.values())


def summary_rows_for(system, tau, t, n=7194.0, delta=5):
    row = dict.fromkeys(ev.SUMMARY_COLUMNS)
    row.update(system=system, encoding="geohash", dataset="day", subset="1of3",
               threshold_frac=tau, n_clients=n, threshold_abs=t, delta=delta,
               online_ms_median=1000.0 if system == "mastic" else 2000.0)
    return row


def test_parameter_checks_with_several_taus():
    rows = [summary_rows_for("mastic", 0.005, 35.0), summary_rows_for("mosaic", 0.02, 143.0),
            summary_rows_for("mosaic", 0.005, 35.0)]
    # Different taus per system are expected; N, delta and T at the shared tau agree.
    assert ev.parameter_mismatches(rows) == []
    rows[2]["threshold_abs"] = 36.0
    fields = {f for _, _, f, _ in ev.parameter_mismatches(rows)}
    assert fields == {"threshold_abs at τ=0.005", "threshold_abs != max(1, floor(τ·N))"}
    rows = [summary_rows_for("mastic", 0.005, 35.0), summary_rows_for("mosaic", 0.02, 143.0,
                                                                       n=7195.0)]
    assert {f for _, _, f, _ in ev.parameter_mismatches(rows)} == {"n_clients"}
    assert ev.duplicate_keys(rows + rows[:1]) == [("mastic", "day", "geohash", "1of3", 0.005)]


def test_comparison_and_order_with_several_taus():
    rows = [summary_rows_for("mosaic", 0.02, 143.0), summary_rows_for("mastic", 0.005, 35.0),
            summary_rows_for("mosaic", 0.005, 35.0)]
    assert ev.series_of(rows, ["mastic", "mosaic"]) == [("mastic", 0.005), ("mosaic", 0.005),
                                                        ("mosaic", 0.02)]
    ordered = ev.row_order(rows, ["mastic", "mosaic"])
    assert [(r["system"], r["threshold_frac"]) for r in ordered] == [
        ("mastic", 0.005), ("mosaic", 0.005), ("mosaic", 0.02)]
    md = ev.comparison_md(rows, ["mastic", "mosaic"], {"mastic": "a", "mosaic": "b"})
    assert "Online (mosaic τ=0.02)" in md and "Online ratio mosaic τ=0.02" in md
    assert "Common parameters are consistent" in md
    # Online medians 1000 (mastic) and 2000 (mosaic, both taus): ratios 2.00 against mastic.
    online_row = next(line for line in md.splitlines() if line.startswith("| `geohash`"))
    assert online_row.count("2.00") == 2


def test_old_summary_without_tau_is_rejected(tmp_path):
    old = [c for c in ev.SUMMARY_COLUMNS if c != "threshold_frac"]
    path = tmp_path / "summary_old.csv"
    path.write_text(",".join(old) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="threshold_frac"):
        ev.read_summary(path)
