"""Research pipeline pure logic + the card history line."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from research import ff_history, learn, prices
from src import release_stats


# ---- ForexFactory month page JSON ----

def test_extract_days_handles_brackets_inside_strings():
    html = ('x window.calendarComponentStates[1] = {\ndays: [{"date":"Fri <span>Mar 1</span>",'
            '"events":[{"id":1,"name":"a ] tricky [ name","dateline":1709226000,"currency":"USD",'
            '"impactName":"high","actual":"0.4%","forecast":"0.3%","previous":"0.4%",'
            '"ebaseId":79,"actualBetterWorse":1}]}], more: 1};')
    days = ff_history.extract_days(html)
    evs = ff_history.events_from_days(days)
    assert evs[0]["title"] == "a ] tricky [ name"
    assert evs[0]["dt_utc"] == "2024-02-29T17:00:00Z"
    assert evs[0]["better_worse"] == 1


def test_masked_time_events_dropped():
    days = [{"events": [{"id": 1, "dateline": 1709226000, "timeMasked": True}]}]
    assert ff_history.events_from_days(days) == []


# ---- HistData parsing: fixed EST (UTC-5) → UTC ----

def test_parse_m1_converts_est_to_utc():
    m, o, c = prices.parse_m1("20240312 072900;2169.0;2170.0;2168.0;2169.975;0\n"
                              "20240312 072800;2168.0;2169.0;2167.0;2168.5;0\n")
    first = datetime.fromtimestamp(int(m[0]) * 60, tz=timezone.utc)
    assert first == datetime(2024, 3, 12, 12, 28, tzinfo=timezone.utc)   # sorted
    assert c[1] == 2169.975


# ---- learning helpers ----

def test_robust_scale_ignores_outliers():
    vals = [10, -12, 8, -9, 11, -10, 9, 5_000_000]     # one COVID-sized print
    s = learn.robust_scale(vals)
    assert 5 < s < 30


def test_robust_scale_needs_history():
    assert learn.robust_scale([1, 2, 3]) is None


def test_wilson_lb_below_point_estimate():
    assert learn.wilson_lb(9, 10) < 0.9
    assert learn.wilson_lb(0, 0) == 0.0


@pytest.mark.parametrize("ret,band,exp", [(0.05, 0.10, 0), (0.2, 0.10, 1), (-0.2, 0.10, -1), (None, 0.1, None)])
def test_outcome(ret, band, exp):
    assert learn.outcome(ret, band) == exp


def test_rule_sign_matches_live_rules():
    assert learn.rule_sign("Core CPI m/m", "USD") == 1
    assert learn.rule_sign("Unemployment Claims", "USD") == -1
    assert learn.rule_sign("Federal Funds Rate", "USD") is None
    assert learn.rule_sign("Trade Balance", "USD") is None


# ---- card history line ----

@pytest.fixture
def stats_file(tmp_path, monkeypatch):
    data = {"generated_from": ["2019-01-03T13:15:00Z", "2026-09-25T14:00:00Z"],
            "series": {"USD|Core CPI m/m": {"cells": {
                "XAU|5": {"n": 15, "hit": 0.8}, "XAU|15": {"n": 23, "hit": 0.696},
                "XAU|60": {"n": 30, "hit": 0.6}}},
                "USD|Tiny": {"cells": {"XAU|5": {"n": 3, "hit": 1.0}}}}}
    p = tmp_path / "rs.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(release_stats, "STATS_PATH", str(p))
    release_stats._load.cache_clear()
    yield
    release_stats._load.cache_clear()


def test_history_line(stats_file):
    line = release_stats.history_line_th("USD", "Core CPI m/m")
    assert line == "ย้อนหลัง 2019–2026 (n=23): ทองไปตามทิศนี้ 80% ใน 5 นาที · 70% ใน 15 นาที"


def test_history_line_hidden_when_thin_or_unknown(stats_file):
    assert release_stats.history_line_th("USD", "Tiny") is None
    assert release_stats.history_line_th("GBP", "CPI y/y") is None


# ---- HistData EU-summer-time correction ----

def test_eu_summer_mask_boundaries():
    import numpy as np

    def em(y, mo, d, h=12):
        return int(datetime(y, mo, d, h, tzinfo=timezone.utc).timestamp()) // 60
    m = np.array([em(2025, 3, 25), em(2025, 4, 1), em(2025, 10, 24), em(2025, 10, 28), em(2025, 1, 15)])
    assert prices._eu_summer_mask(m).tolist() == [False, True, True, False, False]


def test_parse_m1_summer_shift():
    # 2025-07-15 08:00 "EST" stamp = 13:00 UTC + the measured 60-min summer lag → 12:00 UTC
    m, _o, _c = prices.parse_m1("20250715 080000;1;1;1;1;0\n")
    assert datetime.fromtimestamp(int(m[0]) * 60, tz=timezone.utc) == datetime(2025, 7, 15, 12, 0, tzinfo=timezone.utc)


# ---- live composite call ----

@pytest.fixture
def model_file(tmp_path, monkeypatch):
    data = {
        "generated_from": ["2019-01-03T13:15:00Z", "2026-09-25T14:00:00Z"],
        "model": {
            "scales": {"USD|Non-Farm Employment Change": 50.0, "USD|Core PPI m/m": 0.1},
            "asset_dir": {"XAU|5": -1, "USDJPY|5": 1},
            "weights": {"USD|Non-Farm Employment Change@XAU|5": 0.5,
                        "USD|Non-Farm Employment Change@USDJPY|5": 0.6},
            "gates": {"XAU|5": {"threshold": 0.5}, "USDJPY|5": {"threshold": 0.5}},
        },
        "oos": {"XAU|5": {"gated": {"precision": 0.894, "right": 76, "wrong": 9}},
                "USDJPY|5": {"gated": {"precision": 0.937, "right": 104, "wrong": 7}}},
    }
    p = tmp_path / "rs.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(release_stats, "STATS_PATH", str(p))
    release_stats._load.cache_clear()
    yield
    release_stats._load.cache_clear()


def test_minute_call_strong_nfp_beat(model_file):
    calls = release_stats.minute_calls([("USD", "Non-Farm Employment Change", "162K", "55K")])
    assert [(c["asset"], c["dir"]) for c in calls] == [("XAU", -1), ("USDJPY", 1)]
    line = release_stats.minute_call_line_th([("USD", "Non-Farm Employment Change", "162K", "55K")])
    assert line.startswith("🎯 สัญญาณรวมผ่านเกณฑ์: ทอง ↓ 5 นาที (แม่นย้อนหลัง 89%, n=85)")


def test_minute_call_none_below_threshold_or_unweighted(model_file):
    # 60K vs 55K → z = 0.1, composite 0.05 < 0.5 threshold
    assert release_stats.minute_calls([("USD", "Non-Farm Employment Change", "60K", "55K")]) == []
    # Core PPI has no learned weight → contributes nothing
    assert release_stats.minute_calls([("USD", "Core PPI m/m", "0.5%", "0.2%")]) == []
    # non-USD ignored
    assert release_stats.minute_calls([("GBP", "Non-Farm Employment Change", "162K", "55K")]) == []
