"""News classification + Thai market-alert rewrite.

The OLD `translator.to_thai()` does a literal translation, which produced
two failure modes visible in production:

  1. Personal-finance / evergreen / opinion articles leak into Breaking
     and Digest because they share a keyword like "inflation" or "Fed",
     wasting Claude tokens and polluting the LINE channel with noise.
  2. Literal translation ("Bank of Japan" → "ธนาคารกลางญี่ปุ่น") sounds
     awkward to Thai traders who actually use "BoJ". Long article bodies
     come through as-is, overflowing the mobile card. Claude also
     occasionally hallucinates Buddhist Era year conversions (writing
     "2563" when the source says "2026").

`classify_and_rewrite()` replaces that pipeline with a single Claude
call that does BOTH steps and returns structured JSON:

  - action      → "keep" | "reject" (caller skips event entirely on reject)
  - news_type   → data_release / central_bank / geopolitics / ...
  - tone        → hawkish / dovish / risk_on / risk_off / neutral
  - category    → Inflation / Central Bank / Geopolitics / Energy / ...
  - headline_th → Thai headline, ≤ 90 chars (the bubble title)
  - body_th     → 1-3 short bullets, each ≤ 120 chars
  - impact_th   → 1-sentence XAU/USD impact summary
  - reason      → why it was rejected (when applicable)

Cached in the same `translation_cache` Sheet tab; rows are tagged with
an "alert:" cache_key prefix so they coexist with the legacy
`to_thai()` plain-text cache rows. Both share the 24h TTL.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .store import Store

log = logging.getLogger(__name__)


@dataclass
class MarketAlert:
    """Result of classify_and_rewrite. `should_send` is the only flag
    callers should check — the rest are display fields used by the LINE
    Flex builders when sending."""
    action: str = "reject"      # "keep" | "reject"
    news_type: str = "other"
    relevance_to_gold: str = "none"
    freshness: str = "unknown"
    tone: str = "neutral"
    category: str = "Other"
    headline_th: str | None = None
    body_th: list[str] = field(default_factory=list)
    impact_th: str | None = None
    reason: str = ""

    @property
    def should_send(self) -> bool:
        return self.action == "keep"

    def to_json(self) -> str:
        return json.dumps({
            "action": self.action,
            "news_type": self.news_type,
            "relevance_to_gold": self.relevance_to_gold,
            "freshness": self.freshness,
            "tone": self.tone,
            "category": self.category,
            "headline_th": self.headline_th,
            "body_th": self.body_th,
            "impact_th": self.impact_th,
            "reason": self.reason,
        }, ensure_ascii=False)

    @classmethod
    def from_json(cls, s: str) -> "MarketAlert | None":
        try:
            d = json.loads(s)
            if not isinstance(d, dict) or "action" not in d:
                return None
            return cls(
                action=d.get("action", "reject"),
                news_type=d.get("news_type", "other"),
                relevance_to_gold=d.get("relevance_to_gold", "none"),
                freshness=d.get("freshness", "unknown"),
                tone=d.get("tone", "neutral"),
                category=d.get("category", "Other"),
                headline_th=d.get("headline_th"),
                body_th=list(d.get("body_th") or [])[:3],
                impact_th=d.get("impact_th"),
                reason=str(d.get("reason") or ""),
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            return None


_REJECTED_NO_TITLE = MarketAlert(action="reject", reason="empty title")


def _has_cjk_in_alert(alert: MarketAlert) -> bool:
    """True when any user-facing field contains Chinese / Japanese /
    Korean script. Reused from translator's logic but applied across all
    rewrite output fields, not just a single string."""
    from .translator import _has_cjk
    for field in (alert.headline_th, alert.impact_th):
        if _has_cjk(field):
            return True
    for b in alert.body_th or []:
        if _has_cjk(b):
            return True
    return False


# --------------------------------------------------------------------- prompt
#
# Hand-tuned. Edit with care — every line addresses an observed failure
# from production output.

_SYSTEM_PROMPT = """You are a professional macro news editor for a real-time XAU/USD (gold) trading alert system used by Thai traders.

TWO steps in one response:
1. CLASSIFY the news item. Decide whether it is worth sending.
2. If keep, REWRITE it as a concise Thai market alert (NOT a literal translation).

REJECT the item if it is any of these:
- personal finance advice (savings tips, retirement planning, annuity, insurance)
- evergreen educational article (how-to, guides, explainers)
- lifestyle / wellness / health content
- opinion piece without a fresh market catalyst
- stock-specific article with no macro impact (e.g., single-stock Nvidia analysis with no broader signal)
- duplicate / rephrased article
- older than 48 hours
- only contains keywords like "inflation" / "Fed" / "gold" but no fresh event / data / policy signal
- low-impact with no XAU/USD/yields/rates/inflation/labor/geopolitics/oil/risk-sentiment relevance
- crypto / coin price predictions (unrelated to macro XAU)
- meta articles ("This article was written by...", site promos)

KEEP the item only if it reports:
- fresh economic data release (CPI, PCE, NFP, GDP, Retail Sales, ISM, PMI, Jobless Claims, Durable Goods, etc.)
- fresh central bank signal (Fed/ECB/BoJ/BoE/SNB/BoC speech, decision, minutes, dot plot)
- geopolitical escalation / de-escalation (war, sanctions, ceasefire, Hormuz, oil supply disruption)
- USD / yield / DXY breakout or sharp move
- material risk sentiment shift in equities or bonds with broader macro implications

OUTPUT — STRICT JSON only. No prose, no markdown fence, no explanation. Exactly these keys:
{
  "action": "keep" or "reject",
  "news_type": "data_release | central_bank | geopolitics | energy | rates_yields | fx | equity_macro | equity_specific | personal_finance | evergreen_article | opinion | duplicate | crypto | other",
  "relevance_to_gold": "high | medium | low | none",
  "freshness": "fresh | stale | unknown",
  "tone": "hawkish | dovish | risk_on | risk_off | neutral",
  "category": "Inflation | Central Bank | Geopolitics | Energy | Equity | Macro | Other",
  "headline_th": "...",
  "body_th": ["...", "..."],
  "impact_th": "...",
  "reason": "..."
}

When action="reject": headline_th=null, body_th=[], impact_th=null, and reason MUST contain a short explanation.

REWRITE RULES (only when action="keep"):
- headline_th ≤ 90 Thai characters. Direct, market-relevant, no clickbait.
- body_th: 1-3 bullets, each ≤ 120 chars. Facts and figures only. NO meta references.
- impact_th: ONE short sentence focused on XAU/USD/yields impact.
  If unclear: "ผลกระทบต่อทองคำยังไม่ชัดเจน".

GLOSSARY — PRESERVE these terms EXACTLY in English. Do NOT translate to Thai:
  CPI, Core CPI, PCE, Core PCE, PPI, NFP, GDP, PMI, ISM, FOMC,
  Fed, FOMC, ECB, BoJ, BoE, PBOC, SNB, BoC, RBA, RBNZ, IMF, OPEC,
  USD, EUR, JPY, GBP, CNY, AUD, CAD, CHF, NZD,
  DXY, RSI, MACD, S&P 500, Nasdaq, Dow,
  hawkish, dovish, risk-on, risk-off, yield, yields, soft landing, hard landing,
  safe-haven, dot plot, forward guidance

INSTITUTION ABBREVIATIONS — use these exact short forms:
- Bank of Japan → BoJ        (NEVER "ธนาคารกลางญี่ปุ่น")
- Federal Reserve → Fed      (NEVER "ธนาคารกลางสหรัฐ")
- European Central Bank → ECB
- Bank of England → BoE
- People's Bank of China → PBOC

NAMES — Thai-script transliteration:
ทรัมป์ / ไบเดน / แฮร์ริส / สี จิ้นผิง / หลี่ เฉียง / ปูติน / เซเลนสกี / เนทันยาฮู /
คิม จอง อึน / ยุน ซอกยอล / โมดี / พาวเวลล์ / ลาการ์ด / อูเอดะ / เบลีย์ /
แมคเลม / จอร์แดน / เยลเลน

DATES — CRITICAL RULE:
- ALWAYS use Gregorian year (2026, 2027, etc.).
- NEVER convert to Buddhist Era. NEVER write "2563", "2568", "2569", "พ.ศ.", "BE".
- If source mentions a year, copy it exactly. Do NOT do math on years.

CLASSIFICATION RULES:
- Higher inflation than expected → tone: hawkish
- Lower inflation than expected → tone: dovish
- Hawkish central bank rhetoric → tone: hawkish
- Dovish central bank rhetoric → tone: dovish
- War / supply disruption / sanctions escalation → tone: risk_off
- Ceasefire / de-escalation → tone: risk_on (gold often softens)
- Equity rally with macro context → tone: risk_on
- Equity selloff with macro context → tone: risk_off
- Pure single-stock article → action: reject

NO hallucinations. NO emojis. NO casual language. NO meta references.
If facts are missing, leave the impact_th vague — do NOT invent.

INPUT:
SOURCE_ID: {source_id}
SOURCE_AGE_HOURS: {age_hours}
TITLE: {title}
SUMMARY: {summary}

JSON output (strict, single object, no surrounding text):"""


def _cache_key_alert(title: str, summary: str) -> str:
    """Cache key for the structured alert format. The "alert:" prefix
    distinguishes structured-JSON cache rows from the legacy plain-text
    rows written by `translator.to_thai()` — both live in the same Sheet
    tab and share the 24h TTL."""
    h = hashlib.sha256(f"{title}\n{summary or ''}".encode("utf-8")).hexdigest()[:14]
    return f"al{h}"   # total 16 chars to fit existing cache_key column width


def _cache_lookup(store: "Store | None", key: str) -> MarketAlert | None:
    if store is None:
        return None
    row = store.get("translation_cache", (key,))
    if not row:
        return None
    blob = row.get("thai_text")
    if not blob:
        return None
    return MarketAlert.from_json(blob)


def _cache_write(store: "Store | None", key: str, src_title: str, alert: MarketAlert) -> None:
    if store is None:
        return
    from .utils_time import iso_utc, now_utc
    existing = store.get("translation_cache", (key,)) or {}
    hits = int(existing.get("hits") or 0) + 1
    created = existing.get("created_at") or iso_utc(now_utc())
    store.upsert("translation_cache", {
        "cache_key": key,
        "source_preview": src_title[:80],
        "thai_text": alert.to_json(),
        "hits": str(hits),
        "created_at": created,
    })


def classify_and_rewrite(
    title: str,
    summary: str,
    source_id: str = "",
    age_hours: float | None = None,
    store: "Store | None" = None,
) -> MarketAlert:
    """Returns a `MarketAlert`. Check `.should_send` to decide whether
    to publish. When `keep`, the returned object has headline_th /
    body_th / impact_th ready for the LINE Flex builders.

    Cache-first via the `translation_cache` Sheet tab. Falls back to a
    permissive accept (literal-translation) when Claude is unavailable
    so the pipeline still publishes during ANTHROPIC_API_KEY outages."""
    if not title:
        return _REJECTED_NO_TITLE

    key = _cache_key_alert(title, summary or "")

    cached = _cache_lookup(store, key)
    if cached is not None:
        _cache_write(store, key, title, cached)
        return cached

    age_h_str = f"{age_hours:.1f}" if age_hours is not None else "unknown"
    result = _classify_claude(title, summary or "", source_id, age_h_str)
    if result is None:
        result = _fallback_alert(title, summary or "", store=store)

    _cache_write(store, key, title, result)
    return result


def _strip_codefence(text: str) -> str:
    """LLMs sometimes wrap JSON in ```json ... ``` despite explicit
    instructions otherwise. Strip if present."""
    text = text.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl >= 0:
            text = text[first_nl + 1:]
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


def _classify_claude(title: str, summary: str, source_id: str, age_h: str) -> MarketAlert | None:
    """Claude call with built-in retry for 529 overloaded / 503 transient.
    Three attempts with short backoff — Claude overload is usually <60s."""
    import time as _t
    from .translator import _get_anthropic_client
    client = _get_anthropic_client()
    if not client:
        return None
    # Avoid str.format() because the JSON schema in the prompt has many
    # unescaped braces — use .replace() for the 4 placeholders.
    prompt = (
        _SYSTEM_PROMPT
        .replace("{source_id}", source_id or "unknown")
        .replace("{age_hours}", age_h)
        .replace("{title}", title[:300])
        .replace("{summary}", (summary or "")[:1500])
    )
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=900,
                messages=[{"role": "user", "content": prompt}],
            )
            text = _strip_codefence(resp.content[0].text)
            d = json.loads(text)
            alert = MarketAlert(
                action=d.get("action", "reject"),
                news_type=d.get("news_type", "other"),
                relevance_to_gold=d.get("relevance_to_gold", "none"),
                freshness=d.get("freshness", "unknown"),
                tone=d.get("tone", "neutral"),
                category=d.get("category", "Other"),
                headline_th=d.get("headline_th"),
                body_th=list(d.get("body_th") or [])[:3],
                impact_th=d.get("impact_th"),
                reason=str(d.get("reason") or ""),
            )
            if alert.action == "keep" and not alert.headline_th:
                log.warning("claude returned keep without headline_th — downgrading to reject")
                alert = MarketAlert(action="reject", reason="malformed-keep-no-headline")
            # CJK leak guard — Claude occasionally reaches for Japanese /
            # Chinese kanji when paraphrasing related-region news (e.g.
            # ceasefire = 休戦). Reject the rewrite so the caller doesn't
            # publish mixed-script Thai.
            if alert.action == "keep" and _has_cjk_in_alert(alert):
                log.warning("claude rewrite contained CJK characters — downgrading to reject")
                alert = MarketAlert(action="reject", reason="cjk-leak-in-rewrite")
            return alert
        except Exception as e:
            last_exc = e
            # Retry on transient overload codes only; everything else
            # bubbles to the fallback.
            s = str(e)
            transient = any(c in s for c in (" 529", " 503", " 502", " 504",
                                             "overloaded", "rate_limit"))
            if attempt < 2 and transient:
                wait = 2.0 * (attempt + 1)
                log.info("claude transient (attempt %d): %s — sleeping %ss",
                         attempt + 1, s[:80], wait)
                _t.sleep(wait)
                continue
            log.warning("claude classify+rewrite failed: %s", e)
            return None
    if last_exc:
        log.warning("claude classify+rewrite exhausted retries: %s", last_exc)
    return None


def _fallback_alert(title: str, summary: str, store: "Store | None" = None) -> MarketAlert:
    """Conservative fallback when Claude is unavailable. Accepts the item
    and runs a literal translation so the pipeline still publishes — we
    prefer noisy output over a silent channel during a Claude outage."""
    from .translator import to_thai
    th_title = to_thai(title, max_len=200, store=store) or title
    th_summary = to_thai(summary, max_len=400, store=store) if summary else ""
    body: list[str] = []
    if th_summary:
        # Split summary at sentence boundaries to make bullets
        parts = [p.strip() for p in th_summary.replace("\n", " ").split(".") if p.strip()]
        for p in parts[:2]:
            body.append(p[:120])
    return MarketAlert(
        action="keep",
        news_type="other",
        relevance_to_gold="medium",
        freshness="unknown",
        tone="neutral",
        category="Other",
        headline_th=th_title[:90],
        body_th=body,
        impact_th=None,
        reason="claude-unavailable: literal-translation fallback",
    )
