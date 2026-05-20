"""Google Sheets store.

Bound contract:
  1. Load every state tab once at run start.
  2. Process in memory.
  3. Batch-write only changed rows at run end.

No per-event Sheets calls. Tracking via row-keyed dicts; each row carries `updated_at`.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

import gspread
from google.oauth2.service_account import Credentials

from .utils_time import iso_utc, now_utc

log = logging.getLogger(__name__)

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

# Tab schemas. Order matters — written in this column order.
SCHEMAS: dict[str, list[str]] = {
    "source_state": [
        "source_id", "last_attempt_ts", "last_success_ts", "last_item_ts",
        "etag", "last_modified", "consecutive_errors", "items_last_hour",
        "last_validation_ts", "last_health_alert_ts", "updated_at",
    ],
    "event_state": [
        "event_id", "cluster_key", "topic_bucket", "entity", "direction_label",
        "first_seen_ts", "last_seen_ts", "source_list", "source_count",
        "score", "status", "updated_at",
    ],
    "sent_log": [
        "event_id", "route_type", "sent_ts", "line_status", "updated_at",
    ],
    "calibration_log": [
        "event_id", "first_seen_ts", "topic_bucket", "entity", "direction_label",
        "source_list", "source_count", "score", "routed_as",
        "xau_return_5m", "xau_return_15m", "xau_return_30m", "updated_at",
    ],
    "health_log": [
        "source_id", "warning_type", "warning_ts", "resolved_ts", "updated_at",
    ],
}

# Primary keys per tab — drive upsert behaviour.
PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "source_state": ("source_id",),
    "event_state": ("event_id",),
    "sent_log": ("event_id", "route_type"),
    "calibration_log": ("event_id",),
    "health_log": ("source_id", "warning_type", "warning_ts"),
}


def _row_key(tab: str, row: dict[str, Any]) -> str:
    return "|".join(str(row.get(k, "")) for k in PRIMARY_KEYS[tab])


@dataclass
class Store:
    sheet_id: str
    creds_json: str
    _gc: gspread.Client | None = None
    _sh: gspread.Spreadsheet | None = None
    # In-memory state. {tab: {row_key: row_dict}}
    data: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    # Tracks which row_keys were touched; only these get written back.
    dirty: dict[str, set[str]] = field(default_factory=dict)
    # Track API call count for acceptance criterion §6.8.
    api_calls: int = 0

    @classmethod
    def from_env(cls) -> "Store":
        sheet_id = os.environ["GSHEET_ID"]
        creds_json = os.environ["GSHEET_CREDS"]
        return cls(sheet_id=sheet_id, creds_json=creds_json)

    def connect(self) -> None:
        info = json.loads(self.creds_json)
        creds = Credentials.from_service_account_info(info, scopes=_SCOPES)
        self._gc = gspread.authorize(creds)
        self._sh = self._gc.open_by_key(self.sheet_id)
        self.api_calls += 1

    def _ensure_tab(self, name: str) -> gspread.Worksheet:
        assert self._sh is not None
        try:
            ws = self._sh.worksheet(name)
        except gspread.WorksheetNotFound:
            ws = self._sh.add_worksheet(title=name, rows=1000, cols=max(20, len(SCHEMAS[name]) + 2))
            ws.update("A1", [SCHEMAS[name]])
            self.api_calls += 2
            return ws
        self.api_calls += 1
        # Ensure header row present and matches schema.
        header = ws.row_values(1)
        self.api_calls += 1
        if header != SCHEMAS[name]:
            ws.update("A1", [SCHEMAS[name]])
            self.api_calls += 1
        return ws

    def load_all(self) -> None:
        """One read per tab. Populates self.data."""
        for tab, cols in SCHEMAS.items():
            ws = self._ensure_tab(tab)
            records = ws.get_all_records(expected_headers=cols)
            self.api_calls += 1
            buf: dict[str, dict[str, Any]] = {}
            for r in records:
                rk = _row_key(tab, r)
                if not rk.strip("|"):
                    continue
                buf[rk] = {c: r.get(c, "") for c in cols}
            self.data[tab] = buf
            self.dirty[tab] = set()
        log.info("store.load_all done: %s", {t: len(self.data[t]) for t in SCHEMAS})

    # ---------- in-memory upsert helpers ----------

    def get(self, tab: str, key_values: tuple[str, ...]) -> dict[str, Any] | None:
        rk = "|".join(str(v) for v in key_values)
        return self.data.get(tab, {}).get(rk)

    def upsert(self, tab: str, row: dict[str, Any]) -> dict[str, Any]:
        row = {c: row.get(c, "") for c in SCHEMAS[tab]}
        row["updated_at"] = iso_utc(now_utc())
        rk = _row_key(tab, row)
        self.data.setdefault(tab, {})[rk] = row
        self.dirty.setdefault(tab, set()).add(rk)
        return row

    def all_rows(self, tab: str) -> list[dict[str, Any]]:
        return list(self.data.get(tab, {}).values())

    # ---------- batch flush ----------

    def purge_older_than(self, tab: str, days: int, ts_col: str = "updated_at") -> int:
        """Drop rows from `tab` whose `ts_col` is older than `days` days.
        Returns the number of rows removed. Rows with missing/unparseable
        timestamps are kept (safe default — never delete on uncertainty)."""
        from datetime import timedelta
        from .utils_time import now_utc, parse_iso

        if tab not in self.data:
            return 0
        cutoff = now_utc() - timedelta(days=days)
        kept: dict[str, dict[str, Any]] = {}
        removed = 0
        for rk, row in self.data[tab].items():
            ts = parse_iso(row.get(ts_col))
            if ts is None or ts >= cutoff:
                kept[rk] = row
            else:
                removed += 1
        if removed:
            self.data[tab] = kept
            # flush() rewrites the whole tab if its dirty set is non-empty;
            # add a sentinel so the trimmed sheet gets written even when
            # no other upserts happened this run.
            self.dirty.setdefault(tab, set()).add("__purge__")
        return removed

    def flush(self) -> None:
        """Write back only dirty rows. One batch write per tab."""
        if not self._sh:
            log.warning("store.flush called without connect() — skipping")
            return
        for tab, dirty_keys in self.dirty.items():
            if not dirty_keys:
                continue
            ws = self._sh.worksheet(tab)
            self.api_calls += 1
            all_rows = list(self.data[tab].values())
            cols = SCHEMAS[tab]
            values = [cols] + [[_cell(r.get(c, "")) for c in cols] for r in all_rows]
            ws.clear()
            self.api_calls += 1
            ws.update("A1", values, value_input_option="RAW")
            self.api_calls += 1
            log.info("store.flush tab=%s rows=%d dirty=%d", tab, len(all_rows), len(dirty_keys))
            self.dirty[tab] = set()


def _cell(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return ",".join(str(x) for x in v)
    return v
