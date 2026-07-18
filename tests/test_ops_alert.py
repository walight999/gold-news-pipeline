"""Tests for the LINE-independent ops-alert channel (src/ops_alert.py)."""
from __future__ import annotations

import pytest

from src import ops_alert


def test_unconfigured_is_noop(monkeypatch):
    monkeypatch.delenv("OPS_TG_BOT_TOKEN", raising=False)
    monkeypatch.delenv("OPS_TG_CHAT_ID", raising=False)
    assert ops_alert.is_configured() is False
    assert ops_alert.send_ops_alert("hi") is False


def test_partial_config_is_noop(monkeypatch):
    monkeypatch.setenv("OPS_TG_BOT_TOKEN", "123:abc")
    monkeypatch.delenv("OPS_TG_CHAT_ID", raising=False)
    assert ops_alert.is_configured() is False
    assert ops_alert.send_ops_alert("hi") is False


def test_should_mirror_critical_and_quota_but_not_routine():
    assert ops_alert.should_mirror("line_push_failing") is True   # CRITICAL
    assert ops_alert.should_mirror("watchdog_silence") is True    # CRITICAL
    assert ops_alert.should_mirror("line_quota_high") is True     # always-mirror early warning
    assert ops_alert.should_mirror("tier2_no_item") is False      # ROUTINE
    assert ops_alert.should_mirror("source_noisy:x") is False     # ROUTINE


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

    def post(self, url, json=None, headers=None):  # noqa: A002
        _FakeClient.last_url = url
        _FakeClient.last_json = json
        return _FakeResp(200)


def test_send_hits_telegram_bot_api(monkeypatch):
    monkeypatch.setenv("OPS_TG_BOT_TOKEN", "99:tok")
    monkeypatch.setenv("OPS_TG_CHAT_ID", "555")
    monkeypatch.setattr(ops_alert.httpx, "Client", _FakeClient)
    assert ops_alert.send_ops_alert("LINE down") is True
    assert _FakeClient.last_url == "https://api.telegram.org/bot99:tok/sendMessage"
    assert _FakeClient.last_json["chat_id"] == "555"
    assert _FakeClient.last_json["text"] == "LINE down"
    assert _FakeClient.last_json["disable_web_page_preview"] is True


class _RaisingClient:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json=None, headers=None):  # noqa: A002
        raise RuntimeError("network gone")


def test_send_never_raises_returns_false(monkeypatch):
    monkeypatch.setenv("OPS_TG_BOT_TOKEN", "99:tok")
    monkeypatch.setenv("OPS_TG_CHAT_ID", "555")
    monkeypatch.setattr(ops_alert.httpx, "Client", _RaisingClient)
    # Must swallow the error and report failure, not propagate.
    assert ops_alert.send_ops_alert("boom") is False


def test_mirror_health_filters_and_sends(monkeypatch):
    sent = {}

    def _fake_send(text):
        sent["text"] = text
        return True

    monkeypatch.setattr(ops_alert, "send_ops_alert", _fake_send)
    pairs = [
        ("_line_push", "line_quota_high", "LINE 410/500 (82%)"),
        ("_line_push", "line_push_failing", "5x in a row"),
        ("forexlive", "tier2_no_item", "quiet 40 min"),  # ROUTINE — must be dropped
    ]
    assert ops_alert.mirror_health(pairs, kind="alert") is True
    assert "line_quota_high" in sent["text"]
    assert "line_push_failing" in sent["text"]
    assert "tier2_no_item" not in sent["text"]


def test_mirror_health_noop_when_nothing_qualifies(monkeypatch):
    called = {"n": 0}

    def _fake_send(text):
        called["n"] += 1
        return True

    monkeypatch.setattr(ops_alert, "send_ops_alert", _fake_send)
    pairs = [("forexlive", "tier2_no_item", "quiet")]
    assert ops_alert.mirror_health(pairs, kind="alert") is False
    assert called["n"] == 0
