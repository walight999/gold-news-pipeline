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
