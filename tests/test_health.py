"""§6.6 + §4.1 health: cooldown + per-tier thresholds."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.health import check_source_health, raise_warning, resolve_warning
from src.utils_time import iso_utc


def test_cooldown_suppresses_repeat(store):
    assert raise_warning(store, "forexlive", "tier2_no_item", cooldown_min=60) is True
    assert raise_warning(store, "forexlive", "tier2_no_item", cooldown_min=60) is False


def test_resolved_then_can_fire_again(store):
    raise_warning(store, "forexlive", "tier2_no_item", cooldown_min=60)
    resolved = resolve_warning(store, "forexlive", "tier2_no_item")
    assert resolved >= 1
    assert raise_warning(store, "forexlive", "tier2_no_item", cooldown_min=60) is True


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
