"""2026-10-19: FXStreet sat dead 10-14→19 (403 from Actions) and nobody knew —
its verify warning was auto-closed by the next cron, 110 stale warnings buried
it, and nothing surfaced dead sources. These guard the three fixes."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import yaml

from src import health, weekly_report
from src import store as store_mod
from src.line_flex import weekly_report_bubble

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _store():
    s = store_mod.Store(sheet_id="x", creds_json="{}")
    for tab in store_mod.SCHEMAS:
        s.data[tab] = {}
    return s


def test_config_fxstreet_via_apify_and_yahoo_disabled():
    cfg = yaml.safe_load(open("config/sources.yaml", encoding="utf-8"))
    src = {x["id"]: x for x in cfg["sources"]}
    # direct fetch is 403 from Actions even with a browser UA → off; recovered instead
    assert src["fxstreet"]["enabled"] is False
    rc = cfg["apify_recover"]
    assert rc["enabled"] is True
    assert [f["id"] for f in rc["feeds"]] == ["fxstreet_apify"]
    assert rc["feeds"][0]["organization"] == "fxstreet"        # same org for confirmation logic
    assert src["yahoo_finance"]["enabled"] is False
    from src.line_flex import SOURCE_NAMES
    assert SOURCE_NAMES["fxstreet_apify"] == "FXStreet"


def test_dead_sources_listed_for_enabled_only():
    rows = [
        {"source_id": "fxstreet", "last_success_ts": _iso(NOW - timedelta(days=5))},
        {"source_id": "cnbc", "last_success_ts": _iso(NOW - timedelta(minutes=20))},
        {"source_id": "yahoo_finance", "last_success_ts": _iso(NOW - timedelta(days=13))},  # disabled
        {"source_id": "newfeed", "last_success_ts": ""},                                     # never ok
        {"source_id": "_classifier_health", "last_success_ts": ""},                          # synthetic
    ]
    snap = weekly_report.source_snapshot(rows, NOW, enabled_ids={"fxstreet", "cnbc", "newfeed"})
    assert [d["id"] for d in snap["dead_sources"]] == ["newfeed", "fxstreet"]
    assert snap["dead_sources"][1]["days"] == 5.0
    # back-compat: no enabled list → no dead list
    assert weekly_report.source_snapshot(rows, NOW)["dead_sources"] == []


def test_weekly_card_shows_dead_sources():
    rep = {"delivery": {}, "scorecard": {}, "tuning_candidates": [],
           "sources": {"dead_sources": [{"id": "fxstreet", "days": 5.0}, {"id": "newfeed", "days": None}]}}
    text = json.dumps(weekly_report_bubble(rep, "2026-W43"), ensure_ascii=False)
    assert "fxstreet (5 วัน)" in text and "newfeed (ไม่เคยดึงได้)" in text
    rep["sources"]["dead_sources"] = []
    assert "48 ชม." not in json.dumps(weekly_report_bubble(rep, "W"), ensure_ascii=False)


def test_disabled_source_warnings_closed_others_kept():
    s = _store()
    for sid, wt in (("benzinga", "tier2_no_item"), ("yahoo_finance", "http_errors_streak"),
                    ("fxstreet", "http_errors_streak"), ("forexfactory_json", "verify_empty")):
        s.upsert("health_log", {"source_id": sid, "warning_type": wt,
                                "warning_ts": _iso(NOW), "resolved_ts": ""})
    n = health.resolve_disabled_source_warnings(s, {"benzinga", "yahoo_finance"})
    assert n == 2
    still_open = {r["source_id"] for r in s.all_rows("health_log") if not r["resolved_ts"]}
    assert still_open == {"fxstreet", "forexfactory_json"}
    assert health.resolve_disabled_source_warnings(s, {"benzinga"}) == 0     # idempotent


def test_cron_recovery_loop_skips_verify_warnings():
    # The loop in run_once must not clear verify_* (only verify_sources does).
    import inspect
    from src import main as m
    src = inspect.getsource(m.run_once)
    assert 'startswith("verify_")' in src
    vsrc = inspect.getsource(m.run_verify_sources)
    assert 's.get("user_agent")' in vsrc and "resolve_warning" in vsrc


def test_recovered_feed_with_zero_entries_is_dead():
    rows = [{"source_id": "_apify_recover:fxstreet_apify", "items_last_hour": "0"},
            {"source_id": "_apify_recover:other_apify", "items_last_hour": "30"},
            {"source_id": "_apify", "items_last_hour": "0"}]          # X scrape row: not a feed
    rows.append({"source_id": "_apify_recover:benzinga_apify", "items_last_hour": "0"})  # removed feed
    snap = weekly_report.source_snapshot(rows, NOW, enabled_ids={"fxstreet_apify", "other_apify"})
    assert snap["dead_sources"] == [{"id": "fxstreet_apify", "days": None, "why": "proxy 0 entries"}]
    rep = {"delivery": {}, "scorecard": {}, "tuning_candidates": [], "sources": snap}
    assert "fxstreet_apify (proxy 0 entries)" in json.dumps(weekly_report_bubble(rep, "W"), ensure_ascii=False)


def test_enabled_ids_include_recover_feeds_only_when_on():
    from src import main as m
    cfg = {"sources": [{"id": "cnbc", "enabled": True}, {"id": "fxstreet", "enabled": False}],
           "apify_recover": {"enabled": True, "feeds": [{"id": "fxstreet_apify"}]}}
    assert m._enabled_source_ids(cfg) == {"cnbc", "fxstreet_apify"}
    cfg["apify_recover"]["enabled"] = False
    assert m._enabled_source_ids(cfg) == {"cnbc"}
