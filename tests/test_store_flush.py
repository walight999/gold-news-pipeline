"""Store.flush ordering — the idempotency ledger (sent_log) must persist before
the content tabs it guards, so a mid-flush Sheets error can't cause a re-send."""
from __future__ import annotations

from src import store as store_mod


class _WS:
    def __init__(self, name: str):
        self.name = name
        self.row_count = 1


def _patch_sheet(monkeypatch, order: list[str]):
    monkeypatch.setattr(store_mod, "_ws_worksheet", lambda sh, name: _WS(name))
    monkeypatch.setattr(store_mod, "_ws_update",
                        lambda ws, rng, values, **kw: order.append(ws.name))
    monkeypatch.setattr(store_mod, "_ws_batch_clear", lambda ws, ranges: None)


def _make_store():
    s = store_mod.Store(sheet_id="x", creds_json="{}")
    s._sh = object()  # truthy so flush() proceeds
    s.data = {t: {} for t in store_mod.SCHEMAS}
    s.dirty = {}       # controlled insertion order below
    return s


def test_flush_writes_sent_log_before_event_state(monkeypatch):
    order: list[str] = []
    _patch_sheet(monkeypatch, order)
    s = _make_store()
    # Insertion order puts event_state FIRST — the natural (buggy) flush order.
    s.data["event_state"]["e1"] = {"event_id": "e1"}
    s.dirty["event_state"] = {"e1"}
    s.data["sent_log"]["e1|breaking"] = {"event_id": "e1", "route_type": "breaking"}
    s.dirty["sent_log"] = {"e1|breaking"}

    s.flush()

    assert "sent_log" in order and "event_state" in order
    assert order.index("sent_log") < order.index("event_state")


def test_flush_skips_clean_tabs_and_still_prioritizes_sent_log(monkeypatch):
    order: list[str] = []
    _patch_sheet(monkeypatch, order)
    s = _make_store()
    # calibration_log dirty first, then sent_log; source_state clean (skipped).
    s.data["calibration_log"]["e1"] = {"event_id": "e1"}
    s.dirty["calibration_log"] = {"e1"}
    s.data["sent_log"]["e1|alert"] = {"event_id": "e1", "route_type": "alert"}
    s.dirty["sent_log"] = {"e1|alert"}
    s.dirty["source_state"] = set()  # clean

    s.flush()

    assert order[0] == "sent_log"
    assert "source_state" not in order  # clean tab never written


def test_flush_noop_when_nothing_dirty(monkeypatch):
    order: list[str] = []
    _patch_sheet(monkeypatch, order)
    s = _make_store()
    s.dirty = {t: set() for t in store_mod.SCHEMAS}
    s.flush()
    assert order == []
