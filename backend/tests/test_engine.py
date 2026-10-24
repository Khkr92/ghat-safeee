import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import Bend, Engine, ssd, zone_length  # noqa: E402

SPEC = json.loads((Path(__file__).resolve().parents[1] / "bends.json").read_text())["bends"][0]


def make():
    e = Engine()
    e.add_bend(Bend(**SPEC))
    return e, e.bends[SPEC["id"]]


def drive(e, b, vid, s, speed, t):
    lat, lon = b.line.point_at(s)
    return e.report(b.id, vid, lat, lon, speed, ts=t)


def test_ssd_matches_hand_calculation():
    # 40 km/h, f = 0.40: 11.11*2.5 + 11.11^2 / (2*9.81*0.40) = 27.78 + 15.73
    assert ssd(40, 0.40) == pytest.approx(43.5, abs=0.1)
    assert zone_length(40, 0.40) == pytest.approx(65.7, abs=0.1)
    assert zone_length(40, 0.30) > zone_length(40, 0.40)  # wet road needs a longer zone


def test_projection_round_trip():
    _, b = make()
    for s in (5, 60, 150, 250):
        lat, lon = b.line.point_at(s)
        s2, off = b.line.project(lat, lon)
        assert s2 == pytest.approx(s, abs=0.5)
        assert off < 0.5


def test_clear_when_road_is_empty():
    e, b = make()
    drive(e, b, "car-a", 50, 30, 0)
    a = drive(e, b, "car-a", 60, 24, 1)
    assert a["state"] == "clear"
    assert a["direction"] == "A->B"


def test_oncoming_warning_reaches_other_side():
    e, b = make()
    drive(e, b, "car-a", 70, 24, 0)
    drive(e, b, "car-a", 76, 24, 1)
    drive(e, b, "bus-b", b.bend_end + 40, 24, 0)
    alert_b = drive(e, b, "bus-b", b.bend_end + 34, 24, 1)
    assert alert_b["state"] == "oncoming"
    assert e.signs(b.id, now=1)["A"] == "oncoming"


def test_head_on_risk_when_oncoming_vehicle_is_inside_bend():
    e, b = make()
    drive(e, b, "car-a", b.bend_start + 5, 20, 0)
    drive(e, b, "car-a", b.bend_start + 10, 20, 1)
    drive(e, b, "bike-b", b.bend_end + 30, 22, 0)
    alert = drive(e, b, "bike-b", b.bend_end + 24, 22, 1)
    assert alert["state"] == "risk"


def test_overspeed_flag_and_single_log():
    e, b = make()
    drive(e, b, "car-a", 60, 45, 0)
    a = drive(e, b, "car-a", 72, 45, 1)
    assert a["state"] == "overspeed"
    drive(e, b, "car-a", 84, 45, 2)
    assert sum(ev["type"] == "overspeed" for ev in e.events) == 1


def test_off_route_fix_is_ignored():
    e, b = make()
    lat, lon = b.line.point_at(60)
    a = e.report(b.id, "far", lat + 0.01, lon, 30, ts=0)
    assert a["state"] == "off_route"
    assert e.state(b.id)["vehicles"] == 0


def test_stale_phone_is_dropped():
    e, b = make()
    drive(e, b, "car-a", 70, 24, 0)
    drive(e, b, "car-a", 76, 24, 1)
    assert e.signs(b.id, now=2)["B"] == "oncoming"
    assert e.signs(b.id, now=20)["B"] == "clear"


def test_cctv_counts_vehicles_without_app():
    e, b = make()
    e.cctv_update(b.id, approach_a=0, approach_b=0, in_bend_from_a=1, in_bend_from_b=0, ts=0)
    drive(e, b, "bus-b", b.bend_end + 40, 24, 0)
    alert = drive(e, b, "bus-b", b.bend_end + 34, 24, 1)
    assert alert["state"] == "risk"
