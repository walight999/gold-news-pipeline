"""LINE push outcome tracking — counters used by watchdog to detect
silent push failures and 500-msg/month quota exhaustion."""
from __future__ import annotations

from src.line_client import (
    LINE_FREE_TIER_QUOTA,
    LINE_PUSH_SOURCE_ID,
    get_line_quota_status,
    record_line_outcome,
)


def test_record_outcome_increments_monthly_count_on_success(store):
    record_line_outcome(store, 200)
    record_line_outcome(store, 200)
    record_line_outcome(store, 200)
    qs = get_line_quota_status(store)
    assert qs["count"] == 3
    assert qs["limit"] == LINE_FREE_TIER_QUOTA


def test_record_outcome_does_not_increment_on_failure(store):
    record_line_outcome(store, 500)
    record_line_outcome(store, 429)
    qs = get_line_quota_status(store)
    assert qs["count"] == 0


def test_record_outcome_increments_consecutive_errors(store):
    """5 failures in a row → consecutive_errors=5 → watchdog warning."""
    for _ in range(5):
        record_line_outcome(store, 500)
    row = store.get("source_state", (LINE_PUSH_SOURCE_ID,))
    assert int(row["consecutive_errors"]) == 5


def test_record_outcome_resets_streak_on_success(store):
    """One success resets the consecutive-failure streak — push channel
    is healthy again."""
    record_line_outcome(store, 500)
    record_line_outcome(store, 500)
    record_line_outcome(store, 200)
    row = store.get("source_state", (LINE_PUSH_SOURCE_ID,))
    assert int(row["consecutive_errors"]) == 0


def test_quota_pct_calculation(store):
    """80% of 500 = 400. Need exact int math."""
    for _ in range(400):
        record_line_outcome(store, 200)
    qs = get_line_quota_status(store)
    assert qs["pct"] == 80


def test_watchdog_flags_line_quota_high_at_80pct(store):
    """500-msg free tier — flag at >=80%."""
    from src.health import check_pipeline_health, write_heartbeat
    write_heartbeat(store, items_seen=5)
    for _ in range(450):
        record_line_outcome(store, 200)
    warns = check_pipeline_health(store)
    types = [wt for wt, _ in warns]
    assert "line_quota_high" in types


def test_watchdog_flags_line_push_failing_at_5_consecutive(store):
    """5 consecutive 5xx → channel may be dead."""
    from src.health import check_pipeline_health, write_heartbeat
    write_heartbeat(store, items_seen=5)
    for _ in range(5):
        record_line_outcome(store, 502)
    warns = check_pipeline_health(store)
    types = [wt for wt, _ in warns]
    assert "line_push_failing" in types


def test_watchdog_no_warning_at_low_volume(store):
    """A few messages — no warnings should fire."""
    from src.health import check_pipeline_health, write_heartbeat
    write_heartbeat(store, items_seen=5)
    for _ in range(10):
        record_line_outcome(store, 200)
    warns = check_pipeline_health(store)
    types = [wt for wt, _ in warns]
    assert "line_quota_high" not in types
    assert "line_push_failing" not in types
