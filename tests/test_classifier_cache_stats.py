"""The classifier cache hit rate must be measurable.

Background: the `hits` column in translation_cache counts WRITES (_cache_write
increments it; _cache_lookup deliberately does not, to avoid dirtying a ~2-3 MB
tab every run). Every row in the live sheet therefore reads `hits: 1` and the
real hit rate was unknowable — which left the "is the 1-day TTL right?" question
with no data behind it. These in-process counters are the free replacement.
"""
from __future__ import annotations

import pytest

from src import news_alert
from src.news_alert import MarketAlert


@pytest.fixture(autouse=True)
def _clean_counters():
    news_alert.reset_cache_stats()
    yield
    news_alert.reset_cache_stats()


class _CacheStore:
    """Just enough Store for the classifier cache path."""

    def __init__(self):
        self.data = {"translation_cache": {}}
        self.dirty = {}

    def get(self, tab, key):
        return self.data.get(tab, {}).get(key[0])

    def upsert(self, tab, row):
        rows = self.data.setdefault(tab, {})
        rows[row.get("cache_key") or row.get("source_id")] = dict(row)

    def all_rows(self, tab):
        return list(self.data.get(tab, {}).values())


def _keep(headline="ทองขึ้น"):
    return MarketAlert(action="keep", category="Macro", headline_th=headline,
                       body_th="รายละเอียด", impact_th="บวกต่อทอง",
                       relevance_to_gold="high")


def _seed(store, title, summary, alert):
    news_alert._cache_write(store, news_alert._cache_key_alert(title, summary),
                            title, alert)


def _no_providers(monkeypatch):
    """Force both providers to fail so classify falls to the literal fallback —
    lets these tests run without network or API keys."""
    monkeypatch.setattr(news_alert, "_classify_claude_with_usage",
                        lambda *a, **k: (None, 0, 0))
    monkeypatch.setattr(news_alert, "_classify_gemini_with_usage",
                        lambda *a, **k: (None, 0, 0))
    monkeypatch.setattr(news_alert, "_fallback_alert",
                        lambda title, summary, store=None: _keep("fallback"))


def test_idle_run_reports_no_data_not_zero_percent():
    """A run that classified nothing must not look like a 0% hit rate."""
    stats = news_alert.run_cache_stats()

    assert stats["total"] == 0
    assert stats["hit_pct"] == -1


def test_cache_hit_is_counted(monkeypatch):
    _no_providers(monkeypatch)
    store = _CacheStore()
    _seed(store, "Fed holds rates", "", _keep())

    news_alert.classify_and_rewrite("Fed holds rates", "", store=store)
    stats = news_alert.run_cache_stats()

    assert (stats["hits"], stats["misses"], stats["hit_pct"]) == (1, 0, 100)


def test_cache_miss_is_counted(monkeypatch):
    _no_providers(monkeypatch)
    store = _CacheStore()

    news_alert.classify_and_rewrite("Never seen before", "", store=store)
    stats = news_alert.run_cache_stats()

    assert (stats["hits"], stats["misses"], stats["hit_pct"]) == (0, 1, 0)


def test_hit_rate_is_the_ratio_over_a_run(monkeypatch):
    _no_providers(monkeypatch)
    store = _CacheStore()
    for i in range(3):
        _seed(store, f"cached {i}", "", _keep())

    for i in range(3):
        news_alert.classify_and_rewrite(f"cached {i}", "", store=store)
    news_alert.classify_and_rewrite("fresh", "", store=store)

    stats = news_alert.run_cache_stats()
    assert (stats["hits"], stats["misses"], stats["total"]) == (3, 1, 4)
    assert stats["hit_pct"] == 75


def test_fallbacks_are_counted_separately(monkeypatch):
    """Fallback count is the 'both providers down' alarm — it must not be
    conflated with a cache miss, though every fallback is also a miss."""
    _no_providers(monkeypatch)
    store = _CacheStore()

    news_alert.classify_and_rewrite("fresh one", "", store=store)
    stats = news_alert.run_cache_stats()

    assert stats["fallbacks"] == 1
    assert stats["misses"] == 1


def test_high_quality_refresh_of_a_cached_keep_counts_as_a_miss(monkeypatch):
    """It re-hits the provider, so it costs a call — the counter measures calls
    saved, not lookups satisfied."""
    _no_providers(monkeypatch)
    store = _CacheStore()
    _seed(store, "Fed cuts 50bp", "", _keep())

    news_alert.classify_and_rewrite("Fed cuts 50bp", "", store=store,
                                    high_quality=True)
    stats = news_alert.run_cache_stats()

    assert (stats["hits"], stats["misses"]) == (0, 1)


def test_high_quality_still_honors_a_cached_reject(monkeypatch):
    """The sticky-breaking-item spend guard: a cached REJECT must stay a hit
    even for high_quality, or a noisy item re-runs on the stronger model every
    5 minutes forever."""
    _no_providers(monkeypatch)
    store = _CacheStore()
    _seed(store, "Celebrity buys gold chain", "",
          MarketAlert(action="reject", reason="not market-moving"))

    news_alert.classify_and_rewrite("Celebrity buys gold chain", "",
                                    store=store, high_quality=True)
    stats = news_alert.run_cache_stats()

    assert (stats["hits"], stats["misses"]) == (1, 0)


def test_no_store_means_every_call_is_a_miss(monkeypatch):
    """Without a store there is no cache — the log should say so honestly."""
    _no_providers(monkeypatch)

    news_alert.classify_and_rewrite("headline", "", store=None)
    stats = news_alert.run_cache_stats()

    assert (stats["hits"], stats["misses"]) == (0, 1)


def test_empty_title_short_circuits_without_touching_the_counters(monkeypatch):
    """The no-title reject never reaches the cache, so it must not skew the rate."""
    _no_providers(monkeypatch)

    news_alert.classify_and_rewrite("", "some summary", store=_CacheStore())
    stats = news_alert.run_cache_stats()

    assert stats["total"] == 0


def test_reset_zeroes_everything(monkeypatch):
    _no_providers(monkeypatch)
    news_alert.classify_and_rewrite("x", "", store=_CacheStore())

    news_alert.reset_cache_stats()

    assert news_alert.run_cache_stats() == {
        "hits": 0, "misses": 0, "fallbacks": 0, "total": 0, "hit_pct": -1}
