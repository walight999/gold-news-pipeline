"""calendar_check re-attempts a missed 04:40 ICT daily card (cancelled runs
2026-09-22..24 left those days without one)."""
from __future__ import annotations

from datetime import datetime, timedelta

from src import calendar as cal
from src import store as store_mod
from src.main import _calendar_daily_needs_catchup
from src.utils_time import ICT, UTC


def _store(sent_keys=()):
    s = store_mod.Store(sheet_id="x", creds_json="{}")
    for tab in store_mod.SCHEMAS:
        s.data[tab] = {}
    for k in sent_keys:
        row = {"event_id": k, "route_type": "calendar_daily", "line_status": "200"}
        s.data["sent_log"][store_mod._row_key("sent_log", row)] = row
    return s


def _ev(ict_dt, country="USD", impact="High"):
    return cal.CalEvent(event_id="x", title="CPI", country=country, impact=impact,
                        forecast="", previous="", dt_utc=ict_dt.astimezone(UTC))


NOW = datetime(2026, 9, 23, 6, 0, tzinfo=ICT)
EVENTS = [_ev(NOW.replace(hour=19, minute=30))]


def test_catchup_when_missing_inside_window():
    assert _calendar_daily_needs_catchup(_store(), NOW, EVENTS, {})


def test_no_catchup_when_already_sent():
    s = _store([f"cal_daily:{NOW:%Y-%m-%d}:main"])
    assert not _calendar_daily_needs_catchup(s, NOW, EVENTS, {})


def test_no_catchup_outside_window():
    assert not _calendar_daily_needs_catchup(_store(), NOW.replace(hour=4, minute=45), EVENTS, {})
    assert not _calendar_daily_needs_catchup(_store(), NOW.replace(hour=11, minute=0), EVENTS, {})


def test_no_catchup_on_empty_day():
    tomorrow = [_ev(NOW + timedelta(days=1))]
    low_only = [_ev(NOW.replace(hour=20), impact="Low")]
    assert not _calendar_daily_needs_catchup(_store(), NOW, tomorrow, {})
    assert not _calendar_daily_needs_catchup(_store(), NOW, low_only, {})
