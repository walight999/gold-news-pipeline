"""first_seen_ts must carry forward across runs (was reset every cron run)."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from src import store as store_mod
from src.main import _persisted_first_seen
from src.utils_time import iso_utc, now_utc


def _store(rows):
    s = store_mod.Store(sheet_id="x", creds_json="{}")
    for tab in store_mod.SCHEMAS:
        s.data[tab] = {}
    for tab, r in rows:
        s.data[tab][store_mod._row_key(tab, r)] = r
    return s


def test_earlier_event_state_first_seen_wins():
    now = now_utc()
    earlier = now - timedelta(hours=3)
    s = _store([("event_state", {"event_id": "e1", "first_seen_ts": iso_utc(earlier)})])
    ev = SimpleNamespace(event_id="e1", first_seen_ts=now)
    assert abs((_persisted_first_seen(s, ev) - earlier).total_seconds()) < 1


def test_calibration_log_first_seen_used_when_earliest():
    now = now_utc()
    a = now - timedelta(hours=1)
    b = now - timedelta(hours=5)
    s = _store([("event_state", {"event_id": "e1", "first_seen_ts": iso_utc(a)}),
                ("calibration_log", {"event_id": "e1", "first_seen_ts": iso_utc(b)})])
    got = _persisted_first_seen(s, SimpleNamespace(event_id="e1", first_seen_ts=now))
    assert abs((got - b).total_seconds()) < 1


def test_new_event_uses_run_anchor():
    now = now_utc()
    s = _store([])
    assert _persisted_first_seen(s, SimpleNamespace(event_id="new", first_seen_ts=now)) == now


def test_unparseable_stored_value_ignored():
    now = now_utc()
    s = _store([("event_state", {"event_id": "e1", "first_seen_ts": "garbage"})])
    assert _persisted_first_seen(s, SimpleNamespace(event_id="e1", first_seen_ts=now)) == now
