"""Store.upsert dirty tracking when callers mutate the row get() handed out.

Regression: get() returns the live dict. record_line_outcome (and the classifier
token counter) mutate it in place and upsert it back — the no-op check then
compared the row against itself, so the tab was never marked dirty and the
change never reached the sheet in modes that don't dirty source_state otherwise.
"""
from __future__ import annotations

from src import store as store_mod
from src.line_client import LINE_PUSH_SOURCE_ID, record_line_outcome


def _loaded_store(rows: dict[str, list[dict]]):
    """A Store as load_all would leave it (data + clean snapshot, nothing dirty)."""
    s = store_mod.Store(sheet_id="x", creds_json="{}")
    for tab in store_mod.SCHEMAS:
        buf = {}
        for r in rows.get(tab, []):
            full = {c: r.get(c, "") for c in store_mod.SCHEMAS[tab]}
            buf[store_mod._row_key(tab, full)] = full
        s.data[tab] = buf
        s._clean[tab] = {k: dict(v) for k, v in buf.items()}
        s.dirty[tab] = set()
    return s


def test_in_place_mutation_then_upsert_marks_dirty():
    s = _loaded_store({"source_state": [{"source_id": "a", "consecutive_errors": "0"}]})
    row = s.get("source_state", ("a",))
    row["consecutive_errors"] = 3          # mutate the live dict
    s.upsert("source_state", row)
    assert s.dirty["source_state"] == {"a"}
    assert s.get("source_state", ("a",))["consecutive_errors"] == 3


def test_identical_upsert_is_still_a_noop():
    s = _loaded_store({"source_state": [{"source_id": "a", "consecutive_errors": "0"}]})
    s.upsert("source_state", {"source_id": "a", "consecutive_errors": "0"})
    assert s.dirty["source_state"] == set()


def test_second_identical_upsert_after_change_does_not_redirty_needlessly():
    s = _loaded_store({"source_state": [{"source_id": "a", "consecutive_errors": "0"}]})
    s.upsert("source_state", {"source_id": "a", "consecutive_errors": "1"})
    s.dirty["source_state"] = set()        # pretend flushed
    s.upsert("source_state", {"source_id": "a", "consecutive_errors": "1"})
    assert s.dirty["source_state"] == set()


def test_record_line_outcome_failure_persists():
    s = _loaded_store({"source_state": [{"source_id": LINE_PUSH_SOURCE_ID,
                                         "consecutive_errors": "0"}]})
    record_line_outcome(s, {"status": 500})
    assert LINE_PUSH_SOURCE_ID in s.dirty["source_state"]
    assert int(s.get("source_state", (LINE_PUSH_SOURCE_ID,))["consecutive_errors"]) == 1
