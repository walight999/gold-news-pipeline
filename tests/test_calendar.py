"""Tests for the economic calendar module + Flex builders."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src import calendar as cal
from src.line_flex import calendar_day_bubble, pre_release_bubble

ICT = timezone(timedelta(hours=7))


def _ff_item(title: str, country: str, dt_iso: str, impact: str = "High",
             forecast: str = "0.3%", previous: str = "0.2%") -> dict:
    return {"title": title, "country": country, "date": dt_iso,
            "impact": impact, "forecast": forecast, "previous": previous}


def test_parse_ff_payload_skips_malformed():
    data = [
        _ff_item("Core CPI m/m", "USD", "2026-05-15T08:30:00-04:00"),
        {"title": "no date"},                              # missing date
        {"title": "", "country": "USD", "date": "2026-05-15T08:30:00-04:00"},  # empty title
        _ff_item("ECB Rate Decision", "EUR", "2026-05-15T08:00:00-04:00", impact="High"),
    ]
    events = cal.parse_ff_payload(data)
    assert len(events) == 2
    assert events[0].country == "EUR"  # earlier than USD by 30min
    assert events[1].title == "Core CPI m/m"


def test_event_id_deterministic():
    a = cal.parse_ff_payload([_ff_item("CPI", "USD", "2026-05-15T08:30:00-04:00")])[0]
    b = cal.parse_ff_payload([_ff_item("CPI", "USD", "2026-05-15T08:30:00-04:00")])[0]
    assert a.event_id == b.event_id
    c = cal.parse_ff_payload([_ff_item("CPI", "EUR", "2026-05-15T08:30:00-04:00")])[0]
    assert a.event_id != c.event_id  # different country -> different id


def test_filter_today_ict():
    today_ict = datetime.now(ICT).replace(hour=20, minute=0, second=0, microsecond=0)
    yesterday_ict = today_ict - timedelta(days=1)
    events = cal.parse_ff_payload([
        _ff_item("Today CPI",     "USD", today_ict.isoformat()),
        _ff_item("Yesterday NFP", "USD", yesterday_ict.isoformat()),
    ])
    filtered = cal.filter_today_ict(events)
    assert len(filtered) == 1
    assert filtered[0].title == "Today CPI"


def test_filter_by_country_and_impact():
    events = cal.parse_ff_payload([
        _ff_item("Major USD", "USD", "2026-05-15T08:30:00-04:00", "High"),
        _ff_item("Tier-2 GBP", "GBP", "2026-05-15T05:00:00-04:00", "Medium"),
        _ff_item("Noise NZD",  "NZD", "2026-05-15T22:00:00-04:00", "Low"),
    ])
    only_usd = cal.filter_by_country(events, ("USD", "EUR"))
    assert {e.country for e in only_usd} == {"USD"}
    only_high = cal.filter_by_impact(events, ("High",))
    assert {e.impact for e in only_high} == {"High"}


def test_filter_upcoming_window():
    now = datetime.now(timezone.utc)
    events = cal.parse_ff_payload([
        _ff_item("In 5 min",  "USD", (now + timedelta(minutes=5)).isoformat()),
        _ff_item("In 20 min", "USD", (now + timedelta(minutes=20)).isoformat()),
        _ff_item("In 60 min", "USD", (now + timedelta(minutes=60)).isoformat()),
    ])
    upcoming = cal.filter_upcoming(events, 15, 25, ref=now)
    assert len(upcoming) == 1
    assert upcoming[0].title == "In 20 min"


def test_calendar_day_bubble_shape():
    events = cal.parse_ff_payload([
        _ff_item("Core CPI m/m", "USD", "2026-05-15T08:30:00-04:00", "High"),
        _ff_item("ECB Decision", "EUR", "2026-05-15T08:00:00-04:00", "High"),
    ])
    b = calendar_day_bubble(events, "Thu 15 May 2026")
    assert b["type"] == "bubble"
    assert b["size"] == "giga"
    assert b["header"]["backgroundColor"] == "#2563EB"
    assert len(b["body"]["contents"]) == 2


def test_pre_release_bubble_shape():
    events = cal.parse_ff_payload([
        _ff_item("Core CPI m/m", "USD", "2026-05-15T08:30:00-04:00", "High"),
    ])
    b = pre_release_bubble(events[0], 15)
    assert b["type"] == "bubble"
    # High-impact header is red
    assert b["header"]["backgroundColor"] == "#DC2626"
    # Header right shows the countdown
    sub_label = b["header"]["contents"][1]["text"]
    assert "T-15min" in sub_label
