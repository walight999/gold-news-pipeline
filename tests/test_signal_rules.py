"""2026-09-27 signal-rule corrections: US-only FRED, correct series,
statement-driven rate decisions, scenario pills, new rules, B/T units."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src import calendar as cal
from src import fred


def _ev(title, country="USD", forecast="0.3%", previous="0.2%"):
    return cal.CalEvent(event_id="x", title=title, country=country, impact="High",
                        forecast=forecast, previous=previous,
                        dt_utc=datetime(2026, 9, 11, 12, 30, tzinfo=timezone.utc))


# ---- FRED: US only + right series ----

@pytest.mark.parametrize("country", ["GBP", "CAD", "EUR", "CHF"])
def test_fred_refuses_non_usd(country, monkeypatch):
    called = []
    monkeypatch.setattr(fred, "_get_observations", lambda *a, **k: called.append(1) or [])
    assert fred.fetch_actual("Retail Sales m/m", "key", country=country) is None
    assert not called, "must not even query FRED for a non-US release"


@pytest.mark.parametrize("title,sid", [
    ("PPI m/m", "PPIFIS"),
    ("Core PPI m/m", "PPIFES"),
    ("Retail Sales m/m", "RSAFS"),
    ("Core Retail Sales m/m", "RSFSXMV"),
    ("Durable Goods Orders m/m", "DGORDER"),
    ("Core Durable Goods Orders m/m", "ADXTNO"),
    ("Core CPI m/m", "CPILFESL"),
    ("CPI m/m", "CPIAUCSL"),
])
def test_series_mapping(title, sid):
    assert fred.find_series_for_event(title)[0] == sid


def test_parse_billions_and_trillions():
    assert fred.parse_forecast_value("-78.2B") == -78.2
    assert fred.parse_forecast_value("1.2T") == 1.2


# ---- rate decisions are statement-driven ----

@pytest.mark.parametrize("title", ["Federal Funds Rate", "Main Refinancing Rate",
                                   "Official Bank Rate", "BOJ Policy Rate", "Cash Rate"])
def test_rate_decisions_neutral(title):
    e = _ev(title, forecast="4.00%", previous="4.25%")
    assert cal.is_statement_driven(e)
    assert "Neutral" in cal.gold_impact_directional(e)["higher_is"]
    assert all(d == "neutral" for _, d in cal.event_impact_pills(e, actual_text="3.75%"))
    assert all(d == "neutral" for _, d in cal.scenario_pills(e, True))


# ---- scenario pills (T-15 card) ----

def test_scenario_pills_cpi():
    e = _ev("Core CPI m/m")
    assert cal.scenario_pills(e, True) == [("USD", "bullish"), ("EUR", "bearish"), ("XAU", "bearish")]
    assert cal.scenario_pills(e, False) == [("USD", "bearish"), ("EUR", "bullish"), ("XAU", "bullish")]


def test_scenario_pills_claims_inverse():
    e = _ev("Unemployment Claims", forecast="210K", previous="205K")
    # Higher claims = weaker labour = weaker USD = bullish gold.
    assert cal.scenario_pills(e, True)[2] == ("XAU", "bullish")
    assert cal.scenario_pills(e, False)[2] == ("XAU", "bearish")


def test_scenario_pills_unknown_event_neutral():
    assert all(d == "neutral" for _, d in cal.scenario_pills(_ev("Trade Balance"), True))


# ---- new rules ----

@pytest.mark.parametrize("title", ["Average Hourly Earnings m/m", "JOLTS Job Openings",
                                   "Employment Cost Index q/q", "Philly Fed Manufacturing Index",
                                   "Empire State Manufacturing Index", "Building Permits",
                                   "New Home Sales"])
def test_new_rules_higher_is_bearish(title):
    info = cal.gold_impact_directional(_ev(title))
    assert "Bearish" in info["higher_is"] and "Bullish" in info["lower_is"]


def test_adp_is_not_served_nfp():
    """ADP is the private survey — it must never get PAYEMS (the BLS NFP print)."""
    assert fred.find_series_for_event("ADP Non-Farm Employment Change") is None
    assert fred.find_series_for_event("Non-Farm Employment Change")[0] == "PAYEMS"
