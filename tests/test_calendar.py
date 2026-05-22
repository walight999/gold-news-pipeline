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
    # v3 layout drops the T-Xmin sub-label and the directional block; the
    # 3-column row carries Forecast/Previous/GOLD IMPACT instead.
    body_texts = _all_texts(b["body"])
    assert any("GOLD IMPACT" in t for t in body_texts)
    assert not any("↑" in t for t in body_texts), "directional block should be gone"


def test_gold_impact_directional_cpi_bearish():
    e = cal.parse_ff_payload([_ff_item("Core CPI m/m", "USD", "2026-05-15T08:30:00-04:00", "High")])[0]
    info = cal.gold_impact_directional(e)
    # Higher CPI = bearish gold
    assert "Bearish" in info["higher_is"]
    assert "Bullish" in info["lower_is"]


def test_gold_impact_directional_unemployment_inverse():
    e = cal.parse_ff_payload([_ff_item("Unemployment Rate", "USD", "2026-05-15T08:30:00-04:00", "High")])[0]
    info = cal.gold_impact_directional(e)
    # Higher unemployment = bullish gold (Fed dovish)
    assert "Bullish" in info["higher_is"]
    assert "Bearish" in info["lower_is"]


def test_gold_impact_directional_nfp():
    e = cal.parse_ff_payload([_ff_item("Non-Farm Employment Change", "USD", "2026-05-15T08:30:00-04:00", "High")])[0]
    info = cal.gold_impact_directional(e)
    assert "Bearish" in info["higher_is"]


def test_post_release_bubble_shape():
    from src.line_flex import post_release_bubble
    e = cal.parse_ff_payload([_ff_item("Core CPI m/m", "USD", "2026-05-15T08:30:00-04:00", "High")])[0]
    info = cal.gold_impact_directional(e)
    eff = cal.forecast_vs_previous_effect(e)
    b = post_release_bubble(e, info, effect=eff)
    assert b["type"] == "bubble"
    assert b["header"]["backgroundColor"] == "#DC2626"
    # v3: no-FRED path now mirrors pre-release — 3-col Forecast/Previous/GOLD IMPACT.
    body_texts = _all_texts(b["body"])
    assert any("GOLD IMPACT" in t for t in body_texts)


def _all_texts(node) -> list[str]:
    """Recursively collect every `text` value from a Flex node."""
    out: list[str] = []
    if not isinstance(node, dict):
        return out
    if node.get("type") == "text" and node.get("text"):
        out.append(node["text"])
    for child in node.get("contents", []) or []:
        out.extend(_all_texts(child))
    return out


def test_post_release_bubble_with_fred_actual():
    from src.line_flex import post_release_bubble
    e = cal.parse_ff_payload([_ff_item("Core CPI m/m", "USD",
                                       "2026-05-15T08:30:00-04:00", "High",
                                       forecast="0.3%", previous="0.4%")])[0]
    info = cal.gold_impact_directional(e)
    b = post_release_bubble(e, info, actual_text="+0.5%", surprise="beat",
                            verdict="🔴 Bearish gold")
    texts = _all_texts(b["body"])
    assert any("+0.5%" in t for t in texts)
    assert any("BEAT" in t for t in texts)
    # Verdict rendered as a colored XAU↓ pill (post-Batch-K). The pill
    # text is "XAU ↓" — check for the down arrow so we don't depend on
    # the exact character spacing.
    assert any("XAU" in t and "↓" in t for t in texts), \
        "expected 'XAU ↓' pill in bearish post-release bubble"


# ---------- XAU direction pill (Batch K) ----------


def test_xau_pill_bullish_is_green():
    from src.line_flex import _xau_direction_pill
    pill = _xau_direction_pill("bullish")
    assert pill["backgroundColor"] == "#059669"
    assert pill["contents"][0]["text"] == "XAU ↑"


def test_xau_pill_bearish_is_red():
    from src.line_flex import _xau_direction_pill
    pill = _xau_direction_pill("bearish")
    assert pill["backgroundColor"] == "#DC2626"
    assert pill["contents"][0]["text"] == "XAU ↓"


def test_xau_pill_neutral_is_amber():
    from src.line_flex import _xau_direction_pill
    pill = _xau_direction_pill("neutral")
    assert pill["backgroundColor"] == "#D97706"
    assert pill["contents"][0]["text"] == "XAU ≈"


def test_xau_pill_unknown_falls_back_to_neutral():
    """Defensive — any unrecognized label maps to neutral instead of
    crashing. The classifier sometimes returns words like 'mixed' or
    'unclear' which legitimately are neutral."""
    from src.line_flex import _xau_direction_pill
    pill = _xau_direction_pill("mixed")
    assert pill["backgroundColor"] == "#D97706"


def test_pre_release_bubble_uses_pill_not_emoji():
    """Pre-release bubble's 3rd column now carries a pill, not 🟢/🔴/🟡."""
    from src.calendar import CalEvent
    e = CalEvent(event_id="x", title="CPI m/m", country="USD",
                 impact="High", forecast="0.5%", previous="0.3%",
                 dt_utc=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc))
    eff = {"emoji": "🔴", "label": "bearish"}
    b = pre_release_bubble(e, minutes_to_release=15, effect=eff)
    texts = _all_texts(b["body"])
    assert any("XAU" in t and "↓" in t for t in texts)
    # Old plain emoji shouldn't appear in any text node (a pill carries
    # text "XAU ↓", not "🔴" as the standalone cell content).
    assert not any(t == "🔴" for t in texts)


# ---------- FRED unit tests (no network) ----------

def test_find_series_for_event_supported():
    from src.fred import find_series_for_event
    assert find_series_for_event("Core CPI m/m")[0] == "CPILFESL"
    assert find_series_for_event("CPI m/m")[0] == "CPIAUCSL"
    assert find_series_for_event("Non-Farm Employment Change")[0] == "PAYEMS"
    assert find_series_for_event("Unemployment Rate")[0] == "UNRATE"


def test_find_series_for_event_unsupported():
    from src.fred import find_series_for_event
    assert find_series_for_event("ECB Press Conference") is None
    assert find_series_for_event("BoE Inflation Report") is None


def test_parse_forecast_value():
    from src.fred import parse_forecast_value
    assert parse_forecast_value("0.3%") == 0.3
    assert parse_forecast_value("+0.4%") == 0.4
    assert parse_forecast_value("215K") == 215
    assert parse_forecast_value("+215K") == 215
    assert parse_forecast_value("3.9%") == 3.9
    assert parse_forecast_value("") is None
    assert parse_forecast_value("garbage") is None


def test_compute_surprise_label():
    from src.fred import compute_surprise_label
    # within 5% of 0.3 = 0.015 — so 0.31 is in-line, 0.40 is beat, 0.20 is miss
    assert compute_surprise_label(0.31, 0.3) == "in-line"
    assert compute_surprise_label(0.40, 0.3) == "beat"
    assert compute_surprise_label(0.20, 0.3) == "miss"


def test_reconcile_with_impact_cpi_beat():
    """CPI beat → higher actual → bearish gold."""
    from src.fred import reconcile_with_impact
    e = cal.parse_ff_payload([_ff_item("Core CPI m/m", "USD", "2026-05-15T08:30:00-04:00")])[0]
    info = cal.gold_impact_directional(e)
    verdict = reconcile_with_impact("beat", info)
    assert "Bearish" in verdict


def test_reconcile_with_impact_unemployment_beat():
    """Unemployment 'beat' (higher actual) → bullish gold (inverse rule)."""
    from src.fred import reconcile_with_impact
    e = cal.parse_ff_payload([_ff_item("Unemployment Rate", "USD", "2026-05-15T08:30:00-04:00")])[0]
    info = cal.gold_impact_directional(e)
    verdict = reconcile_with_impact("beat", info)
    assert "Bullish" in verdict


def test_fetch_actual_returns_none_without_key():
    from src.fred import fetch_actual
    # Empty key → None
    assert fetch_actual("Core CPI m/m", "") is None


def test_forecast_vs_previous_effect_cpi_lower_bullish():
    """CPI forecast lower than previous = expected cooling = bullish gold."""
    e = cal.parse_ff_payload([_ff_item("Core CPI m/m", "USD",
                                       "2026-05-15T08:30:00-04:00", "High",
                                       forecast="0.2%", previous="0.4%")])[0]
    eff = cal.forecast_vs_previous_effect(e)
    assert eff["emoji"] == "🟢"
    assert eff["label"] == "bullish"


def test_forecast_vs_previous_effect_cpi_higher_bearish():
    """CPI forecast higher than previous = expected hotter = bearish gold."""
    e = cal.parse_ff_payload([_ff_item("Core CPI m/m", "USD",
                                       "2026-05-15T08:30:00-04:00", "High",
                                       forecast="0.5%", previous="0.3%")])[0]
    eff = cal.forecast_vs_previous_effect(e)
    assert eff["emoji"] == "🔴"


def test_forecast_vs_previous_effect_unemployment_inverse():
    """Unemployment forecast HIGHER → bullish gold (inverse rule)."""
    e = cal.parse_ff_payload([_ff_item("Unemployment Rate", "USD",
                                       "2026-05-15T08:30:00-04:00", "High",
                                       forecast="4.2%", previous="4.0%")])[0]
    eff = cal.forecast_vs_previous_effect(e)
    assert eff["emoji"] == "🟢"
    assert eff["label"] == "bullish"


def test_forecast_vs_previous_effect_equal_neutral():
    e = cal.parse_ff_payload([_ff_item("Core CPI m/m", "USD",
                                       "2026-05-15T08:30:00-04:00", "High",
                                       forecast="0.3%", previous="0.3%")])[0]
    eff = cal.forecast_vs_previous_effect(e)
    assert eff["emoji"] == "🟡"
