"""2026-10-02: FinancialJuice noise filters + scorecard graded per release minute."""
from __future__ import annotations

from types import SimpleNamespace

import yaml

from src import main as m
from src import scorecard


def _item(title, src="financialjuice"):
    return SimpleNamespace(title=title, source_id=src)


FILTERS = yaml.safe_load(open("config/sources.yaml", encoding="utf-8"))["noise_filters"]


def test_noise_filter_drops_promo_and_data_prints_only_for_fj():
    items = [_item("Goldman Sachs: PCE Inflation - FJElite"),
             _item("US Chicago PMI Actual 58.8 (Forecast 51, Previous 47.1)"),
             _item("US ISM Manufacturing PMI Actual 54.5 (Previous 54.6)"),
             _item("Fed's Waller: actual inflation still too high"),
             _item("US Chicago PMI Actual 58.8 (Forecast 51)", src="forexlive")]
    kept = [i.title for i in m._drop_noise(items, FILTERS)]
    assert kept == ["Fed's Waller: actual inflation still too high",
                    "US Chicago PMI Actual 58.8 (Forecast 51)"]


def test_noise_filter_bad_regex_is_ignored():
    items = [_item("anything")]
    assert m._drop_noise(items, [{"pattern": "("}]) == items


def _row(ts, title, d, routed="calendar_post", r15="0.4"):
    return {"first_seen_ts": ts, "title": title, "predicted_dir": d, "routed_as": routed,
            "xau_return_15m": r15, "country": "USD"}


def test_conflicting_minute_is_not_graded_without_a_composite_call():
    rows = [_row("2026-09-30T12:30:00Z", "Core PCE", "bull"),
            _row("2026-09-30T12:30:00Z", "Final GDP", "bear"),
            _row("2026-09-30T12:30:00Z", "GDP Price Index", "neutral")]
    out = scorecard.collapse_minutes(rows, lambda g: None)
    assert len(out) == 1 and out[0]["predicted_dir"] == "neutral"
    assert out[0]["title"] == "Core PCE + Final GDP + GDP Price Index"


def test_minute_uses_the_cards_gated_call_or_unanimous_pills():
    rows = [_row("2026-10-02T12:30:00Z", "NFP", "bear"), _row("2026-10-02T12:30:00Z", "Unemployment Rate", "bear")]
    assert scorecard.collapse_minutes(rows, lambda g: None)[0]["predicted_dir"] == "bear"
    assert scorecard.collapse_minutes(rows, lambda g: "bull")[0]["predicted_dir"] == "bull"


def test_singletons_and_speech_pass_through():
    rows = [_row("2026-09-30T12:15:00Z", "ADP", "bear"),
            _row("2026-09-30T14:32:00Z", "Lagarde Speaks", "bear", routed="speech"),
            _row("2026-09-30T14:32:00Z", "Waller Speaks", "bull", routed="speech")]
    out = scorecard.collapse_minutes(rows, lambda g: None)
    assert [(r["title"], r["predicted_dir"]) for r in out] == [
        ("Lagarde Speaks", "bear"), ("Waller Speaks", "bull"), ("ADP", "bear")]
