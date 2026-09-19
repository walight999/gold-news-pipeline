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


def test_multi_recipient_resp_counts_per_recipient(store):
    """A broadcast to 1:1 + group that both succeed consumes 2 of the 500/mo
    quota, not 1 — the old +1-per-call undercount fired the 80% alarm too late."""
    resp = {"status": 200, "body": "multi:2/2_ok", "results": [
        {"to": "U1", "status": 200}, {"to": "C2", "status": 200}]}
    record_line_outcome(store, resp)
    assert get_line_quota_status(store)["count"] == 2


def test_multi_recipient_partial_counts_delivered_and_stays_alive(store):
    """1:1 delivered, group 429'd: bill the one that landed AND keep the channel
    marked alive (partial success must not trip the push-failing streak)."""
    resp = {"status": 429, "body": "multi:1/2_ok", "results": [
        {"to": "U1", "status": 200}, {"to": "C2", "status": 429}]}
    record_line_outcome(store, resp)
    assert get_line_quota_status(store)["count"] == 1
    row = store.get("source_state", (LINE_PUSH_SOURCE_ID,))
    assert int(row["consecutive_errors"]) == 0


def test_full_multi_failure_increments_streak(store):
    """Both recipients failed → nothing billed, streak advances."""
    resp = {"status": 500, "body": "multi:0/2_ok", "results": [
        {"to": "U1", "status": 500}, {"to": "C2", "status": 500}]}
    record_line_outcome(store, resp)
    assert get_line_quota_status(store)["count"] == 0
    row = store.get("source_state", (LINE_PUSH_SOURCE_ID,))
    assert int(row["consecutive_errors"]) == 1


def test_line_push_failing_message_names_quota_on_429(store):
    """A 429 streak must say 'quota exhausted, resets on the 1st' — NOT the
    generic 'token expired' — so the operator knows it self-heals and doesn't
    go chasing a dead token. This is the 2026-07-18 incident's signal."""
    from src.health import check_pipeline_health, write_heartbeat
    write_heartbeat(store, items_seen=5)
    for _ in range(5):
        record_line_outcome(store, 429)
    warns = dict(check_pipeline_health(store))
    assert "line_push_failing" in warns
    assert "429" in warns["line_push_failing"]
    assert "quota" in warns["line_push_failing"].lower()


def test_line_push_failing_message_names_auth_on_401(store):
    """A 401 streak = token/channel problem — a human must act; message must
    NOT claim quota (which would wrongly imply it self-heals on the 1st)."""
    from src.health import check_pipeline_health, write_heartbeat
    write_heartbeat(store, items_seen=5)
    for _ in range(5):
        record_line_outcome(store, 401)
    warns = dict(check_pipeline_health(store))
    assert "line_push_failing" in warns
    assert "401" in warns["line_push_failing"]
    assert "quota" not in warns["line_push_failing"].lower()


def test_quota_allows_fail_open_when_no_store():
    from src.line_client import PRIORITY_REDUNDANT, quota_allows
    assert quota_allows(None, PRIORITY_REDUNDANT)[0] is True


def test_quota_allows_normal_sends_every_priority(store):
    from src.line_client import (
        PRIORITY_BRIEFING, PRIORITY_CORE, PRIORITY_CRITICAL, PRIORITY_REDUNDANT,
        quota_allows,
    )
    record_line_outcome(store, 200)  # last_status=200, pct ~0
    for p in (PRIORITY_CRITICAL, PRIORITY_CORE, PRIORITY_BRIEFING, PRIORITY_REDUNDANT):
        assert quota_allows(store, p)[0] is True, p


def test_quota_hard_429_sheds_all_but_breaking_alert(store):
    """A 429 this month = exhausted: shed CORE/BRIEFING/REDUNDANT, but keep
    breaking/alert (priority 0) as the recovery probe + highest value."""
    from src.line_client import (
        PRIORITY_CORE, PRIORITY_CRITICAL, PRIORITY_REDUNDANT, quota_allows,
    )
    record_line_outcome(store, 429)  # stamps last_status=429, month=current ICT
    assert quota_allows(store, PRIORITY_CRITICAL)[0] is True
    ok_core, reason = quota_allows(store, PRIORITY_CORE)
    assert ok_core is False and "429" in reason
    assert quota_allows(store, PRIORITY_REDUNDANT)[0] is False


def test_quota_429_from_previous_month_does_not_gate(store):
    """The free tier resets on the 1st — a July 429 must not gate August."""
    import json
    from src.line_client import LINE_PUSH_SOURCE_ID, PRIORITY_CORE, quota_allows
    store.upsert("source_state", {
        "source_id": LINE_PUSH_SOURCE_ID,
        "last_status": "429",
        "items_last_hour": json.dumps({"month": "2020-01", "count": 0}),
    })
    assert quota_allows(store, PRIORITY_CORE)[0] is True


def test_quota_soft_gate_sheds_redundant_at_80pct(store):
    from src.line_client import (
        PRIORITY_BRIEFING, PRIORITY_CORE, PRIORITY_REDUNDANT, quota_allows,
    )
    for _ in range(400):  # 80% of 500
        record_line_outcome(store, 200)
    assert quota_allows(store, PRIORITY_REDUNDANT)[0] is False   # T-15 shed
    assert quota_allows(store, PRIORITY_BRIEFING)[0] is True     # not yet
    assert quota_allows(store, PRIORITY_CORE)[0] is True


def test_quota_soft_gate_sheds_briefing_at_90pct_core_protected(store):
    from src.line_client import PRIORITY_BRIEFING, PRIORITY_CORE, quota_allows
    for _ in range(450):  # 90% of 500
        record_line_outcome(store, 200)
    assert quota_allows(store, PRIORITY_BRIEFING)[0] is False
    assert quota_allows(store, PRIORITY_CORE)[0] is True         # core stays


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


# --- LINE-API-authoritative quota (fixes the 708/500=141% false alarm) --------

def _seed_quota_blob(store, **counters):
    import json
    store.upsert("source_state", {
        "source_id": LINE_PUSH_SOURCE_ID,
        "items_last_hour": json.dumps(counters),
    })


def test_quota_prefers_line_api_when_fresh(store):
    """The real bug: local estimate 708 vs cap 500 = 141% (false alarm) while
    LINE's own API says 23962/35000 = 68%. A fresh API reading must win."""
    from src.utils_time import iso_utc, now_utc
    _seed_quota_blob(store, month="2026-09", count=708,
                     api_limit=35000, api_usage=23962, api_ts=iso_utc(now_utc()))
    qs = get_line_quota_status(store)
    assert qs["source"] == "api"
    assert qs["count"] == 23962
    assert qs["limit"] == 35000
    assert qs["pct"] == 68            # NOT 141


def test_quota_falls_back_to_local_when_api_stale(store):
    from datetime import timedelta

    from src.utils_time import iso_utc, now_utc
    _seed_quota_blob(store, month="2026-09", count=708,
                     api_limit=35000, api_usage=23962,
                     api_ts=iso_utc(now_utc() - timedelta(hours=12)))
    qs = get_line_quota_status(store)
    assert qs["source"] == "local"
    assert qs["count"] == 708
    assert qs["limit"] == LINE_FREE_TIER_QUOTA


def test_quota_unlimited_plan_never_alarms(store):
    """type 'none' → api_limit 0 → unlimited → pct 0, never fires line_quota_high."""
    from src.utils_time import iso_utc, now_utc
    _seed_quota_blob(store, month="2026-09", count=5,
                     api_limit=0, api_usage=99999, api_ts=iso_utc(now_utc()))
    assert get_line_quota_status(store)["pct"] == 0


def test_refresh_quota_is_noop_without_token(store):
    """Best-effort: no token → no crash, no write."""
    from src.line_client import refresh_line_quota_from_api
    refresh_line_quota_from_api(store, "")
    assert store.get("source_state", (LINE_PUSH_SOURCE_ID,)) is None
