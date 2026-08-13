"""A source that keeps failing must be polled less often, not forever.

Benzinga went behind Cloudflare on 2026-06-19 and was still polled every 5 min
55 days and 1149 consecutive failures later — roughly 16,000 pointless requests
— because the only escape hatch was a human editing sources.yaml.
"""
from __future__ import annotations

from datetime import timedelta

from src import fetcher
from src.utils_time import iso_utc, now_utc


class _FakeStore:
    def __init__(self, state: dict[str, dict]):
        self._state = state

    def get(self, tab, key):
        assert tab == "source_state"
        return self._state.get(key[0])


def _src(sid="benzinga", poll_min=5, enabled=True):
    return {"id": sid, "poll_min": poll_min, "enabled": enabled}


def _state(minutes_ago: float, errors: int):
    return {"last_attempt_ts": iso_utc(now_utc() - timedelta(minutes=minutes_ago)),
            "consecutive_errors": str(errors)}


# ---- effective_poll_min ----

def test_healthy_source_keeps_its_configured_interval():
    for errors in range(fetcher.ERROR_BACKOFF_AFTER):
        assert fetcher.effective_poll_min(5, errors) == 5, errors


def test_interval_doubles_per_error_streak():
    assert fetcher.effective_poll_min(5, 5) == 10
    assert fetcher.effective_poll_min(5, 10) == 20
    assert fetcher.effective_poll_min(5, 15) == 40


def test_backoff_is_capped():
    """1149 errors must not compute a 10-year interval."""
    assert fetcher.effective_poll_min(5, 1149) == fetcher.ERROR_BACKOFF_CAP_MIN
    assert fetcher.effective_poll_min(60, 10_000) == fetcher.ERROR_BACKOFF_CAP_MIN


def test_backoff_never_shortens_the_interval():
    """A slow-polling source must not be sped up by failing."""
    for errors in (0, 4, 5, 50, 5000):
        assert fetcher.effective_poll_min(360, errors) >= 360


def test_garbage_error_count_is_treated_as_healthy():
    assert fetcher.effective_poll_min(5, 0) == 5


# ---- plan_fetch integration ----

def test_dead_source_is_skipped_between_backoff_windows():
    """benzinga's real numbers: 5-min cadence, 1149 errors → 6h interval, so a
    poll 30 min ago is skipped where a healthy source would be fetched."""
    store = _FakeStore({"benzinga": _state(minutes_ago=30, errors=1149)})
    plan = fetcher.plan_fetch([_src()], store)

    assert plan.sources == []
    assert plan.skipped_polled_recently == ["benzinga"]


def test_dead_source_is_still_retried_once_the_window_elapses():
    """Backoff, not a hard disable — the source must get its chance to recover."""
    store = _FakeStore({"benzinga": _state(minutes_ago=400, errors=1149)})
    plan = fetcher.plan_fetch([_src()], store)

    assert [s["id"] for s in plan.sources] == ["benzinga"]


def test_healthy_source_is_unaffected():
    store = _FakeStore({"forexlive": _state(minutes_ago=6, errors=0)})
    plan = fetcher.plan_fetch([_src("forexlive")], store)

    assert [s["id"] for s in plan.sources] == ["forexlive"]


def test_recovered_source_returns_to_full_cadence_immediately():
    """_update_source_state zeroes the streak on the first success, so the very
    next plan uses the configured interval again."""
    store = _FakeStore({"benzinga": _state(minutes_ago=6, errors=0)})
    plan = fetcher.plan_fetch([_src()], store)

    assert [s["id"] for s in plan.sources] == ["benzinga"]


def test_force_overrides_the_backoff():
    store = _FakeStore({"benzinga": _state(minutes_ago=1, errors=1149)})
    plan = fetcher.plan_fetch([_src()], store, force=True)

    assert [s["id"] for s in plan.sources] == ["benzinga"]


def test_disabled_source_is_still_skipped_outright():
    store = _FakeStore({"benzinga": _state(minutes_ago=999, errors=1149)})
    plan = fetcher.plan_fetch([_src(enabled=False)], store)

    assert plan.sources == []
    assert plan.skipped_disabled == ["benzinga"]


def test_source_with_no_state_is_fetched():
    """First run ever — no source_state row yet."""
    plan = fetcher.plan_fetch([_src()], _FakeStore({}))

    assert [s["id"] for s in plan.sources] == ["benzinga"]
