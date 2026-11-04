"""2026-11-04: weekdays with no High/Medium release still get a morning card
(prices + "nothing scheduled" + bank holidays). Before, calendar_daily
returned without pushing (e.g. Mon 2026-10-26: "4 today, 0 after filter")."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from src import calendar as cal
from src import main as m
from src import store as store_mod
from src.line_flex import calendar_quiet_day_bubble

# Mon 2026-10-26 04:41 ICT
NOW_UTC = datetime(2026, 10, 25, 21, 41, tzinfo=timezone.utc)


def _ev(title, impact, country="USD", hour_utc=13):
    dt = datetime(2026, 10, 26, hour_utc, 30, tzinfo=timezone.utc)
    return cal.CalEvent(event_id=cal._make_event_id(title, dt.isoformat(), country), title=title,
                        country=country, impact=impact, forecast="", previous="", dt_utc=dt)


def _texts(bubble):
    return [c.get("text", "") for c in bubble["body"]["contents"]]


def test_quiet_bubble_has_note_prices_and_holidays():
    b = calendar_quiet_day_bubble("Mon 26 Oct 2026", [_ev("Bank Holiday", "Holiday", country="CNY")],
                                  xau_snapshot=(4100.0, 0.4), dxy_snapshot=(99.1, -0.1))
    texts = _texts(b)
    assert "วันนี้ไม่มีตัวเลขเศรษฐกิจระดับ High / Medium" in texts
    assert "🏦 CNY Bank Holiday" in texts
    assert b["body"]["contents"][0]["layout"] == "horizontal"     # price strip first
    assert "—" not in json.dumps(b, ensure_ascii=False)          # no em-dash in group copy


def test_quiet_bubble_without_quotes_or_holidays():
    b = calendar_quiet_day_bubble("Mon 26 Oct 2026")
    assert _texts(b) == ["วันนี้ไม่มีตัวเลขเศรษฐกิจระดับ High / Medium",
                         "ข่าวด่วนระหว่างวันยังแจ้งตามปกติ"]


def _store():
    s = store_mod.Store(sheet_id="x", creds_json="{}")
    for tab in store_mod.SCHEMAS:
        s.data[tab] = {}
        s.dirty[tab] = set()
    return s


def _run_daily(monkeypatch, s, events):
    pushed, tg = [], []

    class _Line:
        def push_flex(self, target, alt, bubble):
            pushed.append((alt, bubble))
            return {"status": 200}

    class _TG:
        def post_calendar(self, payload):
            tg.append(payload)

    monkeypatch.setattr(m, "is_weekend_ict", lambda *a, **k: False)
    monkeypatch.setattr(m, "is_quiet_hours_ict", lambda *a, **k: False)
    monkeypatch.setattr(m.Store, "from_env", classmethod(lambda cls: s))
    monkeypatch.setattr(s, "connect", lambda: None)
    monkeypatch.setattr(s, "load_all", lambda: None)
    monkeypatch.setattr(s, "flush", lambda *a, **k: None)
    monkeypatch.setattr(m, "now_utc", lambda: NOW_UTC)
    monkeypatch.setattr(m, "now_ict", lambda: NOW_UTC.astimezone(cal.ICT))
    monkeypatch.setattr(cal, "now_utc", lambda: NOW_UTC)
    monkeypatch.setattr(cal, "fetch_calendar", lambda *a, **k: events)
    monkeypatch.setattr(m, "record_line_outcome", lambda *a, **k: None)
    monkeypatch.setattr(m.LineClient, "from_env", classmethod(lambda cls: _Line()))
    monkeypatch.setattr(m, "_group_targets", lambda: "C1")
    monkeypatch.setattr(m.telegram_news.TelegramNewsClient, "from_env", classmethod(lambda cls, **k: _TG()))
    for name in ("get_xau_snapshot", "get_dxy_snapshot", "get_hui_snapshot",
                 "get_gld_snapshot", "get_thb_snapshot"):
        monkeypatch.setattr(m.price_feed, name, lambda: None)
    assert asyncio.run(m.run_calendar_daily()) == 0
    return pushed, tg


def test_quiet_weekday_sends_the_quiet_card(monkeypatch):
    s = _store()
    events = [_ev("Some Low Thing", "Low"), _ev("Bank Holiday", "Holiday", country="JPY", hour_utc=0)]
    pushed, tg = _run_daily(monkeypatch, s, events)
    assert len(pushed) == 1
    alt, bubble = pushed[0]
    assert "ไม่มีตัวเลขสำคัญ" in alt
    assert "🏦 JPY Bank Holiday" in _texts(bubble)
    assert s.get("sent_log", ("cal_daily:2026-10-26:main", "calendar_daily")) is not None
    assert tg == []                     # Telegram's calendar card needs events


def test_event_day_still_sends_the_full_card(monkeypatch):
    s = _store()
    pushed, tg = _run_daily(monkeypatch, s, [_ev("CB Consumer Confidence", "High")])
    assert len(pushed) == 1 and "1 events today" in pushed[0][0]
    assert len(tg) == 1
