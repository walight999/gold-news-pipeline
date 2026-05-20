"""LINE Messaging API push with retry/backoff.

Idempotency contract:
  - Caller checks sent_log BEFORE invoking push.
  - On failure (after retries) we DO NOT mark as sent — so the next run retries.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import RetryError, retry, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)

_PUSH_URL = "https://api.line.me/v2/bot/message/push"


@dataclass
class LineClient:
    token: str

    @classmethod
    def from_env(cls) -> "LineClient":
        return cls(token=os.environ["LINE_CHANNEL_TOKEN"])

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10),
           reraise=True)
    def _post(self, target: str, text: str) -> dict[str, Any]:
        payload = {"to": target, "messages": [{"type": "text", "text": text[:4900]}]}
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        with httpx.Client(timeout=15.0) as c:
            r = c.post(_PUSH_URL, json=payload, headers=headers)
        if r.status_code >= 500 or r.status_code == 429:
            raise httpx.HTTPStatusError(f"LINE {r.status_code}", request=r.request, response=r)
        return {"status": r.status_code, "body": r.text}

    def push(self, target: str, text: str) -> dict[str, Any]:
        try:
            return self._post(target, text)
        except RetryError as e:
            log.warning("line push failed after retries: %s", e)
            return {"status": 0, "body": "retry_exhausted"}
        except httpx.HTTPError as e:
            log.warning("line push http error: %s", e)
            return {"status": 0, "body": str(e)}
