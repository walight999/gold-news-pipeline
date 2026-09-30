"""Monthly LINE quota report: push counting, usage split, month-end capture."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src import delivery_stats as ds
from src import line_client
from src import main as m
from src import store as store_mod
from src.line_client import LINE_PUSH_SOURCE_ID

ICT = timezone(timedelta(hours=7))


def _store():
    s = store_mod.Store(sheet_id="x", creds_json="{}")
    for tab in store_mod.SCHEMAS:
        s.data[tab] = {}
        s.dirty[tab] = set()
    return s


def _row(route, ts, eid, status="200"):
    return {"event_id": eid, "route_type": route, "sent_ts": ts, "line_status": status}


def test_carousel_counts_as_one_push():
    rows = [_row("digest", "2026-09-29T05:30:05Z", f"d{i}") for i in range(4)]
    rows += [_row("breaking", "2026-09-29T06:00:10Z", "b1"), _row("breaking", "2026-09-29T06:05:10Z", "b2")]
    rows += [_row("content", "2026-09-29T05:30:05Z", "c1"),                       # marker
             _row("digest", "2026-08-31T05:30:05Z", "old"),                        # other month
             _row("breaking", "2026-09-29T07:00:00Z", "f", status="429")]          # failed
    p = ds.monthly_pushes(rows, "2026-09")
    assert p == {"digest": {"pushes": 1, "cards": 4}, "breaking": {"pushes": 2, "cards": 2}}


def test_quota_breakdown_solves_group_size():
    pushes = {"breaking": {"pushes": 10, "cards": 10}, "digest": {"pushes": 5, "cards": 20},
              "scorecard": {"pushes": 3, "cards": 3}}
    g, rows = ds.quota_breakdown(pushes, usage=15 * 60 + 3)
    assert g == pytest.approx(60.0)
    assert [r["route"] for r in rows] == ["breaking", "digest", "scorecard"]
    assert rows[0]["pct"] == pytest.approx(600 / 903 * 100)


def _refresh(store, monkeypatch, usage, month):
    monkeypatch.setattr("src.utils_time.now_utc",
                        lambda: datetime(int(month[:4]), int(month[5:]), 15, tzinfo=timezone.utc))

    class C:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

        def get(self, url, headers=None):
            body = {"type": "limited", "value": 35000} if url.endswith("/quota") else {"totalUsage": usage}
            return httpx.Response(200, json=body, request=httpx.Request("GET", url))
    monkeypatch.setattr(line_client.httpx, "Client", C)
    line_client.refresh_line_quota_from_api(store, "tok")
    return json.loads(store.get("source_state", (LINE_PUSH_SOURCE_ID,))["items_last_hour"])


def test_month_end_reading_captured_on_usage_drop_and_kept(monkeypatch):
    s = _store()
    _refresh(s, monkeypatch, 34037, "2026-09")
    b = _refresh(s, monkeypatch, 120, "2026-09")          # LINE reset before ICT midnight
    assert (b["prev_api_month"], b["prev_api_usage"]) == ("2026-09", 34037)
    b = _refresh(s, monkeypatch, 300, "2026-10")          # ICT month flips later
    assert (b["prev_api_month"], b["prev_api_usage"]) == ("2026-09", 34037)   # not overwritten
    assert (b["api_month"], b["api_usage"]) == ("2026-10", 300)


def test_report_sent_once_on_the_first(monkeypatch):
    s = _store()
    s.upsert("source_state", {"source_id": LINE_PUSH_SOURCE_ID, "items_last_hour": json.dumps(
        {"api_month": "2026-10", "api_usage": 50, "prev_api_month": "2026-09",
         "prev_api_usage": 34037, "prev_api_limit": 35000})})
    for i in range(3):
        s.upsert("sent_log", _row("breaking", f"2026-09-2{i}T06:00:00Z", f"b{i}"))
    pushed = []

    class L:
        def push_flex(self, target, alt, contents):
            pushed.append(alt)
            return {"status": 200}
    monkeypatch.setattr(m.LineClient, "from_env", classmethod(lambda cls: L()))
    monkeypatch.setattr(m, "now_ict", lambda: datetime(2026, 10, 1, 23, 0, tzinfo=ICT))
    monkeypatch.setattr(m, "now_utc", lambda: datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc))
    monkeypatch.setenv("LINE_NEWS_TARGET", "U123,C456")
    assert m._monthly_quota_report(s) is True
    assert pushed == ["📊 โควต้า LINE 2026-09: ใช้ 34,037/35,000"]
    assert m._monthly_quota_report(s) is False               # once
    monkeypatch.setattr(m, "now_ict", lambda: datetime(2026, 10, 5, 23, 0, tzinfo=ICT))
    s2 = _store()
    assert m._monthly_quota_report(s2) is False               # only on the 1st-3rd


def test_billing_month_is_jst(monkeypatch):
    """22:30 ICT on the last day = 00:30 JST on the 1st → already next month."""
    s = _store()
    monkeypatch.setattr("src.utils_time.now_utc", lambda: datetime(2026, 9, 30, 15, 30, tzinfo=timezone.utc))

    class C:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

        def get(self, url, headers=None):
            body = {"type": "limited", "value": 35000} if url.endswith("/quota") else {"totalUsage": 55}
            return httpx.Response(200, json=body, request=httpx.Request("GET", url))
    monkeypatch.setattr(line_client.httpx, "Client", C)
    line_client.refresh_line_quota_from_api(s, "tok")
    b = json.loads(s.get("source_state", (LINE_PUSH_SOURCE_ID,))["items_last_hour"])
    assert b["api_month"] == "2026-10"
