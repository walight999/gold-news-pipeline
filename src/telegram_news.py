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
    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        endpoint = f"{self.base_url}/webhook/news/{self.secret}"
        with httpx.Client(timeout=15.0) as c:
            r = c.post(endpoint, json=payload)
        if r.status_code >= 500 or r.status_code == 429:
            raise httpx.HTTPStatusError(f"news-bot {r.status_code}", request=r.request, response=r)
        return {"status": r.status_code, "body": r.text}

    def post(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._post(payload)
        except RetryError as e:
            log.warning("telegram news push failed after retries: %s", e)
            return {"status": 0, "body": "retry_exhausted"}
        except httpx.HTTPError as e:
            log.warning("telegram news push http error: %s", e)
            return {"status": 0, "body": str(e)}
