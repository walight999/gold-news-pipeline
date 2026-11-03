"""2026-11-03: a Released card must not go out twice when FF re-times an event.

2026-10-30: BOJ Policy Rate / Outlook Report were listed at 02:30 UTC and
carded at 02:32 + 02:43; FF then moved them to 03:02. event_id hashes the
time, so the 03:52 run saw two "new" events and carded both again."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from src import calendar as cal
from src import delivery_stats
from src import main as m
from src import store as store_mod

LISTED = datetime(2026, 10, 30, 2, 30, tzinfo=timezone.utc)
RETIMED = datetime(2026, 10, 30, 3, 2, tzinfo=timezone.utc)


def _store():
    s = store_mod.Store(sheet_id="x", creds_json="{}")
    for tab in store_mod.SCHEMAS:
        s.data[tab] = {}
        s.dirty[tab] = set()
    return s


def _ev(title, when, forecast="0.75%", country="JPY"):
    return cal.CalEvent(event_id=cal._make_event_id(title, when.isoformat(), country),
                        title=title, country=country, impact="High",
                        forecast=forecast, previous="0.75%", dt_utc=when)


def _run_check(monkeypatch, s, events, now):
    pushed = []

    class _Line:
        def push_flex(self, target, alt, bubble):
            pushed.append(alt)
            return {"status": 200}

    from src import ff_scraper, spot_feed
    src_cfg, kw_cfg, sched_cfg = m._load_configs()
    monkeypatch.setattr(m, "_load_configs", lambda: (src_cfg, kw_cfg, sched_cfg))
    monkeypatch.setattr(m, "is_weekend_ict", lambda *a, **k: False)
    monkeypatch.setattr(m, "is_quiet_hours_ict", lambda *a, **k: False)
    monkeypatch.setattr(m.Store, "from_env", classmethod(lambda cls: s))
    monkeypatch.setattr(s, "connect", lambda: None)
    monkeypatch.setattr(s, "load_all", lambda: None)
    monkeypatch.setattr(s, "flush", lambda *a, **k: None)
    monkeypatch.setattr(m, "now_utc", lambda: now)
    monkeypatch.setattr(cal, "now_utc", lambda: now)
    monkeypatch.setattr(cal, "fetch_calendar", lambda *a, **k: events)
    for name in ("_record_pinger_spot_base", "_record_spot_tape", "_speech_plan",
                 "_dispatch_release_pingers", "record_line_outcome"):
        monkeypatch.setattr(m, name, lambda *a, **k: None)
    monkeypatch.setattr(m.macro_push, "push_next_event_window", lambda *a, **k: None)
    monkeypatch.setattr(m.LineClient, "from_env", classmethod(lambda cls: _Line()))
    monkeypatch.setattr(m, "_group_targets", lambda: "C1")
    monkeypatch.setattr(m.telegram_news.TelegramNewsClient, "from_env", classmethod(lambda cls, **k: None))
    monkeypatch.setattr(m.fred, "fetch_actual", lambda *a, **k: None)
    monkeypatch.setattr(ff_scraper, "scrape_current_week_actuals", lambda: {"x": "0.75%"})
    monkeypatch.setattr(ff_scraper, "lookup_actual_for_event", lambda ev, cache: "0.75%")
    monkeypatch.setattr(spot_feed, "move_since", lambda *a, **k: None)
    monkeypatch.setattr(m.news_alert, "explain_calendar_release", lambda **k: None)
    assert asyncio.run(m.run_calendar_check()) == 0
    return pushed


def test_retimed_event_gets_no_second_released_card(monkeypatch):
    s = _store()
    first = _run_check(monkeypatch, s,
                       [_ev("BOJ Policy Rate", LISTED), _ev("BOJ Outlook Report", LISTED, forecast="")],
                       now=LISTED + timedelta(minutes=13))
    assert len(first) == 1                       # both in one carousel push

    retimed = [_ev("BOJ Policy Rate", RETIMED), _ev("BOJ Outlook Report", RETIMED, forecast="")]
    assert {e.event_id for e in retimed}.isdisjoint(
        {_ev("BOJ Policy Rate", LISTED).event_id, _ev("BOJ Outlook Report", LISTED).event_id})
    second = _run_check(monkeypatch, s, retimed, now=RETIMED + timedelta(minutes=50))
    assert second == []
    for e in retimed:
        assert s.get("sent_log", (f"postcal:{e.event_id}", "calendar_post")) is None


def test_same_title_on_another_day_or_currency_still_goes_out(monkeypatch):
    s = _store()
    _run_check(monkeypatch, s, [_ev("CPI y/y", LISTED, forecast="2.9%", country="EUR")],
               now=LISTED + timedelta(minutes=5))
    next_day = LISTED + timedelta(days=1)
    pushed = _run_check(monkeypatch, s,
                        [_ev("CPI y/y", next_day, forecast="2.9%", country="EUR"),
                         _ev("CPI y/y", next_day, forecast="2.9%", country="CHF")],
                        now=next_day + timedelta(minutes=5))
    assert len(pushed) == 1 and "EUR" in pushed[0] and "+1" in pushed[0]   # one carousel, both cards
    assert len([r for r in s.all_rows("sent_log") if r["route_type"] == "calendar_post"]) == 3


def test_name_marker_is_not_a_delivery():
    s = _store()
    ev = _ev("BOJ Policy Rate", LISTED)
    s.upsert("sent_log", {"event_id": f"postcal:{ev.event_id}", "route_type": "calendar_post",
                          "sent_ts": "2026-10-30T02:43:02Z", "line_status": "200",
                          "release_ts": "2026-10-30T02:30:00Z"})
    s.upsert("sent_log", {"event_id": m._post_name_key(ev), "route_type": m.POST_NAME_ROUTE,
                          "sent_ts": "2026-10-30T02:43:02Z", "line_status": "marker"})
    rows = s.all_rows("sent_log")
    (day,) = delivery_stats.aggregate(rows, datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert (day["n_sent"], day["n_failed"]) == (1, 0)
    assert delivery_stats.monthly_pushes(rows, "2026-10") == {"calendar_post": {"pushes": 1, "cards": 1}}
