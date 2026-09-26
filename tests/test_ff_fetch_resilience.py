"""ForexFactory fetch: retry on 429/5xx and fall back to a last-good cache.

Live 2026-09-25 13:31 UTC: one FF 429 aborted calendar_check right after a
release slot (no retry, no cache)."""
from __future__ import annotations

import json
import time

import httpx
import pytest

from src import calendar as cal

_PAYLOAD = [{"title": "CPI m/m", "country": "USD", "date": "2026-09-25T08:30:00-04:00",
             "impact": "High", "forecast": "0.3%", "previous": "0.2%"}]


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(cal, "FF_CACHE_PATH", str(tmp_path / "ff.json"))
    monkeypatch.setattr(cal, "_ff_sleep", lambda s: None)


def _patch_responses(monkeypatch, responses):
    """Serve the given (status, body) pairs in order through a MockTransport."""
    it = iter(responses)
    calls = []

    def handler(request):
        status, body = next(it)
        calls.append(status)
        return httpx.Response(status, json=body)

    real_client = httpx.Client

    def fake_client(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real_client(*a, **kw)

    monkeypatch.setattr(cal.httpx, "Client", fake_client)
    return calls


def test_retries_429_then_succeeds_and_writes_cache(monkeypatch):
    calls = _patch_responses(monkeypatch, [(429, {}), (200, _PAYLOAD)])
    events = cal.fetch_calendar()
    assert calls == [429, 200]
    assert [e.title for e in events] == ["CPI m/m"]
    with open(cal.FF_CACHE_PATH, encoding="utf-8") as f:
        assert json.load(f)["data"] == _PAYLOAD


def test_all_attempts_fail_uses_cache(monkeypatch):
    with open(cal.FF_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump({"fetched_at": time.time(), "data": _PAYLOAD}, f)
    _patch_responses(monkeypatch, [(429, {})] * 3)
    events = cal.fetch_calendar()
    assert [e.country for e in events] == ["USD"]


def test_all_attempts_fail_no_cache_raises(monkeypatch):
    _patch_responses(monkeypatch, [(503, {})] * 3)
    with pytest.raises(httpx.HTTPStatusError):
        cal.fetch_calendar()


def test_stale_cache_is_not_used(monkeypatch):
    old = time.time() - (cal.FF_CACHE_MAX_AGE_H + 1) * 3600
    with open(cal.FF_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump({"fetched_at": old, "data": _PAYLOAD}, f)
    _patch_responses(monkeypatch, [(429, {})] * 3)
    with pytest.raises(httpx.HTTPStatusError):
        cal.fetch_calendar()


def test_non_retryable_404_fails_fast(monkeypatch):
    calls = _patch_responses(monkeypatch, [(404, {})])
    with pytest.raises(httpx.HTTPStatusError):
        cal.fetch_calendar()
    assert calls == [404]
