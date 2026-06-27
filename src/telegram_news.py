"""Push routed, translated news to the off-GAS CHUM News Bot (Cloudflare Worker).

The worker (chum-news-bot) renders its own Telegram message from these structured
fields and fans out to its D1 subscribers — the SAME MarketAlert data that feeds
LINE, so Telegram readers get the identical quality.

Design (mirrors line_client.py):
  - Best-effort + env-gated. If NEWS_BOT_WEBHOOK_URL / NEWS_BOT_SECRET are unset,
    from_env() returns None and callers skip silently — safe to ship before the
    worker is deployed, and never blocks the LINE path.
  - The worker dedups by (event_id, route), so a retry never double-sends. We do
    NOT add per-channel sent_log state here (the worker is the dedup backstop).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import RetryError, retry, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)


def build_payload(
    *,
    event_id: str,
    route: str,
    category: str | None,
    tone: str | None,
    impact_level: str | None,
    headline_th: str | None,
    body_th: list[str] | None,
    impact_th: str | None,
    source: str | None,
    url: str | None,
    ts: str | None = None,
) -> dict[str, Any]:
    """Map a routed MarketAlert + Event into the worker's /webhook/news contract."""
    return {
        "event_id": event_id,
        "route": route,
        "category": category,
        "tone": tone,
        "impact_level": impact_level,
        "headline_th": headline_th,
        "body_th": body_th or [],
        "impact_th": impact_th,
        "source": source,
        "url": url,
        "ts": ts,
    }


@dataclass
class TelegramNewsClient:
    base_url: str
    secret: str

    @classmethod
    def from_env(cls) -> "TelegramNewsClient | None":
        url = os.environ.get("NEWS_BOT_WEBHOOK_URL", "").strip()
        secret = os.environ.get("NEWS_BOT_SECRET", "").strip()
        if not url or not secret:
            return None
        return cls(base_url=url.rstrip("/"), secret=secret)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10),
           reraise=True)
    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        endpoint = f"{self.base_url}/{path}/{self.secret}"
        with httpx.Client(timeout=15.0) as c:
            r = c.post(endpoint, json=payload)
        if r.status_code >= 500 or r.status_code == 429:
            raise httpx.HTTPStatusError(f"news-bot {r.status_code}", request=r.request, response=r)
        return {"status": r.status_code, "body": r.text}

    def _send(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._post(path, payload)
        except RetryError as e:
            log.warning("telegram push failed after retries (%s): %s", path, e)
            return {"status": 0, "body": "retry_exhausted"}
        except httpx.HTTPError as e:
            log.warning("telegram push http error (%s): %s", path, e)
            return {"status": 0, "body": str(e)}

    def post(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._send("webhook/news", payload)

    def post_calendar(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._send("webhook/calendar", payload)

    def post_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._send("webhook/event", payload)


def build_event_payload(
    *,
    event_id: str,
    phase: str,  # "pre" | "post"
    ev,
    mins_to: int | None = None,
    actual: str | None = None,
    detail_th: str | None = None,
) -> dict[str, Any]:
    """Map a CalEvent (pre/post release) → the worker's /webhook/event contract.
    Asset-neutral: no XAU reaction/verdict — the worker derives beat/miss itself."""
    return {
        "event_id": event_id,
        "phase": phase,
        "country": getattr(ev, "country", "") or "",
        "title": getattr(ev, "title", "") or "",
        "impact": getattr(ev, "impact", "") or "",
        "mins_to": mins_to,
        "forecast": (getattr(ev, "forecast", "") or None),
        "previous": (getattr(ev, "previous", "") or None),
        "actual": (actual or None),
        "detail_th": (detail_th or None),
    }


def build_calendar_payload(event_id: str, date_label: str, events: list) -> dict[str, Any]:
    """Map filtered CalEvent objects → the worker's /webhook/calendar contract."""
    return {
        "event_id": event_id,
        "date_label": date_label,
        "events": [
            {
                "time": getattr(e, "hhmm_ict", "") or "",
                # UTC ISO timestamp → the worker localizes per-subscriber region
                # (global product; the "time"/hhmm_ict ICT string is the fallback).
                "ts": (e.dt_utc.isoformat() if getattr(e, "dt_utc", None) is not None else None),
                "country": getattr(e, "country", "") or "",
                "title": getattr(e, "title", "") or "",
                "impact": getattr(e, "impact", "") or "",
                "forecast": (getattr(e, "forecast", "") or None),
                "previous": (getattr(e, "previous", "") or None),
            }
            for e in events
        ],
    }
