"""Tests for bench/scripts/evaluate.py.

- the network model (A4) on hand-computed numbers;
- the reference matching (A5) on a small hand-made example with known precision and recall;
- the self-check, the leakage totals, the summary statistics and the summary CSV.

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
    assert m["recall"] is None and m["precision"] is None  # no reference


# --- Matching against the reference (A5) -------------------------------------------------------

# Hand-made example, delta = 5 grid cells (L-infinity).
REFERENCE = [((100, 100), 100), ((200, 200), 50), ((300, 300), 20)]
REPORTED = [
    ((102, 103), 90),   # L-inf 3 from (100, 100): within
    ((200, 204), 45),   # L-inf 4 from (200, 200): within
    ((400, 400), 5),    # far from everything
    ((101, 101), 120),  # L-inf 1 from (100, 100): within, and the nearest to it
]


def test_match_hand_made_example():
    m = ev.match_reference(REPORTED, REFERENCE, delta=5)
    # Precision: 3 of the 4 reported locations are within 5 of a reference hotspot.
    assert m["precision"] == approx(3 / 4)
    # Recall: (100,100) and (200,200) are found, (300,300) is not.
    assert m["recall"] == approx(2 / 3)
    # F1 = 2 * 0.75 * 2/3 / (0.75 + 2/3) = 1 / (17/12) = 12/17.
    assert m["f1"] == approx(12 / 17)
    assert m["outputs"] == 4
    # Pairs: (100,100) -> (101,101): |120-100|/100 = 0.2; (200,200) -> (200,204): |45-50|/50 = 0.1.
    assert m["pairs"] == [((100, 100), (101, 101)), ((200, 200), (200, 204))]
    assert m["count_error"] == approx(0.15)


def test_match_radius_is_inclusive_and_l_infinity():
    ref = [((0, 0), 10)]
    assert ev.match_reference([((5, -5), 10)], ref, 5)["recall"] == 1.0  # L-inf 5, L2 7.07
    assert ev.match_reference([((6, 0), 10)], ref, 5)["recall"] == 0.0
    assert ev.match_reference([((0, 6), 10)], ref, 5)["precision"] == 0.0


def test_match_nearest_pair_tie_breaks():
    ref = [((0, 0), 100)]
    # Same L-inf (3): the Euclidean-nearer (3, 0) wins -> |90 - 100| / 100 = 0.1.
    m = ev.match_reference([((3, 3), 110), ((3, 0), 90)], ref, 5)
    assert m["pairs"] == [((0, 0), (3, 0))]
    assert m["count_error"] == approx(0.1)
    # Same L-inf and Euclidean distance: the larger count wins.
    m = ev.match_reference([((0, 3), 80), ((3, 0), 90)], ref, 5)
    assert m["pairs"] == [((0, 0), (3, 0))]


def test_match_empty_and_countless_cases():
    m = ev.match_reference([], REFERENCE, 5)
    assert (m["recall"], m["precision"], m["f1"], m["count_error"]) == (0.0, None, None, None)
    m = ev.match_reference(REPORTED, [], 5)
    assert (m["recall"], m["precision"], m["f1"]) == (None, 0.0, None)
    m = ev.match_reference(REPORTED, [((100, 100), None)], 5)  # reference without counts
    assert m["recall"] == 1.0 and m["count_error"] is None
    assert ev.f1_score(0.0, 0.0) == 0.0


def test_grid_conversion_is_mosaics():
    # lat_grid = round((30.27 - 29.267) * 1000), lon_grid = round((-97.75 + 98.743) * 1000)
    assert ev.to_grid(30.27, -97.75) == (1003, 993)
    assert ev.reported_points([{"label": "x", "lat": 30.27, "lon": -97.75, "count": 7}]) == [
        ((1003, 993), 7)]


def test_load_reference_formats(tmp_path):
    def load(obj):
        path = tmp_path / "ref.json"
        path.write_text(json.dumps(obj), encoding="utf-8")
        return ev.load_reference(path)

    assert load([[1003, 993], [991, 1004, 63]])["hotspots"] == [((1003, 993), None),
                                                                 ((991, 1004), 63)]
    r = load({"T": 107, "delta": 5, "hotspots": [{"grid": [1, 2], "count": 5}]})
    assert r["hotspots"] == [((1, 2), 5)] and r["meta"] == {"T": 107, "delta": 5}
    assert load({"cells": [{"lat_grid": 3, "lon_grid": 4}]})["hotspots"] == [((3, 4), None)]
    assert load([{"lat": 30.27, "lon": -97.75, "count": 3}])["hotspots"] == [((1003, 993), 3)]
    with pytest.raises(ValueError):
        load([{"x": 1}])
    with pytest.raises(ValueError):
        load({"something": 1})


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
    assert row["recall_median"] is None and row["reference_hotspots"] is None
    assert row["delta"] == bc.DELTA


def test_summary_columns_are_unique_and_the_metrics_are_derived_from_them():
    assert len(ev.SUMMARY_COLUMNS) == len(set(ev.SUMMARY_COLUMNS))
    assert {"online_ms", "online_est_lan_ms", "online_est_wan_ms", "recall", "precision", "f1",
            "count_error", "outputs", "revealed_pruned_total"} <= set(ev.RUN_METRICS)
    assert "client_keygen_ms" not in ev.RUN_METRICS
    assert not any(m.startswith("online_steps") for m in ev.RUN_METRICS)


def test_summary_csv_round_trip(tmp_path):
    row = ev.summary_row(runs_with_online([(10.0, False), (30.0, False)]))
    path = tmp_path / "summary_mastic.csv"
    ev.write_summary([row], path)
    back = ev.read_summary(path)[0]
    assert tuple(back) == ev.SUMMARY_COLUMNS
    assert back["online_ms_median"] == 20.0 and back["recall_median"] is None
    assert back["encoding"] == "geohash" and back["selfcheck_exact"] is True
    assert back["n_clients"] == 7194.0

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
