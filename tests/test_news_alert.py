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
    assert a.startswith("a3")   # versioned prefix (bumped to invalidate old cache)
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


def test_high_quality_bypasses_cache_and_forwards_flag(store):
    """BREAKING (high_quality) ignores a stale cached classification and gets a
    fresh (stronger-model) result, and the flag reaches the model selector."""
    key = _cache_key_alert("US CPI hot", "CPI prints 3.5%")
    old = MarketAlert(action="keep", tone="hawkish", category="Inflation",
                      headline_th="OLD cached", body_th=["old"])
    store.upsert("translation_cache", {
        "cache_key": key, "source_preview": "US CPI hot",
        "thai_text": old.to_json(), "hits": "1",
        "created_at": "2026-05-22T00:00:00+00:00",
    })
    fresh = MarketAlert(action="keep", tone="hawkish", category="Inflation",
                        headline_th="FRESH sonnet", body_th=["new"])
    with patch("src.news_alert._classify_claude_with_usage",
                return_value=(fresh, 100, 50)) as m:
        out = classify_and_rewrite("US CPI hot", "CPI prints 3.5%",
                                   store=store, high_quality=True)
        assert out.headline_th == "FRESH sonnet"   # not the cached "OLD cached"
        m.assert_called_once()
        assert m.call_args.kwargs.get("high_quality") is True


def test_high_quality_tokens_tracked_separately(store):
    """Sonnet/breaking tokens land in BOTH the aggregate and the hq bucket so
    the Sonnet share of monthly cost is measurable."""
    fresh = MarketAlert(action="keep", tone="hawkish", category="Inflation",
                        headline_th="x", body_th=["y"])
    with patch("src.news_alert._classify_claude_with_usage", return_value=(fresh, 1500, 900)):
        classify_and_rewrite("US CPI hot", "body", store=store, high_quality=True)
    blob = json.loads(store.get("source_state", ("_classifier_health",))["items_last_hour"])
    assert blob["month_tokens_in"] == 1500 and blob["month_tokens_out"] == 900
    assert blob["month_hq_tokens_in"] == 1500 and blob["month_hq_tokens_out"] == 900


def test_normal_quality_not_in_hq_bucket(store):
    fresh = MarketAlert(action="keep", tone="hawkish", category="Inflation",
                        headline_th="x", body_th=["y"])
    with patch("src.news_alert._classify_claude_with_usage", return_value=(fresh, 1000, 500)):
        classify_and_rewrite("Other news", "body", store=store, high_quality=False)
    blob = json.loads(store.get("source_state", ("_classifier_health",))["items_last_hour"])
    assert blob["month_tokens_in"] == 1000
    assert blob.get("month_hq_tokens_in", 0) == 0   # Haiku stays out of the hq bucket


def test_classify_and_rewrite_writes_to_cache(store):
    """First call hits Claude (mocked), writes to cache. Second call
    is a cache hit — Claude not re-called."""
    fake_alert = MarketAlert(
        action="keep", news_type="data_release", tone="hawkish",
        category="Inflation", headline_th="CPI สหรัฐสูงกว่าคาด",
        body_th=["CPI 3.5% vs 3.3% คาด"], impact_th="กดดันทองคำ",
    )
    with patch("src.news_alert._classify_claude_with_usage",
                return_value=(fake_alert, 100, 50)) as m:
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
    with patch("src.news_alert._classify_claude_with_usage",
                return_value=(rejected, 50, 20)):
        out = classify_and_rewrite(
            "5 ways to protect your savings from inflation",
            "Investment tips for retirees", store=store,
        )
        assert out.should_send is False
        assert "personal-finance" in out.reason


def test_classifier_counters_track_kept_rejected(store):
    """Each classify call bumps per-source + global counters. Used by
    watchdog + EOD recap to spot silently degraded classifier."""
    from src.news_alert import get_classifier_counters
    kept = MarketAlert(action="keep", headline_th="x")
    rejected = MarketAlert(action="reject", reason="evergreen")
    with patch("src.news_alert._classify_claude_with_usage",
                side_effect=[(kept, 80, 40), (rejected, 70, 30), (kept, 90, 50)]):
        classify_and_rewrite("title1", "sum1", source_id="forexlive", store=store)
        classify_and_rewrite("title2", "sum2", source_id="forexlive", store=store)
        classify_and_rewrite("title3", "sum3", source_id="bbc_world", store=store)

    global_cnt = get_classifier_counters(store, source_id=None)
    assert global_cnt.get("kept") == 2
    assert global_cnt.get("rejected") == 1

    fl = get_classifier_counters(store, source_id="forexlive")
    assert fl.get("kept") == 1
    assert fl.get("rejected") == 1


def test_classifier_counters_track_fallback(store):
    """Fallback (Claude unavailable) increments the fallback counter so
    the watchdog can detect silent degradation."""
    from src.news_alert import get_classifier_counters
    with patch("src.news_alert._classify_claude_with_usage",
                return_value=(None, 0, 0)), \
         patch("src.news_alert._classify_gemini_with_usage",
                return_value=(None, 0, 0)), \
         patch("src.translator._translate_claude", return_value=None), \
         patch("src.translator._translate_google", return_value="ทอง"):
        classify_and_rewrite("Gold up", "Gold rallies", source_id="forexlive", store=store)

    cnt = get_classifier_counters(store, source_id=None)
    assert cnt.get("fallback") == 1
    # Fallback is counted as "kept" (permissive accept) so total still adds up
    assert cnt.get("kept") == 1


def test_cache_cap_evicts_oldest_when_full(store):
    """When the cache reaches the hard cap, adding a new entry evicts
    the row with the oldest updated_at (LRU-by-time)."""
    from src.news_alert import _CACHE_HARD_CAP, _cache_write
    # Seed cache to exactly the cap with old timestamps
    for i in range(_CACHE_HARD_CAP):
        store.upsert("translation_cache", {
            "cache_key":     f"old{i:04d}xxxxxxxx",
            "source_preview": f"old item {i}",
            "thai_text":     '{"action":"keep","headline_th":"x"}',
            "hits":          "1",
            "created_at":    f"2026-05-01T00:00:00+00:00",
        })
        # Override updated_at to force a known ordering — oldest = highest i? No, lowest i.
        store.data["translation_cache"][f"old{i:04d}xxxxxxxx"]["updated_at"] = f"2026-05-01T{i % 24:02d}:00:00+00:00"

    assert len(store.data["translation_cache"]) == _CACHE_HARD_CAP

    # Write a new key — should evict the oldest entry.
    new_alert = MarketAlert(action="keep", headline_th="new")
    _cache_write(store, "newkey1234567890", "new title", new_alert)

    # Still at cap (one evicted, one added)
    assert len(store.data["translation_cache"]) == _CACHE_HARD_CAP
    # The new key is present
    assert store.get("translation_cache", ("newkey1234567890",)) is not None


def test_gemini_used_when_claude_unavailable(store):
    """When Claude returns None (e.g. Anthropic spend cap), the secondary
    Gemini classifier is tried BEFORE the literal-translation fallback.
    A Gemini `keep` is a real classify — it should be cached, not treated
    as a fallback."""
    gemini_alert = MarketAlert(
        action="keep", news_type="central_bank", tone="dovish",
        category="Central Bank", headline_th="Fed ส่งสัญญาณผ่อนคลาย",
        body_th=["ถ้อยแถลงโทนนุ่มกว่าคาด"], impact_th="หนุนราคาทองคำ",
    )
    with patch("src.news_alert._classify_claude_with_usage",
               return_value=(None, 0, 0)), \
         patch("src.news_alert._classify_gemini_with_usage",
               return_value=(gemini_alert, 120, 60)) as m_gem:
        out1 = classify_and_rewrite("Fed dovish tilt", "Powell softer", store=store)
        assert out1.should_send is True
        assert out1.headline_th == "Fed ส่งสัญญาณผ่อนคลาย"
        assert out1.category == "Central Bank"   # NOT the "Other" fallback
        assert m_gem.call_count == 1

        # Gemini result is cached like Claude's — 2nd call is a cache hit.
        out2 = classify_and_rewrite("Fed dovish tilt", "Powell softer", store=store)
        assert out2.should_send is True
        assert m_gem.call_count == 1


def test_classify_gemini_parses_rest_response(monkeypatch):
    """_classify_gemini_with_usage parses the Gemini REST shape
    (candidates[].content.parts[].text + usageMetadata) into a validated
    MarketAlert, reusing the same JSON contract as Claude."""
    import src.news_alert as na

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    payload = {
        "candidates": [{
            "content": {"parts": [{"text": json.dumps({
                "action": "keep", "news_type": "data_release",
                "relevance_to_gold": "high", "tone": "hawkish",
                "category": "Inflation", "headline_th": "CPI สหรัฐสูงกว่าคาด",
                "body_th": ["CPI 3.5% vs 3.3% คาด"], "impact_th": "กดดันทองคำ",
            }, ensure_ascii=False)}]},
        }],
        "usageMetadata": {"promptTokenCount": 200, "candidatesTokenCount": 80},
    }

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return payload

    class _Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, *a, **k): return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "Client", _Client)

    alert, tin, tout = na._classify_gemini_with_usage(
        "US CPI hot", "CPI prints 3.5%", "forexlive", "0.2")
    assert alert is not None
    assert alert.should_send is True
    assert alert.headline_th == "CPI สหรัฐสูงกว่าคาด"
    assert alert.category == "Inflation"
    assert (tin, tout) == (200, 80)


def test_classify_gemini_no_key_returns_none(monkeypatch):
    """Without GEMINI_API_KEY, the secondary classifier no-ops cleanly
    (so the chain falls through to the literal-translation fallback)."""
    import src.news_alert as na
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    alert, tin, tout = na._classify_gemini_with_usage("x", "y", "s", "1.0")
    assert alert is None
    assert (tin, tout) == (0, 0)


def test_classify_and_rewrite_fallback_when_claude_unavailable(store):
    """When Claude returns None (key missing / API down), we fall through
    to a permissive accept with literal translation, so the pipeline still
    publishes during a Claude outage rather than going silent."""
    with patch("src.news_alert._classify_claude", return_value=None), \
         patch("src.news_alert._classify_claude_with_usage", return_value=(None, 0, 0)), \
         patch("src.news_alert._classify_gemini_with_usage", return_value=(None, 0, 0)), \
         patch("src.translator._translate_claude", return_value=None), \
         patch("src.translator._translate_google", return_value="ราคาทองพุ่ง"):
        out = classify_and_rewrite("Gold surges", "Gold rallies on safe-haven bid",
                                    store=store)
        assert out.should_send is True
        assert out.headline_th == "ราคาทองพุ่ง"
        assert "fallback" in out.reason
