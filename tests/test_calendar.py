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
        _ff_item("Core CPI m/m", "USD", "2026-05-15T08:30:00-04:00", "High",
                 forecast="0.5%", previous="0.3%"),
    ])
    b = pre_release_bubble(events[0], 15)
    assert b["type"] == "bubble"
    assert b["header"]["backgroundColor"] == "#DC2626"
    # Batch M: 3-pill (ECU/USD/XAU) currency-impact row.
    body_texts = _all_texts(b["body"])
    assert any("Currency Impact" in t for t in body_texts)
    # USD-event → ECU=USD + counter=EUR + XAU
    assert any(t.startswith("USD") for t in body_texts)
    assert any(t.startswith("EUR") for t in body_texts)
    assert any(t.startswith("XAU") for t in body_texts)
    # F:/P: inline label present (replaces old 3-col forecast/previous strip)
    assert any("F:" in t and "P:" in t for t in body_texts)


def test_gold_impact_directional_cpi_bearish():
    e = cal.parse_ff_payload([_ff_item("Core CPI m/m", "USD", "2026-05-15T08:30:00-04:00", "High")])[0]
    info = cal.gold_impact_directional(e)
    # Higher CPI = bearish gold
    assert "Bearish" in info["higher_is"]
    assert "Bullish" in info["lower_is"]


def test_gold_impact_directional_unemployment_claims_inverse():
    """User report 2026-05-23: 'Unemployment Claims' on the weekly preview
    showed XAU ↓ when forecast > previous, but higher claims = weaker
    labor = bullish gold. The regex was missing the FF series title
    'Unemployment Claims' (it only matched 'Unemployment Rate' and
    'Jobless Claims'), so the event fell through to the default
    higher=bearish rule."""
    from src.calendar import gold_impact_directional, CalEvent
    e = CalEvent(event_id="x", title="Unemployment Claims", country="USD",
                 impact="High", forecast="240K", previous="220K",
                 dt_utc=datetime(2026, 5, 29, 12, 30, tzinfo=timezone.utc))
    out = gold_impact_directional(e)
    assert "Bullish" in out["higher_is"], \
        f"Unemployment Claims should be inverse (higher=bullish gold), got {out}"


def test_event_impact_pills_gbp_cpi_cooling():
    """User example 2026-05-23: GBP CPI y/y, F: 3.0% / P: 3.3%
    (cooling). Expect GBP↓, USD↑, XAU↑ — image confirmed."""
    from src.calendar import event_impact_pills, CalEvent
    e = CalEvent(event_id="x", title="CPI y/y", country="GBP", impact="High",
                 forecast="3.0%", previous="3.3%",
                 dt_utc=datetime(2026, 5, 27, 6, 0, tzinfo=timezone.utc))
    pills = event_impact_pills(e)
    assert pills == [("GBP", "bearish"), ("USD", "bullish"), ("XAU", "bullish")]


def test_event_impact_pills_us_cpi_hot():
    """Hot US CPI: F > P → USD↑ (Fed hawkish), EUR↓, XAU↓."""
    from src.calendar import event_impact_pills, CalEvent
    e = CalEvent(event_id="x", title="Core CPI m/m", country="USD",
                 impact="High", forecast="0.5%", previous="0.3%",
                 dt_utc=datetime(2026, 5, 28, 12, 30, tzinfo=timezone.utc))
    pills = event_impact_pills(e)
    assert pills == [("USD", "bullish"), ("EUR", "bearish"), ("XAU", "bearish")]


def test_event_impact_pills_unemployment_claims_rising():
    """Higher US unemployment claims: weaker labor → USD↓ → XAU↑."""
    from src.calendar import event_impact_pills, CalEvent
    e = CalEvent(event_id="x", title="Unemployment Claims", country="USD",
                 impact="Medium", forecast="240K", previous="220K",
                 dt_utc=datetime(2026, 5, 29, 12, 30, tzinfo=timezone.utc))
    pills = event_impact_pills(e)
    assert pills == [("USD", "bearish"), ("EUR", "bullish"), ("XAU", "bullish")]


def test_event_impact_pills_neutral_when_forecast_equals_previous():
    """No directional signal → all 3 pills neutral."""
    from src.calendar import event_impact_pills, CalEvent
    e = CalEvent(event_id="x", title="GDP q/q", country="EUR", impact="High",
                 forecast="0.3%", previous="0.3%",
                 dt_utc=datetime(2026, 5, 28, 9, 0, tzinfo=timezone.utc))
    pills = event_impact_pills(e)
    assert pills == [("EUR", "neutral"), ("USD", "neutral"), ("XAU", "neutral")]


def test_event_impact_pills_missing_data_falls_back_to_neutral():
    """No forecast/previous → all neutral instead of crashing."""
    from src.calendar import event_impact_pills, CalEvent
    e = CalEvent(event_id="x", title="Some Speech", country="USD",
                 impact="Low", forecast="", previous="",
                 dt_utc=datetime(2026, 5, 28, 9, 0, tzinfo=timezone.utc))
    pills = event_impact_pills(e)
    assert all(d == "neutral" for _, d in pills)


def test_gold_impact_adp_treated_as_payroll_not_claims():
    """Defensive — ADP Employment Change is jobs CREATED, so higher =
    stronger labor = bearish gold (same direction as NFP). It must NOT
    accidentally match the claims/unemployment regex."""
    from src.calendar import gold_impact_directional, CalEvent
    e = CalEvent(event_id="x", title="ADP Employment Change", country="USD",
                 impact="Medium", forecast="180K", previous="160K",
                 dt_utc=datetime(2026, 5, 29, 12, 30, tzinfo=timezone.utc))
    out = gold_impact_directional(e)
    assert "Bearish" in out["higher_is"], \
        f"ADP higher should be bearish (jobs created), got {out}"


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
    # 3-pill layout (Batch M): bubble carries USD/EUR/XAU direction pills.
    body_texts = _all_texts(b["body"])
    assert any("Currency Impact" in t for t in body_texts)
    # All 3 pill labels present
    assert any(t.startswith("USD") for t in body_texts)
    assert any(t.startswith("EUR") for t in body_texts)
    assert any(t.startswith("XAU") for t in body_texts)


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


def test_calendar_price_strip_drops_missing_tickers():
    """Batch N: cells with no data are HIDDEN (not rendered as placeholders).
    Strip contains only the tickers we actually have a snapshot for."""
    from src.calendar import CalEvent
    e = CalEvent(event_id="x", title="CPI", country="USD", impact="High",
                 forecast="0.5%", previous="0.3%",
                 dt_utc=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc))
    # Only XAU has data; the other 4 are missing
    b = calendar_day_bubble(
        [e], "Fri 22 May 2026",
        xau_snapshot=(4500.0, 0.5),
        dxy_snapshot=None, hui_snapshot=None,
        gld_snapshot=None, thb_snapshot=None,
    )
    texts = _all_texts(b["body"])
    # The strip contains XAU label
    assert "XAU" in texts
    # No "—" placeholder + no "(no data)" leftover
    assert "—" not in texts
    assert "(no data)" not in texts
    # Other ticker labels absent
    assert "DXY" not in texts
    assert "HUI" not in texts


def test_calendar_price_strip_relabels_gld_as_spdr():
    """User preference 2026-05-23: 'SPDR' reads more clearly than 'GLD'."""
    from src.calendar import CalEvent
    e = CalEvent(event_id="x", title="CPI", country="USD", impact="High",
                 forecast="0.5%", previous="0.3%",
                 dt_utc=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc))
    b = calendar_day_bubble(
        [e], "Fri 22 May 2026",
        gld_snapshot=(410.0, 0.3),   # GLD ETF data → labelled SPDR
    )
    texts = _all_texts(b["body"])
    assert "SPDR" in texts
    assert "GLD" not in texts


def test_weekly_preview_header_carries_date_range():
    """Batch N: date range goes inside the title parens, not the right-side
    sub-label."""
    from src.calendar import CalEvent
    from src.line_flex import weekly_preview_bubble
    e = CalEvent(event_id="x", title="CPI", country="USD", impact="High",
                 forecast="0.5%", previous="0.3%",
                 dt_utc=datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc))
    b = weekly_preview_bubble([e], {}, "25/5/26 – 29/5/26")
    header_texts = _all_texts(b["header"])
    # Title text contains the date range in parens
    assert any("Week Ahead" in t and "25/5/26" in t for t in header_texts)
    # The right-side sub-label is empty or absent (no separate date text)
    title_with_range = [t for t in header_texts if "25/5/26" in t]
    assert len(title_with_range) == 1


def test_calendar_price_strip_dropped_when_all_missing():
    """Weekend / total fetch failure — entire strip dropped (rather
    than 5 placeholder cells)."""
    from src.calendar import CalEvent
    e = CalEvent(event_id="x", title="CPI", country="USD", impact="High",
                 forecast="0.5%", previous="0.3%",
                 dt_utc=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc))
    b = calendar_day_bubble(
        [e], "Fri 22 May 2026",
        xau_snapshot=None, dxy_snapshot=None, hui_snapshot=None,
        gld_snapshot=None, thb_snapshot=None,
    )
    texts = _all_texts(b["body"])
    # No price labels at all — strip absent
    assert "XAU" not in texts
    assert "DXY" not in texts


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
