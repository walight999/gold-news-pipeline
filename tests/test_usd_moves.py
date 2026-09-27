"""2026-09-28: moves shown in $ on SPOT (spot tape), full-quota delivery,
typical $ range on the history line, daily-card ledger flushed right away."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from src import release_stats, spot_feed
from src import store as store_mod
from src.calendar import CalEvent
from src.line_flex import _price_cell, post_release_bubble, speech_bubble

T = datetime(2026, 9, 30, 12, 30, tzinfo=timezone.utc)


def _store():
    s = store_mod.Store(sheet_id="x", creds_json="{}")
    for tab in store_mod.SCHEMAS:
        s.data[tab] = {}
        s.dirty[tab] = set()
    return s


def _texts(node):
    out = []
    if isinstance(node, dict):
        if node.get("type") == "text":
            out.append(node["text"])
        for v in node.values():
            out += _texts(v)
    elif isinstance(node, list):
        for v in node:
            out += _texts(v)
    return out


# ---- spot tape ----

def test_tape_records_prunes_and_finds_base():
    s = _store()
    spot_feed.record_tape(s, T - timedelta(hours=30), price=4100.0)     # will be pruned
    spot_feed.record_tape(s, T - timedelta(minutes=4), price=4280.0)
    spot_feed.record_tape(s, T + timedelta(minutes=1), price=4290.0)
    tape = spot_feed.load_tape(s)
    assert [p for _, p in tape] == [4280.0, 4290.0]
    assert spot_feed.tape_price_before(s, T) == (4280.0, 4)
    mv = spot_feed.move_since(s, T, T + timedelta(minutes=7), 4267.6)
    assert mv["usd"] == pytest.approx(-12.4) and mv["minutes"] == 7


def test_no_base_when_tape_point_too_old():
    s = _store()
    spot_feed.record_tape(s, T - timedelta(minutes=20), price=4280.0)
    assert spot_feed.tape_price_before(s, T) is None
    assert spot_feed.move_since(s, T, T + timedelta(minutes=5), 4290.0) is None


def test_record_tape_without_spot_is_noop():
    s = _store()
    assert spot_feed.record_tape(s, T) is None           # conftest: no network
    assert spot_feed.load_tape(s) == []


# ---- $ on the cards ----

def test_released_card_shows_spot_usd_move():
    ev = CalEvent("x", "Core PCE Price Index m/m", "USD", "High", "0.2%", "0.3%", T)
    from src.calendar import gold_impact_directional
    b = post_release_bubble(ev, gold_impact_directional(ev), actual_text="0.4%", surprise="beat",
                            verdict="🔴 Bearish gold",
                            xau_move={"usd": -12.4, "pct": -0.29, "minutes": 7, "base": 4280.0})
    assert "ทองขยับ -$12.4 ใน 7 นาทีหลังข่าว (spot)" in _texts(b)
    assert not any("%" in t and "ขยับ" in t for t in _texts(b))


def test_speech_card_shows_spot_usd_move():
    a = {"tone": "hawkish", "summary_th": "สรุป", "key_points_th": [], "gold_bias": "bearish",
         "gold_confidence": "low", "why_th": ""}
    b = speech_bubble({"title": "FOMC Press Conference", "country": "USD"}, a, 4,
                      {"usd": 21.0, "pct": 0.49, "minutes": 45, "base": 4280.0}, "end")
    assert "ทองขยับ +$21.0 ตั้งแต่เริ่มแถลง (45 นาที, spot)" in _texts(b)


def test_daily_card_xau_change_in_usd_others_in_pct():
    xau = _price_cell("XAU", (4285.2, 0.42), lambda v: f"{v:,.2f}", change_usd=True)
    dxy = _price_cell("DXY", (98.1, -0.2), lambda v: f"{v:,.2f}")
    assert _texts(xau)[-1] == "+$17.9"
    assert _texts(dxy)[-1] == "-0.20%"


# ---- typical $ range ----

def test_typical_range_scales_with_spot(tmp_path, monkeypatch):
    data = {"generated_from": ["2019-01-03T00:00:00Z", "2026-09-25T00:00:00Z"],
            "series": {"USD|Non-Farm Employment Change": {"cells": {
                "XAU|5": {"n": 21, "hit": 0.857}, "XAU|15": {"n": 26, "hit": 0.731,
                                                            "abs_p50_pct": 0.15, "abs_p80_pct": 0.35}}}}}
    p = tmp_path / "rs.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(release_stats, "STATS_PATH", str(p))
    release_stats._load.cache_clear()
    line = release_stats.history_line_th("USD", "Non-Farm Employment Change", spot=4000.0)
    assert line.endswith("· ปกติขยับ $6–$14 ใน 15 นาที")
    assert release_stats.typical_range_usd({"abs_p50_pct": 0.01, "abs_p80_pct": 0.02}, 4000.0) is None
    release_stats._load.cache_clear()


# ---- daily card ledger flushed right after the push ----

def test_calendar_daily_flushes_right_after_push(monkeypatch):
    import asyncio

    from src import main as m
    order = []

    class S:
        data = {}

        def connect(self): pass
        def load_all(self): pass
        def get(self, *a): return None
        def upsert(self, tab, row): order.append(("upsert", tab))
        def flush(self): order.append(("flush",))

    ev = CalEvent("x", "Core CPI m/m", "USD", "High", "0.3%", "0.2%",
                  datetime(2026, 9, 29, 12, 30, tzinfo=timezone.utc))
    monkeypatch.setattr(m.Store, "from_env", classmethod(lambda cls: S()))
    monkeypatch.setattr(m, "is_weekend_ict", lambda: False)
    monkeypatch.setattr(m, "now_ict", lambda: datetime(2026, 9, 29, 4, 40, tzinfo=m.ICT))
    monkeypatch.setattr(m.cal, "fetch_calendar", lambda *a, **k: [ev])
    monkeypatch.setattr(m.cal, "filter_today_ict", lambda evs, *a: evs)
    monkeypatch.setattr(m, "_group_targets", lambda: "C1")
    monkeypatch.setattr(m, "_push_or_skip", lambda *a, **k: {"status": 200})
    monkeypatch.setattr(m.price_feed, "get_snapshot", lambda t: None)
    monkeypatch.setattr(m.telegram_news.TelegramNewsClient, "from_env",
                        classmethod(lambda cls, **k: None))

    class L:
        pass
    monkeypatch.setattr(m.LineClient, "from_env", classmethod(lambda cls: L()))
    asyncio.run(m.run_calendar_daily())
    i = order.index(("upsert", "sent_log"))
    assert order[i + 1] == ("flush",)


def test_history_line_says_unclear_when_hit_below_55(tmp_path, monkeypatch):
    data = {"generated_from": ["2019-01-03T00:00:00Z", "2026-09-25T00:00:00Z"],
            "series": {"USD|Average Hourly Earnings m/m": {"cells": {
                "XAU|5": {"n": 19, "hit": 0.47}, "XAU|15": {"n": 19, "hit": 0.53}}}}}
    p = tmp_path / "rs.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(release_stats, "STATS_PATH", str(p))
    release_stats._load.cache_clear()
    line = release_stats.history_line_th("USD", "Average Hourly Earnings m/m", spot=4000.0)
    assert "ไม่ชัด" in line and "ไปตามทิศนี้" not in line
    release_stats._load.cache_clear()
