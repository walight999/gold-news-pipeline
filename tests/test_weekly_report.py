"""Weekly performance/tuning dashboard — pure aggregation + evidence-cited
tuning candidates. No I/O (main.run_weekly_report wires it to the store)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from src import weekly_report as wr

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat()


def test_summarize_delivery_windows_and_sums():
    rows = [
        {"date_ict": "2026-09-10", "n_sent": 20, "n_failed": 2, "by_route": json.dumps({"digest": 15, "alert": 5})},
        {"date_ict": "2026-09-09", "n_sent": 10, "n_failed": 0, "by_route": json.dumps({"digest": 10})},
        {"date_ict": "2026-08-01", "n_sent": 99, "n_failed": 9, "by_route": "{}"},  # outside 7d window
    ]
    since = (NOW - timedelta(days=7)).strftime("%Y-%m-%d")
    d = wr.summarize_delivery(rows, since)
    assert d["n_sent"] == 30 and d["n_failed"] == 2
    assert d["by_route"]["digest"] == 25 and d["by_route"]["alert"] == 5
    assert d["fail_rate"] == round(2 / 32, 3)


def test_routing_precision_groups_and_hit_rate():
    rows = [
        {"topic_bucket": "rate_policy", "routed_as": "archive", "first_seen_ts": _iso(NOW - timedelta(days=1)),
         "xau_return_15m": 0.20},
        {"topic_bucket": "rate_policy", "routed_as": "archive", "first_seen_ts": _iso(NOW - timedelta(days=2)),
         "xau_return_15m": -0.05},
        {"topic_bucket": "geopolitics", "routed_as": "alert", "first_seen_ts": _iso(NOW - timedelta(days=1)),
         "xau_return_15m": 0.02},
        {"topic_bucket": "old", "routed_as": "archive", "first_seen_ts": _iso(NOW - timedelta(days=30)),
         "xau_return_15m": 0.9},  # outside window → excluded
    ]
    out = wr.routing_precision(rows, NOW - timedelta(days=7))
    rp = next(r for r in out if r["topic"] == "rate_policy")
    assert rp["n"] == 2 and rp["hit_pct"] == 50.0     # one 0.20 hit, one 0.05 miss
    assert rp["avg_abs_move"] == round((0.20 + 0.05) / 2, 3)
    assert all(r["topic"] != "old" for r in out)


def test_gradeability_windowed_exclusion():
    rows = [
        {"predicted_dir": "bull", "first_seen_ts": _iso(NOW - timedelta(days=1)),
         "xau_return_15m": 0.05, "xau_return_30m": 0.25},
        {"predicted_dir": "bear", "first_seen_ts": _iso(NOW - timedelta(days=1)),
         "xau_return_15m": 0.30, "xau_return_30m": 0.30},
        {"predicted_dir": "", "first_seen_ts": _iso(NOW - timedelta(days=1)),
         "xau_return_15m": 0.9},  # not directional → ignored
    ]
    g = wr.gradeability(rows, NOW - timedelta(days=7), flat_pct=0.10, windows=("15m", "30m"))
    assert g["15m"] == {"graded": 1, "flat": 1}
    assert g["30m"] == {"graded": 2, "flat": 0}


def test_classifier_cost_parses_blob():
    row = {"items_last_hour": json.dumps({
        "month": "2026-09", "month_tokens_in": 2000000, "month_tokens_out": 167000,
        "month_hq_tokens_in": 499000, "month_hq_tokens_out": 36000})}
    c = wr.classifier_cost(row)
    assert c["tokens_in"] == 2000000 and c["hq_tokens_in"] == 499000
    assert wr.classifier_cost(None) == {}


def test_source_snapshot_apify_and_stale():
    rows = [
        {"source_id": "_apify_truth", "items_last_hour": 7},
        {"source_id": "_apify", "items_last_hour": 3},
        {"source_id": "_classifier_health", "items_last_hour": "{}"},   # synthetic, ignored
        {"source_id": "forexlive", "last_success_ts": _iso(NOW - timedelta(minutes=10))},   # fresh
        {"source_id": "benzinga", "last_success_ts": _iso(NOW - timedelta(hours=20))},       # stale
    ]
    s = wr.source_snapshot(rows, NOW)
    assert s["apify_entries_last_run"] == {"_apify_truth": 7, "_apify": 3}
    assert s["stale_rss_sources"] == 1


def test_tuning_candidate_under_routed():
    routing = [
        {"topic": "rate_policy", "route": "archive", "n": 100, "hit_pct": 28, "avg_abs_move": 0.140},
        {"topic": "inflation", "route": "digest", "n": 100, "hit_pct": 24, "avg_abs_move": 0.110},
    ]
    cands = wr.tuning_candidates(routing, {"graded": 0}, {}, min_n=20)
    assert any("UNDER-routed" in c and "rate_policy" in c for c in cands)


def test_tuning_candidate_over_routed():
    routing = [
        {"topic": "inflation", "route": "digest", "n": 100, "hit_pct": 30, "avg_abs_move": 0.150},
        {"topic": "geopolitics", "route": "alert", "n": 100, "hit_pct": 20, "avg_abs_move": 0.090},
    ]
    cands = wr.tuning_candidates(routing, {"graded": 0}, {}, min_n=20)
    assert any("OVER-routed" in c and "geopolitics" in c for c in cands)


def test_tuning_candidate_band_recommendation():
    grade = {"15m": {"graded": 10, "flat": 40}, "30m": {"graded": 35, "flat": 15}}
    cands = wr.tuning_candidates([], {"graded": 0}, grade, min_n=20)
    assert any("30m" in c and "flat-band" in c for c in cands)


def test_no_candidates_when_clean():
    routing = [{"topic": "inflation", "route": "digest", "n": 100, "hit_pct": 30, "avg_abs_move": 0.120}]
    grade = {"15m": {"graded": 40, "flat": 10}, "30m": {"graded": 42, "flat": 8}}
    cands = wr.tuning_candidates(routing, {"graded": 50, "accuracy_pct": 62}, grade, min_n=20)
    assert cands == []


def test_build_weekly_report_assembles_all_sections():
    cal = [{"topic_bucket": "rate_policy", "routed_as": "archive",
            "first_seen_ts": _iso(NOW - timedelta(days=1)), "xau_return_15m": 0.2,
            "predicted_dir": "bull", "xau_return_30m": 0.3}]
    rep = wr.build_weekly_report(
        NOW, delivery_rows=[], scorecard_rows=[], calibration_rows=cal,
        source_rows=[], classifier_health_row=None)
    assert rep["window_days"] == 7
    for k in ("delivery", "routing", "scorecard", "gradeability", "classifier_cost",
              "sources", "tuning_candidates"):
        assert k in rep
