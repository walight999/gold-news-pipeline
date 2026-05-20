"""Async fetcher with ETag/Last-Modified, poll_min gating, and source-state updates.

Skips sources with `enabled: false`.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx

from .store import Store
from .utils_time import iso_utc, now_utc, parse_iso

log = logging.getLogger(__name__)


@dataclass
class FetchResult:
    source: dict[str, Any]
    status: int
    body: bytes | None
    error: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False


@dataclass
class FetchPlan:
    sources: list[dict[str, Any]]
    skipped_disabled: list[str] = field(default_factory=list)
    skipped_polled_recently: list[str] = field(default_factory=list)


def plan_fetch(sources: list[dict[str, Any]], store: Store, force: bool = False) -> FetchPlan:
    """Filter sources: enabled, and last_attempt older than poll_min."""
    plan = FetchPlan(sources=[])
    now = now_utc()
    for s in sources:
        sid = s["id"]
        if not s.get("enabled"):
            plan.skipped_disabled.append(sid)
            continue
        state = store.get("source_state", (sid,)) or {}
        last_attempt = parse_iso(state.get("last_attempt_ts"))
        poll_min = int(s.get("poll_min", 15))
        if not force and last_attempt and (now - last_attempt) < timedelta(minutes=poll_min - 1):
            # -1 minute slack so a 5-min cron doesn't slip past a 5-min poll.
            plan.skipped_polled_recently.append(sid)
            continue
        plan.sources.append(s)
    return plan


async def _fetch_one(client: httpx.AsyncClient, source: dict[str, Any], state: dict[str, Any]) -> FetchResult:
    headers: dict[str, str] = {"User-Agent": "gold-news-pipeline/1.0"}
    if state.get("etag"):
        headers["If-None-Match"] = str(state["etag"])
    if state.get("last_modified"):
        headers["If-Modified-Since"] = str(state["last_modified"])
    try:
        resp = await client.get(source["url"], headers=headers, timeout=20.0, follow_redirects=True)
    except (httpx.HTTPError, asyncio.TimeoutError) as e:
        return FetchResult(source=source, status=0, body=None, error=str(e))
    if resp.status_code == 304:
        return FetchResult(source=source, status=304, body=None, not_modified=True,
                           etag=resp.headers.get("ETag"), last_modified=resp.headers.get("Last-Modified"))
    if resp.status_code >= 400:
        return FetchResult(source=source, status=resp.status_code, body=None,
                           error=f"HTTP {resp.status_code}")
    return FetchResult(source=source, status=resp.status_code, body=resp.content,
                       etag=resp.headers.get("ETag"), last_modified=resp.headers.get("Last-Modified"))


async def fetch_all(plan: FetchPlan, store: Store) -> list[FetchResult]:
    results: list[FetchResult] = []
    if not plan.sources:
        return results
    limits = httpx.Limits(max_connections=10, max_keepalive_connections=5)
    async with httpx.AsyncClient(http2=True, limits=limits) as client:
        tasks = []
        for s in plan.sources:
            state = store.get("source_state", (s["id"],)) or {}
            tasks.append(_fetch_one(client, s, state))
        results = await asyncio.gather(*tasks)
    _update_source_state(plan, results, store)
    return results


def _update_source_state(plan: FetchPlan, results: list[FetchResult], store: Store) -> None:
    ts = iso_utc(now_utc())
    for r in results:
        sid = r.source["id"]
        prev = store.get("source_state", (sid,)) or {}
        consecutive_errors = int(prev.get("consecutive_errors") or 0)
        last_success_ts = prev.get("last_success_ts") or ""
        if r.error or r.status >= 400:
            consecutive_errors += 1
        elif r.not_modified or r.status == 200:
            consecutive_errors = 0
            last_success_ts = ts
        row = {
            "source_id": sid,
            "last_attempt_ts": ts,
            "last_success_ts": last_success_ts,
            "last_item_ts": prev.get("last_item_ts") or "",
            "etag": r.etag or prev.get("etag") or "",
            "last_modified": r.last_modified or prev.get("last_modified") or "",
            "consecutive_errors": consecutive_errors,
            "items_last_hour": prev.get("items_last_hour") or 0,
            "last_validation_ts": prev.get("last_validation_ts") or "",
            "last_health_alert_ts": prev.get("last_health_alert_ts") or "",
        }
        store.upsert("source_state", row)
