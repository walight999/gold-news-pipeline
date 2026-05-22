"""Pre-filter + structured Thai market alert rewrite tests.

We test the pure logic (JSON parsing, cache, fallback) without hitting
Claude. The full Claude path is exercised by the standalone live smoke
test tests/smoke_news_alert.py."""
from __future__ import annotations

import json
from unittest.mock import patch

from src.news_alert import (
    MarketAlert,
    _cache_key_alert,
    classify_and_rewrite,
)


def test_market_alert_should_send_only_when_keep():
    """Only `keep` produces a push. Everything else (reject, empty) is
    silently dropped by the caller."""
    assert MarketAlert(action="keep", headline_th="x").should_send is True
    assert MarketAlert(action="reject").should_send is False
    assert MarketAlert().should_send is False   # default = reject


def test_market_alert_roundtrip_json():
    """to_json + from_json is lossless across every field."""
    original = MarketAlert(
        action="keep",
        news_type="data_release",
        relevance_to_gold="high",
        freshness="fresh",
        tone="hawkish",
        category="Inflation",
        headline_th="CPI สหรัฐสูงกว่าคาด",
        body_th=["bullet 1", "bullet 2"],
        impact_th="กดดันราคาทองคำ",
        reason="",
    )
    rebuilt = MarketAlert.from_json(original.to_json())
    assert rebuilt is not None
    assert rebuilt.action == "keep"
    assert rebuilt.headline_th == "CPI สหรัฐสูงกว่าคาด"
    assert rebuilt.body_th == ["bullet 1", "bullet 2"]
    assert rebuilt.tone == "hawkish"


def test_market_alert_from_json_rejects_garbage():
    assert MarketAlert.from_json("not json") is None
    assert MarketAlert.from_json("{}") is None   # missing 'action'
    assert MarketAlert.from_json("null") is None


def test_cache_key_distinct_per_title():
    """Same title+summary → same key. Different → different key."""
    a = _cache_key_alert("Fed signals rate cut", "Powell speech")
    b = _cache_key_alert("Fed signals rate cut", "Powell speech")
    c = _cache_key_alert("Fed signals rate hike", "Powell speech")
    assert a == b
    assert a != c
    assert a.startswith("al")
    assert len(a) == 16


def test_classify_and_rewrite_rejects_empty_title():
    out = classify_and_rewrite("", "some summary")
    assert out.should_send is False
    assert out.reason == "empty title"


def test_classify_and_rewrite_uses_cache_when_present(store):
    """When a cached MarketAlert exists, Claude is NOT called."""
    key = _cache_key_alert("Fed cuts 25bps", "FOMC decision")
    cached = MarketAlert(
        action="keep", news_type="central_bank", tone="dovish",
        category="Central Bank", headline_th="Fed ลดดอกเบี้ย 25bps",
        body_th=["FOMC ลด policy rate 25bps", "ตลาดคาดการณ์ตรงจุด"],
        impact_th="หนุนราคาทองคำ",
    )
    store.upsert("translation_cache", {
        "cache_key": key,
        "source_preview": "Fed cuts 25bps",
        "thai_text": cached.to_json(),
        "hits": "1",
        "created_at": "2026-05-22T00:00:00+00:00",
    })
    with patch("src.news_alert._classify_claude") as m_claude:
        out = classify_and_rewrite("Fed cuts 25bps", "FOMC decision", store=store)
        assert out.should_send is True
        assert out.headline_th == "Fed ลดดอกเบี้ย 25bps"
        m_claude.assert_not_called()


def test_classify_and_rewrite_writes_to_cache(store):
    """First call hits Claude (mocked), writes to cache. Second call
    is a cache hit — Claude not re-called."""
    fake_alert = MarketAlert(
        action="keep", news_type="data_release", tone="hawkish",
        category="Inflation", headline_th="CPI สหรัฐสูงกว่าคาด",
        body_th=["CPI 3.5% vs 3.3% คาด"], impact_th="กดดันทองคำ",
    )
    with patch("src.news_alert._classify_claude", return_value=fake_alert) as m:
        out1 = classify_and_rewrite("US CPI hot", "CPI prints 3.5%", store=store)
        assert out1.should_send is True
        assert m.call_count == 1

        out2 = classify_and_rewrite("US CPI hot", "CPI prints 3.5%", store=store)
        assert out2.should_send is True
        assert m.call_count == 1   # still 1 — cache hit on 2nd call


def test_classify_and_rewrite_reject_skips_send(store):
    """Classifier reject → should_send False, no headline. Caller must
    not push these to LINE."""
    rejected = MarketAlert(action="reject", reason="personal-finance article")
    with patch("src.news_alert._classify_claude", return_value=rejected):
        out = classify_and_rewrite(
            "5 ways to protect your savings from inflation",
            "Investment tips for retirees", store=store,
        )
        assert out.should_send is False
        assert "personal-finance" in out.reason


def test_classify_and_rewrite_fallback_when_claude_unavailable(store):
    """When Claude returns None (key missing / API down), we fall through
    to a permissive accept with literal translation, so the pipeline still
    publishes during a Claude outage rather than going silent."""
    with patch("src.news_alert._classify_claude", return_value=None), \
         patch("src.translator._translate_claude", return_value=None), \
         patch("src.translator._translate_google", return_value="ราคาทองพุ่ง"):
        out = classify_and_rewrite("Gold surges", "Gold rallies on safe-haven bid",
                                    store=store)
        assert out.should_send is True
        assert out.headline_th == "ราคาทองพุ่ง"
        assert "fallback" in out.reason
