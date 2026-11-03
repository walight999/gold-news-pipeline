"""2026-11-03: one breaking card per central-bank speaker burst.

Live 10-21 → 11-03: Fed's Musalem 5 cards in one minute (10-30 23:31 ICT),
RBA 4 + Bullock 3 (11-03), BoJ Ueda 3 (10-30), BoK 3 (10-22). Titles below are
the real en_title values from content_log.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from src import delivery_stats, digest, speaker_burst
from src import store as store_mod

NOW = datetime(2026, 11, 3, 4, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("title,key", [
    ("Fed's Musalem: US central bank to hike interest rates again soon", "fed:musalem"),
    ("Fed’s Musalem: inflation expectations remain steady", "fed:musalem"),
    ("BoJ's Governor Ueda: Japan's financial conditions are accommodative.", "boj:ueda"),
    ("RBA Governor Bullock: rate increase was necessary to reduce inflation", "rba:bullock"),
    ("RBA's Bullock: Labour Market Remains Resilient, With Unemployment Relatively Low", "rba:bullock"),
    ("RBA: inflation now expected to stay above 3% for next year", "rba"),
    ("Bok: Thursday's rate decision was unanimous", "bok"),
    ("ECB's Lane: AI-related price pressures are visible", "ecb:lane"),
    ("FOMC Chair Powell: we are not on a preset course", "fed:powell"),
    ("Bank of Japan board member Masu: underlying inflation remains under 2%", "boj:masu"),
    ("BOJ'S UEDA: WILL RAISE RATES IF OUTLOOK REALISED", "boj:ueda"),
])
def test_speaker_key_central_bank_lines(title, key):
    assert speaker_burst.speaker_key(title) == key


@pytest.mark.parametrize("title", [
    "RBA forecasts unemployment at 4.7% in Q4 2026, 4.8% in Q2 2027",   # no "Speaker:" prefix
    "Trump: we will hit Iran very hard",          # not a central bank — spans unrelated stories
    "US CPI: 2.8% y/y vs 2.9% expected",          # data line
    "BREAKING: Fed's Musalem says hike soon",     # wire tag, not a speaker
    "Fed Q3 2026 survey: banks tighten",          # digits in prefix
    "",
])
def test_speaker_key_ignores_everything_else(title):
    assert speaker_burst.speaker_key(title) is None


MUSALEM = [
    ("m1", "Fed's Musalem: US central bank to hike interest rates again soon"),
    ("m2", "Fed's Musalem: inflation expectations remain steady"),
    ("m3", "Fed's Musalem: wider economy and labor market robust"),
    ("x1", "ECB's Lane: AI-related price pressures are visible"),
    ("m4", "Fed's Musalem: financial conditions continue to be accommodative"),
    ("o1", "Oil jumps 4% after Houthi attack on tanker"),
]


def test_plan_bundles_one_speaker_and_leaves_others_alone():
    extras, absorbed, deferred = speaker_burst.plan(MUSALEM, set())
    assert extras == {"m1": ["m2", "m3", "m4"]}
    assert absorbed == {"m2": "m1", "m3": "m1", "m4": "m1"}
    assert deferred == set()


def test_plan_defers_a_speaker_in_cooldown():
    extras, absorbed, deferred = speaker_burst.plan(MUSALEM, {"fed:musalem"})
    assert extras == {} and absorbed == {}
    assert deferred == {"m1", "m2", "m3", "m4"}


def _store():
    s = store_mod.Store(sheet_id="x", creds_json="{}")
    for tab in store_mod.SCHEMAS:
        s.data[tab] = {}
    return s


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_recent_speaker_keys_window_and_routes():
    s = _store()
    rows = [("a", "RBA Governor Bullock: rate increase was necessary", "breaking", 5),
            ("b", "Fed's Musalem: hike soon", "merged", 10),
            ("c", "ECB's Lane: AI prices", "breaking", 40),      # outside 15 min
            ("d", "BoJ's Ueda: rates on hold", "digest", 2)]     # digest doesn't start a cooldown
    for eid, title, route, mins in rows:
        s.upsert("event_state", {"event_id": eid, "title": title})
        s.upsert("sent_log", {"event_id": eid, "route_type": route, "sent_ts": _iso(NOW - timedelta(minutes=mins))})
    assert speaker_burst.recent_speaker_keys(s, NOW, 15) == {"rba:bullock", "fed:musalem"}


def test_merged_summary():
    assert speaker_burst.merged_summary("ctx", []) == "ctx"
    out = speaker_burst.merged_summary("", ["Fed's Musalem: a", "Fed's Musalem: b"])
    assert out.startswith("More remarks from the same speaker")
    assert "- Fed's Musalem: a\n- Fed's Musalem: b" in out


def test_merged_rows_keep_events_out_of_the_digest_and_out_of_delivery_counts():
    s = _store()
    s.upsert("event_state", {"event_id": "m2", "title": MUSALEM[1][1]})
    s.upsert("sent_log", speaker_burst.merged_row("m2", "m1", _iso(NOW)))
    assert digest._already_individually_sent(s, "m2")
    assert digest.recent_sent_titles(s, NOW, 6) == [MUSALEM[1][1]]
    s.upsert("sent_log", {"event_id": "m1", "route_type": "breaking", "sent_ts": _iso(NOW), "line_status": "200"})
    (day,) = delivery_stats.aggregate(s.all_rows("sent_log"), NOW - timedelta(days=2))
    assert day["n_sent"] == 1 and day["n_failed"] == 0
    assert day["by_route"] == '{"breaking": 1}'


# ---- the real run_once loop, network stubbed --------------------------------

def _event(eid, title, minute):
    from src.dedup import Event
    from src.normalizer import Item
    ts = NOW - timedelta(minutes=minute)
    it = Item(source_id="financialjuice", tier=1, role="macro", title=title, summary="",
              url=f"https://x/{eid}", published_ts=ts, first_seen_ts=ts)
    return Event(event_id=eid, cluster_key=eid, topic_bucket="rate_policy", entity="us",
                 direction_label="", items=[it])


class _Alert:
    should_send = True
    is_fallback = False
    reason = ""
    category = "central_bank"
    tone = "hawkish"
    headline_th = "หัวข้อ"
    body_th = ["เนื้อหา"]
    impact_th = "ผลต่อทอง"


def _run(monkeypatch, store, events):
    from src import main, router
    pushes, classified, tg = [], [], []

    class _Line:
        def push_flex(self, target, alt, bubble):
            pushes.append(alt)
            return {"status": 200}

    class _TG:
        def post(self, payload):
            tg.append(payload)

    def _classify(title, summary, **kw):
        classified.append((title, summary))
        return _Alert()

    src_cfg, kw_cfg, sched_cfg = main._load_configs()
    src_cfg = {**src_cfg, "sources": []}
    sched_cfg = {**sched_cfg, "quiet_hours": {"enabled": False}}
    monkeypatch.setattr(main, "_load_configs", lambda: (src_cfg, kw_cfg, sched_cfg))
    monkeypatch.setattr(main, "is_weekend_ict", lambda *a, **k: False)
    monkeypatch.setattr(main, "is_quiet_hours_ict", lambda *a, **k: False)
    monkeypatch.setattr(main.Store, "from_env", classmethod(lambda cls: store))
    monkeypatch.setattr(store, "connect", lambda: None)
    monkeypatch.setattr(store, "load_all", lambda: None)
    monkeypatch.setattr(store, "flush", lambda *a, **k: None)
    monkeypatch.setattr(main, "now_utc", lambda: NOW)

    async def _fetch_all(plan, store):
        return []
    monkeypatch.setattr(main, "fetch_all", _fetch_all)
    monkeypatch.setattr(main, "_collect_apify_entries", lambda *a, **k: [])
    monkeypatch.setattr(main, "_record_spot_tape", lambda *a, **k: None)
    monkeypatch.setattr(main.dedup, "cluster", lambda items, kw: events)
    monkeypatch.setattr(main.scorer, "score_event", lambda ev, kw: 5.0)
    monkeypatch.setattr(main, "decide", lambda evs, scores, store, **k: [
        router.Decision(ev, 5.0, router.Route.BREAKING, "test") for ev in evs])
    monkeypatch.setattr(main.LineClient, "from_env", classmethod(lambda cls: _Line()))
    monkeypatch.setattr(main, "_group_targets", lambda: "U1")
    monkeypatch.setattr(main.news_alert, "classify_and_rewrite", _classify)
    monkeypatch.setattr(main.price_feed, "get_xau_snapshot", lambda: None)
    monkeypatch.setattr(main.telegram_news.TelegramNewsClient, "from_env",
                        classmethod(lambda cls, **k: _TG()))
    monkeypatch.setattr(main, "breaking_bubble", lambda *a, **k: {"type": "bubble"})
    monkeypatch.setattr(main.telegram_news, "build_payload", lambda **k: k)
    monkeypatch.setattr(main, "record_line_outcome", lambda *a, **k: None)

    assert asyncio.run(main.run_once("cron")) == 0
    return pushes, classified, tg


def test_run_once_sends_one_card_for_a_speaker_burst(monkeypatch):
    s = _store()
    evs = [_event(eid, t, 1) for eid, t in MUSALEM]
    pushes, classified, tg = _run(monkeypatch, s, evs)

    # Musalem x4 → 1 card, Lane → 1, oil → 1
    assert len(pushes) == 3
    titles = [t for t, _ in classified]
    assert titles == [MUSALEM[0][1], MUSALEM[3][1], MUSALEM[5][1]]
    musalem_summary = classified[0][1]
    for _, t in (MUSALEM[1], MUSALEM[2], MUSALEM[4]):
        assert t in musalem_summary
    # absorbed events are ledgered as merged; the primary as breaking
    assert s.get("sent_log", ("m1", "breaking"))["line_status"] in (200, "200")
    for eid in ("m2", "m3", "m4"):
        assert s.get("sent_log", (eid, "merged"))["line_status"] == "into:m1"
        assert s.get("sent_log", (eid, "breaking")) is None
    # Telegram (English, per headline) still gets all six
    assert sorted(p["event_id"] for p in tg) == sorted(e for e, _ in MUSALEM)


def test_run_once_defers_a_speaker_still_in_cooldown(monkeypatch):
    s = _store()
    s.upsert("event_state", {"event_id": "old", "title": "Fed's Musalem: earlier remark on rates"})
    s.upsert("sent_log", {"event_id": "old", "route_type": "breaking",
                          "sent_ts": _iso(NOW - timedelta(minutes=5)), "line_status": "200"})
    evs = [_event(eid, t, 1) for eid, t in MUSALEM[:3]]
    pushes, classified, tg = _run(monkeypatch, s, evs)
    assert pushes == [] and classified == []
    for eid in ("m1", "m2", "m3"):
        assert s.get("sent_log", (eid, "breaking")) is None
        assert s.get("sent_log", (eid, "merged")) is None
