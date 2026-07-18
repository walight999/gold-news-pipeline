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

    def post(self, url, json=None, headers=None):  # noqa: A002
        _FakeClient.last_url = url
        _FakeClient.last_json = json
        _FakeClient.last_headers = headers
        return _FakeResp(200)


def test_post_hits_the_webhook_endpoint(monkeypatch):
    monkeypatch.setattr(telegram_news.httpx, "Client", _FakeClient)
    client = telegram_news.TelegramNewsClient(base_url="https://news.justchum.com", secret="abc")
    res = client.post({"headline_th": "x"})
    assert res["status"] == 200
    # store defaults to None → health tracking is a safe no-op
    assert client.store is None
    # secret is NO LONGER in the URL — it rides the X-News-Secret header (audit H3)
    assert _FakeClient.last_url == "https://news.justchum.com/webhook/news"
    assert _FakeClient.last_headers == {"X-News-Secret": "abc"}
    assert _FakeClient.last_json == {"headline_th": "x"}


def test_post_calendar_hits_the_calendar_endpoint(monkeypatch):
    monkeypatch.setattr(telegram_news.httpx, "Client", _FakeClient)
    client = telegram_news.TelegramNewsClient(base_url="https://news.justchum.com", secret="abc")
    res = client.post_calendar({"events": []})
    assert res["status"] == 200
    assert _FakeClient.last_url == "https://news.justchum.com/webhook/calendar"
    assert _FakeClient.last_headers == {"X-News-Secret": "abc"}


def test_post_accuracy_hits_the_accuracy_endpoint(monkeypatch):
    monkeypatch.setattr(telegram_news.httpx, "Client", _FakeClient)
    client = telegram_news.TelegramNewsClient(base_url="https://news.justchum.com", secret="abc")
    res = client.post_accuracy({"accuracy": 0.68, "correct": 17, "total": 25, "days": 30})
    assert res["status"] == 200
    assert _FakeClient.last_url == "https://news.justchum.com/webhook/accuracy"
    assert _FakeClient.last_headers == {"X-News-Secret": "abc"}
    assert _FakeClient.last_json["accuracy"] == 0.68


class _FailClient:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json=None, headers=None):  # noqa: A002
        # 400 (client error) is non-retryable, so the health-tracking path is
        # exercised without tenacity's exponential backoff slowing the suite.
        return _FakeResp(400)


def test_send_records_telegram_health_failure_streak(store, monkeypatch):
    """5 failed worker posts in a row → _telegram_health consecutive_errors=5,
    the watchdog's telegram_push_failing threshold (primary channel monitor)."""
    from src.telegram_news import TELEGRAM_HEALTH_SOURCE_ID
    monkeypatch.setattr(telegram_news.httpx, "Client", _FailClient)
    client = telegram_news.TelegramNewsClient(
        base_url="https://news.justchum.com", secret="abc", store=store)
    for _ in range(5):
        client.post({"headline_th": "x"})
    row = store.get("source_state", (TELEGRAM_HEALTH_SOURCE_ID,))
    assert int(row["consecutive_errors"]) == 5
    assert row["last_status"] == "400"  # any non-200 advances the failure streak


def test_send_success_resets_telegram_health_streak(store, monkeypatch):
    from src.telegram_news import TELEGRAM_HEALTH_SOURCE_ID
    monkeypatch.setattr(telegram_news.httpx, "Client", _FailClient)
    client = telegram_news.TelegramNewsClient(
        base_url="https://news.justchum.com", secret="abc", store=store)
    client.post({"h": "x"})
    client.post({"h": "x"})
    monkeypatch.setattr(telegram_news.httpx, "Client", _FakeClient)  # now succeeds
    client.post({"h": "x"})
    row = store.get("source_state", (TELEGRAM_HEALTH_SOURCE_ID,))
    assert int(row["consecutive_errors"]) == 0


def test_watchdog_flags_telegram_push_failing(store, monkeypatch):
    from src.health import check_pipeline_health, write_heartbeat
    monkeypatch.setattr(telegram_news.httpx, "Client", _FailClient)
    write_heartbeat(store, items_seen=5)
    client = telegram_news.TelegramNewsClient(
        base_url="https://news.justchum.com", secret="abc", store=store)
    for _ in range(5):
        client.post({"h": "x"})
    types = [wt for wt, _ in check_pipeline_health(store)]
    assert "telegram_push_failing" in types


def test_watchdog_no_telegram_warning_when_never_used(store):
    """No _telegram_health row (NEWS_BOT_* unset) → never fires."""
    from src.health import check_pipeline_health, write_heartbeat
    write_heartbeat(store, items_seen=5)
    types = [wt for wt, _ in check_pipeline_health(store)]
    assert "telegram_push_failing" not in types


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
    assert _FakeClient.last_url == "https://news.justchum.com/webhook/event"
    assert _FakeClient.last_headers == {"X-News-Secret": "abc"}


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


def test_post_weekly_hits_the_weekly_endpoint(monkeypatch):
    monkeypatch.setattr(telegram_news.httpx, "Client", _FakeClient)
    client = telegram_news.TelegramNewsClient(base_url="https://news.justchum.com", secret="abc")
    res = client.post_weekly({"week_label": "x", "events": []})
    assert res["status"] == 200
    assert _FakeClient.last_url == "https://news.justchum.com/webhook/weekly"
    assert _FakeClient.last_headers == {"X-News-Secret": "abc"}


def test_build_weekly_payload_groups_with_day_label_and_utc_ts():
    from datetime import datetime, timezone
    e = _FakeEv(
        country="US", title="CPI", impact="high", _hhmm="19:30",
        dt_utc=datetime(2026, 7, 6, 12, 30, tzinfo=timezone.utc),
        dt_ict=datetime(2026, 7, 6, 19, 30),  # Monday
    )
    p = telegram_news.build_weekly_payload("weekly:2026-07-06", "6–10 Jul", [e])
    assert p["event_id"] == "weekly:2026-07-06"
    assert p["week_label"] == "6–10 Jul"
    ev = p["events"][0]
    assert ev["day"] == "Mon 6/7"
    assert ev["time"] == "19:30"
    assert ev["ts"] == "2026-07-06T12:30:00+00:00"
    assert ev["country"] == "US" and ev["title"] == "CPI" and ev["impact"] == "high"
    # gold-specific forecast/effect intentionally omitted (asset-neutral free bot)
    assert "forecast" not in ev and "effect" not in ev


def test_build_calendar_payload_sends_utc_iso_ts():
    from datetime import datetime, timezone
    e = _FakeEv(country="US", title="Core PCE", impact="high", forecast="", previous="", _hhmm="19:30",
                dt_utc=datetime(2026, 6, 25, 12, 30, tzinfo=timezone.utc))
    p = telegram_news.build_calendar_payload("cal:1", "Thu 25 Jun", [e])
    assert p["events"][0]["ts"] == "2026-06-25T12:30:00+00:00"
