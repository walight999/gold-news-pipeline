"""2026-11-03: no late Released card for an event whose time FF filled in only
after the print ("Tentative" — BOJ Policy Rate / Outlook Report on 2026-10-30
went out 51 min late with no actual, the breaking route had it at +4 min).
Over 59 Released cards those two were the only ones without a T-15 card."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from src import calendar as cal
from src import main as m
from src import store as store_mod

# 2026-10-30 03:52 UTC: the run that sent the BoJ pair
NOW = datetime(2026, 10, 30, 3, 52, tzinfo=timezone.utc)


def _store():
    s = store_mod.Store(sheet_id="x", creds_json="{}")
    for tab in store_mod.SCHEMAS:
        s.data[tab] = {}
        s.dirty[tab] = set()
    return s


def _ev(eid, title, minutes_ago, country="JPY", forecast="0.75%"):
    return cal.CalEvent(event_id=eid, title=title, country=country, impact="High",
                        forecast=forecast, previous="0.75%",
                        dt_utc=NOW - timedelta(minutes=minutes_ago))


def _pre(s, eid):
    s.upsert("sent_log", {"event_id": f"precal:{eid}", "route_type": "calendar_pre",
                          "sent_ts": "2026-10-30T02:45:00Z", "line_status": "200"})


def test_late_and_no_t15_card_is_skipped():
    s = _store()
    ev = _ev("boj", "BOJ Policy Rate", 50)
    assert m._time_filled_after_print(s, ev, NOW, pre_enabled=True, late_cap_min=15)


def test_had_a_t15_card_is_never_held_back():
    s = _store()
    ev = _ev("cpi", "Tokyo Core CPI y/y", 120)
    _pre(s, "cpi")
    assert not m._time_filled_after_print(s, ev, NOW, pre_enabled=True, late_cap_min=15)


def test_on_time_without_t15_card_still_goes_out():
    s = _store()
    ev = _ev("x", "BOJ Policy Rate", 3)
    assert not m._time_filled_after_print(s, ev, NOW, pre_enabled=True, late_cap_min=15)


def test_off_when_pre_release_disabled_or_cap_zero():
    s = _store()
    ev = _ev("boj", "BOJ Policy Rate", 50)
    assert not m._time_filled_after_print(s, ev, NOW, pre_enabled=False, late_cap_min=15)
    assert not m._time_filled_after_print(s, ev, NOW, pre_enabled=True, late_cap_min=0)


def _run_check(monkeypatch, s, events):
    pushed = []

    class _Line:
        def push_flex(self, target, alt, bubble):
            pushed.append(alt)
            return {"status": 200}

    src_cfg, kw_cfg, sched_cfg = m._load_configs()
    monkeypatch.setattr(m, "_load_configs", lambda: (src_cfg, kw_cfg, sched_cfg))
    monkeypatch.setattr(m, "is_weekend_ict", lambda *a, **k: False)
    monkeypatch.setattr(m, "is_quiet_hours_ict", lambda *a, **k: False)
    monkeypatch.setattr(m.Store, "from_env", classmethod(lambda cls: s))
    monkeypatch.setattr(s, "connect", lambda: None)
    monkeypatch.setattr(s, "load_all", lambda: None)
    monkeypatch.setattr(s, "flush", lambda *a, **k: None)
    monkeypatch.setattr(m, "now_utc", lambda: NOW)
    monkeypatch.setattr(cal, "now_utc", lambda: NOW)
    monkeypatch.setattr(cal, "fetch_calendar", lambda *a, **k: events)
    for name in ("_record_pinger_spot_base", "_record_spot_tape", "_speech_plan",
                 "_dispatch_release_pingers", "record_line_outcome"):
        monkeypatch.setattr(m, name, lambda *a, **k: None)
    monkeypatch.setattr(m.macro_push, "push_next_event_window", lambda *a, **k: None)
    monkeypatch.setattr(m.LineClient, "from_env", classmethod(lambda cls: _Line()))
    monkeypatch.setattr(m, "_group_targets", lambda: "C1")
    monkeypatch.setattr(m.telegram_news.TelegramNewsClient, "from_env", classmethod(lambda cls, **k: None))
    monkeypatch.setattr(m.fred, "fetch_actual", lambda *a, **k: None)
    from src import ff_scraper, spot_feed
    monkeypatch.setattr(ff_scraper, "scrape_current_week_actuals", lambda: {"x": "0.75%"})
    monkeypatch.setattr(ff_scraper, "lookup_actual_for_event", lambda ev, cache: "0.75%")
    monkeypatch.setattr(spot_feed, "move_since", lambda *a, **k: None)
    monkeypatch.setattr(m.news_alert, "explain_calendar_release", lambda **k: None)
    assert asyncio.run(m.run_calendar_check()) == 0
    return pushed


def test_run_calendar_check_skips_the_late_boj_pair_but_sends_the_rest(monkeypatch):
    s = _store()
    boj_rate = _ev("boj_rate", "BOJ Policy Rate", 50)
    boj_outlook = _ev("boj_outlook", "BOJ Outlook Report", 50, forecast="")
    aud = _ev("aud", "Tokyo Core CPI y/y", 80, forecast="1.9%")   # had its T-15 card
    _pre(s, "aud")
    pushed = _run_check(monkeypatch, s, [boj_rate, boj_outlook, aud])

    assert len(pushed) == 1                                   # only the CPI card
    assert s.get("sent_log", ("postcal:aud", "calendar_post")) is not None
    assert s.get("sent_log", ("postcal:boj_rate", "calendar_post")) is None
    assert s.get("sent_log", ("postcal:boj_outlook", "calendar_post")) is None
