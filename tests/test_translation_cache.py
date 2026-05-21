"""Translation cache — SHA-keyed reuse across cron iterations.

Tests use a FakeStore (in-memory) so they don't hit Sheets or Claude/Google.
The cache layer is tested independently of the translation backends:
when a hit exists, to_thai() returns it; when a miss, it falls through to
the backends (mocked here to None so we can test the write path)."""
from __future__ import annotations

from unittest.mock import patch

from src.translator import _cache_key, _cache_lookup, _cache_write, to_thai


def test_cache_key_stable():
    """Same input → same key. Different input → different key."""
    a = _cache_key("Trump signs new tariff")
    b = _cache_key("Trump signs new tariff")
    c = _cache_key("Trump signs a new tariff")
    assert a == b
    assert a != c
    assert len(a) == 16


def test_cache_lookup_miss_returns_none(store):
    """Empty cache → None."""
    assert _cache_lookup(store, "deadbeef00000000") is None


def test_cache_lookup_hit_returns_thai(store):
    """Pre-populated row → returned."""
    store.upsert("translation_cache", {
        "cache_key":      "abc1234567890def",
        "source_preview": "Trump signs new tariff",
        "thai_text":      "ทรัมป์เซ็นภาษีนำเข้าใหม่",
        "hits":           "1",
        "created_at":     "2026-05-22T00:00:00+00:00",
    })
    assert _cache_lookup(store, "abc1234567890def") == "ทรัมป์เซ็นภาษีนำเข้าใหม่"


def test_cache_write_increments_hits(store):
    """Repeated writes for same key bump the hits counter."""
    _cache_write(store, "key1234567890abc", "Trump tariff", "ทรัมป์ภาษีนำเข้า")
    _cache_write(store, "key1234567890abc", "Trump tariff", "ทรัมป์ภาษีนำเข้า")
    _cache_write(store, "key1234567890abc", "Trump tariff", "ทรัมป์ภาษีนำเข้า")
    row = store.get("translation_cache", ("key1234567890abc",))
    assert row["hits"] == "3"


def test_cache_write_preserves_created_at(store):
    """Re-write keeps original created_at; only updated_at moves."""
    _cache_write(store, "key1234567890abc", "Trump tariff", "ทรัมป์ภาษีนำเข้า")
    first = store.get("translation_cache", ("key1234567890abc",))
    first_created = first["created_at"]
    _cache_write(store, "key1234567890abc", "Trump tariff", "ทรัมป์ภาษีนำเข้า")
    second = store.get("translation_cache", ("key1234567890abc",))
    assert second["created_at"] == first_created


def test_to_thai_returns_cache_hit_without_calling_backends(store):
    """When cache has the entry, neither Claude nor Google is called.
    This is the whole point of the cache — saves Claude tokens."""
    key = _cache_key("Trump signs tariff")
    store.upsert("translation_cache", {
        "cache_key":      key,
        "source_preview": "Trump signs tariff",
        "thai_text":      "ทรัมป์เซ็นภาษีนำเข้า",
        "hits":           "1",
        "created_at":     "2026-05-22T00:00:00+00:00",
    })
    with patch("src.translator._translate_claude") as m_claude, \
         patch("src.translator._translate_google") as m_google:
        out = to_thai("Trump signs tariff", store=store)
        assert out == "ทรัมป์เซ็นภาษีนำเข้า"
        m_claude.assert_not_called()
        m_google.assert_not_called()


def test_to_thai_writes_cache_on_miss(store):
    """First call: backend hit → write to cache. Second call: cache hit."""
    with patch("src.translator._translate_claude", return_value="ทรัมป์เซ็นภาษีนำเข้า") as m_claude, \
         patch("src.translator._translate_google") as m_google:
        out1 = to_thai("Trump signs tariff", store=store)
        assert out1 == "ทรัมป์เซ็นภาษีนำเข้า"
        assert m_claude.call_count == 1

        # Second call: should be a cache hit, no new Claude call
        out2 = to_thai("Trump signs tariff", store=store)
        assert out2 == "ทรัมป์เซ็นภาษีนำเข้า"
        assert m_claude.call_count == 1   # still 1, no new backend call


def test_to_thai_works_without_store(store):
    """store=None disables cache but translation still works."""
    with patch("src.translator._translate_claude", return_value="ทรัมป์"):
        out = to_thai("Trump", store=None)
        assert out == "ทรัมป์"


def test_to_thai_does_not_cache_failed_translation(store):
    """If both backends return None, nothing is written to cache (so the
    next call retries instead of forever returning the same None)."""
    with patch("src.translator._translate_claude", return_value=None), \
         patch("src.translator._translate_google", return_value=None):
        out = to_thai("Some text", store=store)
        assert out is None
        # cache should be empty
        assert _cache_lookup(store, _cache_key("Some text")) is None
