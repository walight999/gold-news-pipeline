"""Tests for the off-GAS CHUM News Bot delivery target (src/telegram_news.py)."""
from __future__ import annotations

import pytest

from src import telegram_news


def test_from_env_none_when_unset(monkeypatch):
    monkeypatch.delenv("NEWS_BOT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("NEWS_BOT_SECRET", raising=False)
    assert telegram_news.TelegramNewsClient.from_env() is None


def test_from_env_none_when_partial(monkeypatch):
    monkeypatch.setenv("NEWS_BOT_WEBHOOK_URL", "https://news.justchum.com")
    monkeypatch.delenv("NEWS_BOT_SECRET", raising=False)
    assert telegram_news.TelegramNewsClient.from_env() is None


def test_from_env_configured_strips_trailing_slash(monkeypatch):
    monkeypatch.setenv("NEWS_BOT_WEBHOOK_URL", "https://news.justchum.com/")
    monkeypatch.setenv("NEWS_BOT_SECRET", "s3cr3t")
    client = telegram_news.TelegramNewsClient.from_env()
    assert client is not None
    assert client.base_url == "https://news.justchum.com"
    assert client.secret == "s3cr3t"


def test_build_payload_shape():
    p = telegram_news.build_payload(
        event_id="e1",
        route="breaking",
        category="Inflation",
        tone="risk_off",
        impact_level="high",
        headline_th="CPI สูงกว่าคาด",
        body_th=["a", "b"],
        impact_th="กดดันทอง",
        source="Forexlive",
        url="https://x/a",
    )
    assert p["event_id"] == "e1"
    assert p["route"] == "breaking"
    assert p["body_th"] == ["a", "b"]
    assert p["ts"] is None


def test_build_payload_body_defaults_to_list():
    p = telegram_news.build_payload(
        event_id="e", route="alert", category=None, tone=None, impact_level=None,
        headline_th="h", body_th=None, impact_th=None, source=None, url=None,
    )
    assert p["body_th"] == []


class _FakeResp:
    def __init__(self, status_code: int):
        self.status_code = status_code
        self.text = "ok"
        self.request = None


class _FakeClient:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json=None):  # noqa: A002
        _FakeClient.last_url = url
        _FakeClient.last_json = json
        return _FakeResp(200)


def test_post_hits_the_webhook_endpoint(monkeypatch):
    monkeypatch.setattr(telegram_news.httpx, "Client", _FakeClient)
    client = telegram_news.TelegramNewsClient(base_url="https://news.justchum.com", secret="abc")
    res = client.post({"headline_th": "x"})
    assert res["status"] == 200
    assert _FakeClient.last_url == "https://news.justchum.com/webhook/news/abc"
    assert _FakeClient.last_json == {"headline_th": "x"}


def test_post_calendar_hits_the_calendar_endpoint(monkeypatch):
    monkeypatch.setattr(telegram_news.httpx, "Client", _FakeClient)
    client = telegram_news.TelegramNewsClient(base_url="https://news.justchum.com", secret="abc")
    res = client.post_calendar({"events": []})
    assert res["status"] == 200
    assert _FakeClient.last_url == "https://news.justchum.com/webhook/calendar/abc"


class _FakeEv:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    @property
    def hhmm_ict(self):
        return self._hhmm


def test_post_event_hits_the_event_endpoint(monkeypatch):
    monkeypatch.setattr(telegram_news.httpx, "Client", _FakeClient)
    client = telegram_news.TelegramNewsClient(base_url="https://news.justchum.com", secret="abc")
    res = client.post_event({"phase": "pre"})
    assert res["status"] == 200
    assert _FakeClient.last_url == "https://news.justchum.com/webhook/event/abc"


def test_build_event_payload_pre_and_post():
    ev = _FakeEv(country="US", title="Core PCE", impact="high", forecast="0.3%", previous="0.2%")
    pre = telegram_news.build_event_payload(event_id="precal:1", phase="pre", ev=ev, mins_to=15)
    assert pre["phase"] == "pre" and pre["mins_to"] == 15 and pre["actual"] is None
    post = telegram_news.build_event_payload(event_id="postcal:1", phase="post", ev=ev, actual="0.4%", detail_th="ร้อน")
    assert post["phase"] == "post" and post["actual"] == "0.4%" and post["detail_th"] == "ร้อน"


def test_build_calendar_payload_maps_events():
    e = _FakeEv(country="US", title="Core PCE", impact="high", forecast="0.3%", previous="0.2%", _hhmm="19:30")
    blank = _FakeEv(country="JP", title="CPI", impact="medium", forecast="", previous="", _hhmm="06:30")
    p = telegram_news.build_calendar_payload("cal_daily:2026-06-25:main", "Thu 25 Jun", [e, blank])
    assert p["event_id"] == "cal_daily:2026-06-25:main"
    assert p["events"][0] == {
        "time": "19:30", "ts": None, "country": "US", "title": "Core PCE",
        "impact": "high", "forecast": "0.3%", "previous": "0.2%",
    }
    # empty forecast/previous become None (worker hides the F/P line)
    assert p["events"][1]["forecast"] is None
    assert p["events"][1]["previous"] is None


def test_build_calendar_payload_sends_utc_iso_ts():
    from datetime import datetime, timezone
    e = _FakeEv(country="US", title="Core PCE", impact="high", forecast="", previous="", _hhmm="19:30",
                dt_utc=datetime(2026, 6, 25, 12, 30, tzinfo=timezone.utc))
    p = telegram_news.build_calendar_payload("cal:1", "Thu 25 Jun", [e])
    assert p["events"][0]["ts"] == "2026-06-25T12:30:00+00:00"
