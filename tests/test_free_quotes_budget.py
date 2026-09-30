"""2026-09-30: FinancialJuice RSS (free squawk feed), Apify hot-window budget,
per-source User-Agent."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import yaml

from src import apify_budget as ab
from src import fetcher, speech_watch as sw
from src import store as store_mod
from src.calendar import CalEvent
from src.parser import parse_feed

T = datetime(2026, 10, 2, 12, 30, tzinfo=timezone.utc)

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>FJ</title>
<item><title>FinancialJuice: Fed's Waller: more hikes may be needed</title>
<link>https://www.financialjuice.com/News/1</link><pubDate>Fri, 02 Oct 2026 12:31:00 GMT</pubDate></item>
<item><title>No prefix here</title><link>https://x/2</link><pubDate>Fri, 02 Oct 2026 12:32:00 GMT</pubDate></item>
</channel></rss>"""


def _fj_source():
    return [s for s in yaml.safe_load(open("config/sources.yaml", encoding="utf-8"))["sources"]
            if s["id"] == "financialjuice"][0]


def _store():
    s = store_mod.Store(sheet_id="x", creds_json="{}")
    for tab in store_mod.SCHEMAS:
        s.data[tab] = {}
        s.dirty[tab] = set()
    return s


# ---- FinancialJuice RSS ----

def test_financialjuice_source_config():
    src = _fj_source()
    assert src["enabled"] and src["organization"] == "x_financialjuice"   # same outlet as X
    assert src["user_agent"].startswith("Mozilla/5.0")


def test_title_prefix_stripped():
    es = parse_feed(RSS, _fj_source())
    assert [e["title"] for e in es] == ["Fed's Waller: more hikes may be needed", "No prefix here"]
    assert es[0]["organization"] == "x_financialjuice"


def test_financialjuice_lines_count_as_live_quotes():
    assert sw.is_quote("Fed's Waller: more hikes may be needed", "financialjuice", ["WALLER"])
    assert not sw.is_quote("Oil rises 2%", "financialjuice", ["WALLER"])


def test_fetcher_uses_per_source_user_agent():
    seen = {}

    def handler(request):
        seen["ua"] = request.headers.get("User-Agent")
        return httpx.Response(200, content=RSS)

    async def go(src):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await fetcher._fetch_one(c, src, {})
    asyncio.run(go(_fj_source()))
    assert seen["ua"].startswith("Mozilla/5.0")
    asyncio.run(go({"id": "plain", "url": "https://x/feed"}))
    assert seen["ua"] == "gold-news-pipeline/1.0"


# ---- hot windows ----

def test_hot_windows_from_high_usd_and_speeches():
    evs = [CalEvent("a", "Non-Farm Employment Change", "USD", "High", "", "", T),
           CalEvent("b", "CB Consumer Confidence", "USD", "Medium", "", "", T),
           CalEvent("c", "CPI y/y", "EUR", "High", "", "", T)]
    speech = [{"start": (T + timedelta(hours=2)).isoformat(), "end": (T + timedelta(hours=3)).isoformat()}]
    wins = ab.hot_windows_from_events(evs, speech)
    assert wins[0] == (T - timedelta(minutes=10), T + timedelta(minutes=45))
    assert len(wins) == 2                                      # Medium + non-USD excluded


def test_in_hot_window_roundtrip(monkeypatch):
    s = _store()
    monkeypatch.setattr(ab, "now_utc", lambda: T)
    ab.publish_hot_windows(s, [(T - timedelta(minutes=10), T + timedelta(minutes=45))])
    assert ab.in_hot_window(s, T + timedelta(minutes=5))
    assert not ab.in_hot_window(s, T + timedelta(hours=2))


def test_hot_reserve_blocks_only_outside_hot_windows():
    s = _store()
    s.upsert("source_state", {"source_id": ab.STATE_KEY, "last_status": ab.OK,
                              "items_last_hour": json.dumps({"usage": 23.0, "limit": 29.0})})
    cfg = {"enabled": True, "hot_reserve_pct": 25}
    assert ab.hot_reserve_blocks(s, cfg, hot=False) is True    # 23 ≥ 29×0.75
    assert ab.hot_reserve_blocks(s, cfg, hot=True) is False
    s.upsert("source_state", {"source_id": ab.STATE_KEY, "last_status": ab.OK,
                              "items_last_hour": json.dumps({"usage": 10.0, "limit": 29.0})})
    assert ab.hot_reserve_blocks(s, cfg, hot=False) is False
    assert ab.hot_reserve_blocks(s, {}, hot=False) is False    # disabled
