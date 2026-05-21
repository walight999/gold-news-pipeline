"""§6.6 + §4.1 health: cooldown + per-tier thresholds."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.health import check_source_health, raise_warning, resolve_warning
from src.utils_time import iso_utc


def test_cooldown_suppresses_repeat(store):
    assert raise_warning(store, "forexlive", "tier2_no_item", cooldown_min=60) is True
    assert raise_warning(store, "forexlive", "tier2_no_item", cooldown_min=60) is False


def test_resolved_warning_still_suppressed_by_cooldown(store):
    """After fix: cooldown applies even to resolved warnings — prevents the
    flap-loop where a brief oscillating condition bypasses the cooldown by
    resolving and instantly re-firing."""
    assert raise_warning(store, "forexlive", "tier2_no_item", cooldown_min=60) is True
    resolved = resolve_warning(store, "forexlive", "tier2_no_item")
    assert resolved >= 1
    # Cooldown is still active for this (source, type) — second raise within
    # the window is suppressed regardless of resolution status.
    assert raise_warning(store, "forexlive", "tier2_no_item", cooldown_min=60) is False


def test_cooldown_expires_then_can_fire(store):
    """After the cooldown window passes, a new warning IS allowed."""
    assert raise_warning(store, "forexlive", "tier2_no_item", cooldown_min=60) is True
    resolve_warning(store, "forexlive", "tier2_no_item")
    # With 0-minute cooldown the warning is no longer suppressed.
    assert raise_warning(store, "forexlive", "tier2_no_item", cooldown_min=0) is True


def test_tier1_no_success_flagged(store):
    sid = "marketwatch"
    long_ago = datetime.now(timezone.utc) - timedelta(minutes=90)
    store.upsert("source_state", {
        "source_id": sid, "last_attempt_ts": iso_utc(long_ago),
        "last_success_ts": iso_utc(long_ago), "last_item_ts": iso_utc(long_ago),
        "consecutive_errors": 0,
    })
    source = {"id": sid, "tier": 1, "role": "macro"}
    warns = check_source_health(store, source, {"tier1_no_success_minutes": 60,
                                                "tier2_no_item_minutes": 30,
                                                "http_consecutive_errors_threshold": 3})
    assert (sid, "tier1_no_success") in warns


def test_tier0_event_day_no_success(store):
    sid = "fed"
    long_ago = datetime.now(timezone.utc) - timedelta(minutes=30)
    store.upsert("source_state", {
        "source_id": sid, "last_attempt_ts": iso_utc(long_ago),
        "last_success_ts": iso_utc(long_ago), "last_item_ts": iso_utc(long_ago),
        "consecutive_errors": 0,
    })
    source = {"id": sid, "tier": 0, "role": "policy"}
    warns = check_source_health(store, source, {"tier1_no_success_minutes": 60,
                                                "tier2_no_item_minutes": 30,
                                                "http_consecutive_errors_threshold": 3},
                                is_event_day=True)
    assert (sid, "tier0_event_day_no_success") in warns
