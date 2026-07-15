"""FRED freshness guard + surprise-label tolerance floor.

Both guard the same failure: publishing (and scorecard-grading) a bogus
beat/miss verdict — one from a stale FRED observation, one from a tolerance
band that collapses to noise on a small forecast.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

from src import fred


def _obs(pairs):
    return [{"value": v, "date": d} for v, d in pairs]


def test_fetch_actual_rejects_stale_monthly_observation():
    """CPI released mid-July for June: if FRED still shows May (a full extra
    period stale), reject → caller falls back instead of publishing last
    month's number as this month's actual."""
    rel = datetime(2026, 7, 15, tzinfo=timezone.utc)
    stale = _obs([("310.5", "2026-05-01"), ("309.0", "2026-04-01")])  # lag ~75d > 62
    with patch.object(fred, "_get_observations", return_value=stale):
        assert fred.fetch_actual("CPI m/m", api_key="k", release_dt=rel) is None


def test_fetch_actual_accepts_fresh_observation_with_floor():
    rel = datetime(2026, 7, 15, tzinfo=timezone.utc)
    fresh = _obs([("310.5", "2026-06-01"), ("309.0", "2026-05-01")])  # lag ~44d < 62
    with patch.object(fred, "_get_observations", return_value=fresh):
        r = fred.fetch_actual("CPI m/m", api_key="k", release_dt=rel)
        assert r is not None
        assert r.surprise_floor == 0.05                         # mom_pct floor attached
        assert r.actual_value == round((310.5 - 309.0) / 309.0 * 100, 1)  # 1-decimal


def test_fetch_actual_without_release_dt_skips_freshness():
    """Back-compat: no release_dt → no freshness check (old behaviour)."""
    old = _obs([("310.5", "2026-01-01"), ("309.0", "2025-12-01")])
    with patch.object(fred, "_get_observations", return_value=old):
        assert fred.fetch_actual("CPI m/m", api_key="k") is not None


def test_fetch_actual_weekly_freshness_window():
    rel = datetime(2026, 7, 16, tzinfo=timezone.utc)   # Thursday claims
    fresh = _obs([("232000", "2026-07-11"), ("225000", "2026-07-04")])  # lag 5d
    with patch.object(fred, "_get_observations", return_value=fresh):
        assert fred.fetch_actual("Initial Jobless Claims", api_key="k", release_dt=rel) is not None
    stale = _obs([("232000", "2026-06-20"), ("225000", "2026-06-13")])  # lag 26d > 21
    with patch.object(fred, "_get_observations", return_value=stale):
        assert fred.fetch_actual("Initial Jobless Claims", api_key="k", release_dt=rel) is None


def test_surprise_label_absolute_floor():
    # 0.24 vs 0.2 forecast: relative band = 5% of 0.2 = 0.01 → 'beat' without a floor;
    # floor 0.05 makes it correctly 'in-line' (the tape read it as 0.2, in-line).
    assert fred.compute_surprise_label(0.24, 0.2, abs_tol=0.05) == "in-line"
    assert fred.compute_surprise_label(0.3, 0.2, abs_tol=0.05) == "beat"
    assert fred.compute_surprise_label(0.1, 0.2, abs_tol=0.05) == "miss"
    # No floor → prior behaviour preserved.
    assert fred.compute_surprise_label(0.24, 0.2) == "beat"


def test_surprise_label_zero_forecast():
    assert fred.compute_surprise_label(0.0, 0.0) == "in-line"
    assert fred.compute_surprise_label(0.03, 0.0, abs_tol=0.05) == "in-line"
    assert fred.compute_surprise_label(0.5, 0.0) == "beat"
    assert fred.compute_surprise_label(-0.5, 0.0) == "miss"
