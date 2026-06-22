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
import math
import os
from dataclasses import dataclass, field
from typing import Any

import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError
from tenacity import (
    RetryError,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from .utils_time import iso_utc, now_utc

log = logging.getLogger(__name__)


# ---------------- gspread transient-error retry ----------------
#
# Google Sheets API regularly returns 503/500/429 under load. Captured live
# in run 26243926577 (2026-05-22) — calendar_daily failed entirely because
# of a single 503 on open_by_key. Wrap every API entrypoint in tenacity
# retry so we ride out the dip instead of failing the workflow.

def _is_transient_sheets_error(exc: BaseException) -> bool:
    """True for gspread APIErrors worth retrying. Matches on the [STATUS]
    prefix gspread prepends to the error message (works across SDK
    versions without relying on internal attribute layout)."""
    if not isinstance(exc, APIError):
        return False
    s = str(exc)
    return any(f"[{c}]" in s for c in (429, 500, 502, 503, 504))


def _retry(fn):
    """Decorator: 4 attempts, exponential backoff 2-10s, only on transient
    Sheets errors. Other exceptions bubble immediately."""
    return retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception(_is_transient_sheets_error),
        reraise=True,
    )(fn)


@_retry
def _open_by_key(gc: gspread.Client, sheet_id: str) -> gspread.Spreadsheet:
    return gc.open_by_key(sheet_id)


@_retry
def _ws_worksheet(sh: gspread.Spreadsheet, name: str) -> gspread.Worksheet:
    return sh.worksheet(name)


@_retry
def _ws_add(sh: gspread.Spreadsheet, name: str, rows: int, cols: int) -> gspread.Worksheet:
    return sh.add_worksheet(title=name, rows=rows, cols=cols)


@_retry
def _ws_get_all_records(ws: gspread.Worksheet, expected_headers: list[str]) -> list[dict]:
    # numericise_ignore=['all'] keeps EVERY cell a string. Without it gspread
    # auto-converts numeric-looking text — and a sha256 event_id whose first 20
    # hex chars parse as scientific notation (e.g. "1e234567890123456789")
    # becomes float('inf'), which then crashes flush() with InvalidJSONError.
    # Genuinely numeric columns (score, source_count, hits) are already cast
    # with int()/float() at their read sites, so reading them as strings is safe.
    return ws.get_all_records(expected_headers=expected_headers,
                              numericise_ignore=["all"])


@_retry
def _ws_row_values(ws: gspread.Worksheet, n: int) -> list[str]:
    return ws.row_values(n)


@_retry
def _ws_update(ws: gspread.Worksheet, range_: str, values, **kwargs) -> Any:
    return ws.update(range_, values, **kwargs)


@_retry
def _ws_clear(ws: gspread.Worksheet) -> Any:
    return ws.clear()

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
        "score", "status",
        # title/summary/url added 2026-06-22 so the 6-window digest can scan
        # the full 4h window from event_state and render each event as its own
        # full-detail card WITHOUT re-fetching the source. Additive migration:
        # _ensure_tab rewrites the header before load_all reads, so existing
        # rows just get blank cells for the new columns.
        "title", "summary", "url",
        "updated_at",
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
    "translation_cache": [
        # cache_key = first 16 chars of SHA-256(source_text). source_text
        # truncated to 80 chars purely for human inspection; full lookup
        # uses cache_key. Maintain mode caps to 2000 rows + 24h TTL.
        "cache_key", "source_preview", "thai_text", "hits", "created_at", "updated_at",
    ],
}

# Primary keys per tab — drive upsert behaviour.
PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "source_state": ("source_id",),
    "event_state": ("event_id",),
    "sent_log": ("event_id", "route_type"),
    "calibration_log": ("event_id",),
    "health_log": ("source_id", "warning_type", "warning_ts"),
    "translation_cache": ("cache_key",),
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
        self._sh = _open_by_key(self._gc, self.sheet_id)
        self.api_calls += 1

    def _ensure_tab(self, name: str) -> gspread.Worksheet:
        assert self._sh is not None
        try:
            ws = _ws_worksheet(self._sh, name)
        except gspread.WorksheetNotFound:
            ws = _ws_add(self._sh, name, rows=1000,
                         cols=max(20, len(SCHEMAS[name]) + 2))
            _ws_update(ws, "A1", [SCHEMAS[name]])
            self.api_calls += 2
            return ws
        self.api_calls += 1
        # Ensure header row present and matches schema.
        header = _ws_row_values(ws, 1)
        self.api_calls += 1
        if header != SCHEMAS[name]:
            _ws_update(ws, "A1", [SCHEMAS[name]])
            self.api_calls += 1
        return ws

    def load_all(self) -> None:
        """One read per tab. Populates self.data."""
        for tab, cols in SCHEMAS.items():
            ws = self._ensure_tab(tab)
            records = _ws_get_all_records(ws, cols)
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

    def append_feed(self, tab: str, headers: list[str], rows: list[list[Any]]) -> None:
        """Append-only writer for externally-consumed feeds (e.g. social_feed).

        Unlike flush(), this NEVER clears the sheet — rows accumulate at the
        bottom so an external automation's "watch new rows" stays stable and a
        column it owns (e.g. `posted`) is never clobbered. The tab is NOT part
        of SCHEMAS and is not loaded into in-memory state.
        """
        if not self._sh or not rows:
            return
        try:
            ws = _ws_worksheet(self._sh, tab)
            self.api_calls += 1
        except gspread.WorksheetNotFound:
            ws = _ws_add(self._sh, tab, rows=2000, cols=max(20, len(headers) + 2))
            _ws_update(ws, "A1", [headers])
            self.api_calls += 2
        ws.append_rows([[_cell(c) for c in r] for r in rows],
                       value_input_option="RAW")
        self.api_calls += 1
        log.info("store.append_feed tab=%s appended=%d", tab, len(rows))

    def read_feed(self, tab: str) -> tuple[list[str], list[dict[str, Any]]]:
        """Read an append-only feed tab (e.g. social_feed) verbatim. Returns
        (headers, rows) where each row dict also carries `_row` = its 1-based
        sheet row number (for targeted cell updates). Empty on missing tab."""
        if not self._sh:
            return [], []
        try:
            ws = _ws_worksheet(self._sh, tab)
        except gspread.WorksheetNotFound:
            return [], []
        self.api_calls += 1
        values = ws.get_all_values()
        self.api_calls += 1
        if not values:
            return [], []
        headers = values[0]
        rows: list[dict[str, Any]] = []
        for i, r in enumerate(values[1:], start=2):
            d: dict[str, Any] = {headers[j]: (r[j] if j < len(r) else "") for j in range(len(headers))}
            d["_row"] = i
            rows.append(d)
        return headers, rows

    def set_feed_cell(self, tab: str, row: int, col_index_1based: int, value: Any) -> None:
        """Write a single cell in a feed tab (targeted, non-clobbering)."""
        if not self._sh:
            return
        ws = _ws_worksheet(self._sh, tab)
        self.api_calls += 1
        ws.update_cell(row, col_index_1based, _cell(value))
        self.api_calls += 1

    def flush(self) -> None:
        """Write back only dirty rows. One batch write per tab."""
        if not self._sh:
            log.warning("store.flush called without connect() — skipping")
            return
        for tab, dirty_keys in self.dirty.items():
            if not dirty_keys:
                continue
            ws = _ws_worksheet(self._sh, tab)
            self.api_calls += 1
            all_rows = list(self.data[tab].values())
            cols = SCHEMAS[tab]
            # Diagnostic — surface which column carried a non-finite float so the
            # upstream source can be fixed (the value itself is coerced by _cell).
            for r in all_rows:
                for c in cols:
                    val = r.get(c)
                    if isinstance(val, float) and not math.isfinite(val):
                        log.warning("store.flush non-finite float in tab=%s col=%s key=%s value=%r",
                                    tab, c, _row_key(tab, r), val)
            values = [cols] + [[_cell(r.get(c, "")) for c in cols] for r in all_rows]
            _ws_clear(ws)
            self.api_calls += 1
            _ws_update(ws, "A1", values, value_input_option="RAW")
            self.api_calls += 1
            log.info("store.flush tab=%s rows=%d dirty=%d", tab, len(all_rows), len(dirty_keys))
            self.dirty[tab] = set()


def _cell(v: Any) -> Any:
    if v is None:
        return ""
    # NaN / Infinity are not JSON-compliant — gspread's writer raises
    # InvalidJSONError("Out of range float values") and crashes the whole
    # flush (taking down the run). Coerce to "" so one bad float can never
    # nuke a state write. (Source is logged in flush via _scan_nonfinite.)
    if isinstance(v, float) and not math.isfinite(v):
        return ""
    if isinstance(v, (list, tuple)):
        return ",".join(str(x) for x in v)
    return v
