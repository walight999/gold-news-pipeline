"""§6.6 + §4.1 health: cooldown + per-tier thresholds."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.health import (
    HEARTBEAT_SOURCE_ID,
    check_pipeline_health,
    check_source_health,
    raise_warning,
    resolve_warning,
    write_heartbeat,
)
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


# ---------------- Pipeline self-monitoring (watchdog) ----------------


def test_watchdog_flags_silence_when_no_heartbeat(store):
    """Cold start / first run scenario: no heartbeat row at all → silence
    warning fires immediately."""
    warns = check_pipeline_health(store)
    types = [wt for wt, _ in warns]
    assert "watchdog_silence" in types


def test_watchdog_flags_silence_when_heartbeat_stale(store):
    """Heartbeat from 60 min ago → triggers silence warning (default
    threshold = 25 min)."""
    stale = datetime.now(timezone.utc) - timedelta(minutes=60)
    store.upsert("source_state", {
        "source_id": HEARTBEAT_SOURCE_ID,
        "last_success_ts": iso_utc(stale),
        "last_item_ts": iso_utc(stale),
    })
    warns = check_pipeline_health(store)
    types = [wt for wt, _ in warns]
    assert "watchdog_silence" in types


def test_watchdog_healthy_when_heartbeat_fresh(store):
    """Fresh heartbeat + recent items → no warnings."""
    fresh = datetime.now(timezone.utc) - timedelta(minutes=2)
    store.upsert("source_state", {
        "source_id": HEARTBEAT_SOURCE_ID,
        "last_success_ts": iso_utc(fresh),
        "last_item_ts": iso_utc(fresh),
    })
    assert check_pipeline_health(store) == []


def test_watchdog_flags_no_items_during_market_hours(store):
    """Heartbeat ticking (cron is fine) but no items for 4 hours → scraper
    or network suspected."""
    fresh = datetime.now(timezone.utc) - timedelta(minutes=2)
    stale_items = datetime.now(timezone.utc) - timedelta(minutes=240)
    store.upsert("source_state", {
        "source_id": HEARTBEAT_SOURCE_ID,
        "last_success_ts": iso_utc(fresh),
        "last_item_ts": iso_utc(stale_items),
    })
    warns = check_pipeline_health(store)
    types = [wt for wt, _ in warns]
    assert "watchdog_no_items" in types
    # Silence should NOT also fire — heartbeat is fresh.
    assert "watchdog_silence" not in types


def test_write_heartbeat_preserves_last_item_when_zero(store):
    """A quiet-news iteration (items_seen=0) must NOT clobber the prior
    last_item_ts — otherwise the no-items watchdog never fires."""
    old_item = datetime.now(timezone.utc) - timedelta(minutes=200)
    store.upsert("source_state", {
        "source_id": HEARTBEAT_SOURCE_ID,
        "last_success_ts": iso_utc(old_item),
        "last_item_ts": iso_utc(old_item),
    })
    write_heartbeat(store, items_seen=0)
    row = store.get("source_state", (HEARTBEAT_SOURCE_ID,))
    # last_item_ts unchanged
    assert row["last_item_ts"] == iso_utc(old_item)
    # last_success_ts bumped to now (within a second of now)
    assert row["last_success_ts"] != iso_utc(old_item)


def test_write_heartbeat_bumps_last_item_when_nonzero(store):
    """An iteration that sees items DOES bump last_item_ts (resolves any
    open no-items warning the next time watchdog runs)."""
    old = datetime.now(timezone.utc) - timedelta(minutes=200)
    store.upsert("source_state", {
        "source_id": HEARTBEAT_SOURCE_ID,
        "last_success_ts": iso_utc(old),
        "last_item_ts": iso_utc(old),
    })
    write_heartbeat(store, items_seen=7)
    row = store.get("source_state", (HEARTBEAT_SOURCE_ID,))
    assert row["last_item_ts"] != iso_utc(old)
    assert row["items_last_hour"] == "7"


# ---------------- FF scraper health tracking ----------------


def test_ff_scraper_record_resets_streak_on_success(store):
    """A successful scrape (items_count > 0) zeroes consecutive_errors
    and bumps last_success_ts. After 2 failures, a success row clears."""
    from src.ff_scraper import FF_SCRAPER_SOURCE_ID, record_scrape_result
    record_scrape_result(store, 0)
    record_scrape_result(store, 0)
    row = store.get("source_state", (FF_SCRAPER_SOURCE_ID,))
    assert row["consecutive_errors"] == "2"
    # Successful scrape — streak resets
    record_scrape_result(store, 17)
    row = store.get("source_state", (FF_SCRAPER_SOURCE_ID,))
    assert row["consecutive_errors"] == "0"
    assert row["items_last_hour"] == "17"


def test_ff_scraper_record_increments_streak_on_empty(store):
    from src.ff_scraper import FF_SCRAPER_SOURCE_ID, record_scrape_result
    for _ in range(3):
        record_scrape_result(store, 0)
    row = store.get("source_state", (FF_SCRAPER_SOURCE_ID,))
    assert row["consecutive_errors"] == "3"


def test_watchdog_flags_ff_scraper_dead_at_3_consecutive_empties(store):
    """3 consecutive 0-event scrapes → ff_scraper_dead warning."""
    from src.ff_scraper import record_scrape_result
    for _ in range(3):
        record_scrape_result(store, 0)
    warns = check_pipeline_health(store)
    types = [wt for wt, _ in warns]
    assert "ff_scraper_dead" in types


def test_watchdog_no_ff_warning_below_threshold(store):
    """Only 2 empties → no warning yet (threshold is 3)."""
    from src.ff_scraper import record_scrape_result
    for _ in range(2):
        record_scrape_result(store, 0)
    # Heartbeat row also needed so the check doesn't short-circuit on
    # 'no heartbeat ever' silence warning.
    from src.health import write_heartbeat
    write_heartbeat(store, items_seen=5)
    warns = check_pipeline_health(store)
    types = [wt for wt, _ in warns]
    assert "ff_scraper_dead" not in types


def test_watchdog_flags_classifier_degraded_at_high_fallback(store):
    """Claude key invalid → all calls fall through. Watchdog should
    raise classifier_degraded when fallback ratio crosses threshold."""
    from src.news_alert import _record_classifier_outcome, MarketAlert
    fallback_alert = MarketAlert(action="keep", headline_th="x",
                                  reason="claude-unavailable fallback")
    # 25 fallback calls, 25 normal — 50/100 = 50% > 30% threshold
    keep_alert = MarketAlert(action="keep", headline_th="y")
    for _ in range(15):
        _record_classifier_outcome(store, "forexlive", fallback_alert,
                                    used_fallback=True, cache_hit=False)
    for _ in range(15):
        _record_classifier_outcome(store, "forexlive", keep_alert,
                                    used_fallback=False, cache_hit=False)
    # Need a fresh heartbeat so silence doesn't fire
    from src.health import write_heartbeat
    write_heartbeat(store, items_seen=5)
    warns = check_pipeline_health(store)
    types = [wt for wt, _ in warns]
    assert "classifier_degraded" in types


def test_watchdog_no_classifier_warning_below_sample_size(store):
    """Don't fire when there aren't enough samples — random noise
    shouldn't trigger an alert. classifier_min_samples default is 20."""
    from src.news_alert import _record_classifier_outcome, MarketAlert
    fallback_alert = MarketAlert(action="keep", headline_th="x")
    # 5 fallbacks out of 5 — 100% but under min_samples=20
    for _ in range(5):
        _record_classifier_outcome(store, "forexlive", fallback_alert,
                                    used_fallback=True, cache_hit=False)
    from src.health import write_heartbeat
    write_heartbeat(store, items_seen=5)
    warns = check_pipeline_health(store)
    types = [wt for wt, _ in warns]
    assert "classifier_degraded" not in types


def test_watchdog_flags_noisy_source_at_high_reject_rate(store):
    """A source that's 95% reject for the last 50+ items is noise —
    raise a per-source warning suggesting disabling."""
    from src.news_alert import _record_classifier_outcome, MarketAlert
    rejected = MarketAlert(action="reject", reason="evergreen")
    kept = MarketAlert(action="keep", headline_th="x")
    # 55 from yahoo: 53 reject, 2 keep → 96% reject rate
    for _ in range(53):
        _record_classifier_outcome(store, "yahoo_finance", rejected,
                                    used_fallback=False, cache_hit=False)
    for _ in range(2):
        _record_classifier_outcome(store, "yahoo_finance", kept,
                                    used_fallback=False, cache_hit=False)
    from src.health import write_heartbeat
    write_heartbeat(store, items_seen=5)
    warns = check_pipeline_health(store)
    types = [wt for wt, _ in warns]
    noisy_warnings = [t for t in types if t.startswith("source_noisy:")]
    assert any("yahoo_finance" in t for t in noisy_warnings)


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
