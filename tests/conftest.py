"""Shared fixtures."""
from __future__ import annotations

import sys
from pathlib import Path

# Allow `from src.xxx import ...` in tests when pytest is run from repo root.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


import pytest  # noqa: E402

from src.store import SCHEMAS  # noqa: E402


class FakeStore:
    """In-memory drop-in mimicking Store interface needed by routing/health/digest."""

    def __init__(self) -> None:
        self.data: dict[str, dict[str, dict]] = {tab: {} for tab in SCHEMAS}
        self.dirty: dict[str, set] = {tab: set() for tab in SCHEMAS}
        self.api_calls = 0

    # mirror Store.upsert/get/all_rows
    def _row_key(self, tab: str, row: dict) -> str:
        from src.store import PRIMARY_KEYS
        return "|".join(str(row.get(k, "")) for k in PRIMARY_KEYS[tab])

    def get(self, tab, key_values):
        rk = "|".join(str(v) for v in key_values)
        return self.data.get(tab, {}).get(rk)

    def upsert(self, tab, row):
        from src.utils_time import iso_utc, now_utc
        row = {c: row.get(c, "") for c in SCHEMAS[tab]}
        row["updated_at"] = iso_utc(now_utc())
        rk = self._row_key(tab, row)
        self.data[tab][rk] = row
        self.dirty[tab].add(rk)
        return row

    def all_rows(self, tab):
        return list(self.data.get(tab, {}).values())

    def flush(self):
        pass

    def purge_older_than(self, tab, days, ts_col="updated_at"):
        from datetime import timedelta
        from src.utils_time import now_utc, parse_iso
        cutoff = now_utc() - timedelta(days=days)
        kept = {}
        removed = 0
        for rk, row in self.data.get(tab, {}).items():
            ts = parse_iso(row.get(ts_col))
            if ts is None or ts >= cutoff:
                kept[rk] = row
            else:
                removed += 1
        if removed:
            self.data[tab] = kept
            self.dirty.setdefault(tab, set()).add("__purge__")
        return removed


@pytest.fixture
def store():
    return FakeStore()


@pytest.fixture
def kw_config():
    import yaml
    with (ROOT / "config" / "keywords.yaml").open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)
