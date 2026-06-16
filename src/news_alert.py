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
    is_fallback: bool = False    # True = permissive Google-translate accept (Claude down)

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

_SYSTEM_PROMPT = """You are a professional trading-desk editor for a real-time XAU/USD (gold) alert system used by Thai traders.

This is NOT a translation task. You are REWRITING the market-moving point only — short enough to fit a Telegram alert card. If the input is article-style, evergreen, personal-finance, stale, or has no direct catalyst, REJECT it.

============================================================
HARD REJECT — return action="reject" if ANY of these apply
============================================================
1. Personal-finance advice OR personal money-decision questions (savings tips, retirement, annuity, pension, insurance, "I'm 55 with $100k, should I take my pension", "protect your nest egg", Social Security timing)
2. Evergreen / how-to / explainer ("how to invest", "5 ways to ...", "guide to ...")
3. Lifestyle, wellness, health content
4. Opinion piece without a fresh market catalyst (no data print, no policy signal)
5. Single-company story with no macro implication — a single stock, an IPO/listing of one firm, a single-name ETF launch (e.g. "Nvidia rallies", "Tesla earnings", "SpaceX to start trading after IPO", "Canary HBAR ETF approved")
6. Calendar PREVIEW / "What to watch today" / "Today's main events" / session wraps ("Asia wrap", "Europe wrap")
7. Generic market commentary without a SPECIFIC event ("markets mixed", "stocks digest data")
8. Crypto / digital-asset content of any kind — coins, tokens, crypto ETFs, price predictions, altcoin analysis (HBAR, BTC ETF, etc.) unless it explicitly moves gold/USD
9. Duplicate / rephrased / repackaged article
10. Older than 48 hours
11. Meta content ("This article was written by ...", site promos, byline credits)
12. Has macro KEYWORDS only — no fresh event / data / policy signal underneath
13. Garbled / incomplete data you cannot summarise coherently — numbers with no clear figure or unit (e.g. "CPI: +0", "unemployment: 4" with no %, truncated feed rows). If you can't write a clear Thai sentence a reader understands, REJECT.

NOT GOLD-RELEVANT — if relevance_to_gold would be "none", REJECT. The reader only wants items that plausibly move XAU/USD (inflation, central banks, USD/yields, geopolitics/safe-haven, real macro data).

When you reject, set headline_th=null, body_th=[], impact_th=null, and reason MUST briefly say why.

============================================================
KEEP — return action="keep" only if the item reports
============================================================
- Fresh economic data release with actual / forecast / previous numbers (CPI, PCE, NFP, GDP, Retail Sales, ISM, PMI, Jobless Claims, Durable Goods, Housing)
- Fresh central-bank signal — speech, decision, minutes, dot plot, FX intervention rhetoric (Fed/ECB/BoJ/BoE/SNB/BoC speakers naming a policy direction)
- Specific geopolitical escalation / de-escalation (named country / conflict / sanction / Hormuz / oil-supply event)
- USD / yields / DXY breakout with magnitude
- Material risk-sentiment shift with specific drivers

============================================================
OUTPUT — strict JSON, one object, NO surrounding prose or fence
============================================================
{
  "action": "keep" or "reject",
  "news_type": "data_release | central_bank | geopolitics | energy | rates_yields | fx | equity_macro | equity_specific | personal_finance | evergreen_article | opinion | duplicate | crypto | preview_or_wrap | other",
  "relevance_to_gold": "high | medium | low | none",
  "freshness": "fresh | stale | unknown",
  "tone": "hawkish | dovish | risk_on | risk_off | neutral",
  "category": "Inflation | Central Bank | Geopolitics | Energy | Equity | Macro | Other",
  "headline_th": "...",
  "body_th": ["...", "..."],
  "impact_th": "...",
  "reason": "..."
}

============================================================
REWRITE CONSTRAINTS — only when action="keep"
============================================================
- headline_th MUST be ≤ 90 Thai characters. Lead with the entity + the result. A reader sees this FIRST and must grasp the story from it alone. No clickbait, no question marks, no mid-thought cut-offs.
- body_th: 1-2 COMPLETE, self-contained Thai sentences (each ≤ 150 chars). The reader must FULLY UNDERSTAND the story from headline + body WITHOUT opening the link. Each sentence is a whole thought with its figures and context — NOT chopped keyword fragments, NOT a phrase that ends mid-idea. Include the actual numbers (with units / %) when present.
- impact_th: ONE clear sentence on the XAU/USD/JPY/yields impact. If genuinely unclear: "ผลกระทบต่อทองคำยังไม่ชัดเจน".
- NO restating the exact headline in the body — the body ADDS detail (the numbers, the why, the next thing to watch).
- NO "ในซื้อขายวันศุกร์" / "ดึงเอียง" / "ผลกระทบของการเดินทาง" / awkward direct translations. Use natural Thai trading desk language.

CATEGORY ROUTING — pick the most precise category:
- Central-bank outlook / analyst forecasts of central-bank action → "Central Bank" (NOT "Inflation", even if the article mentions CPI)
- Actual CPI / PCE / PPI data release → "Inflation"
- Geopolitics affecting equities AND gold simultaneously → "Geopolitics" (not "Equity")
- Pure stock-market move with no clear macro catalyst → action=reject

EXAMPLES — these are the EXACT REWRITE STYLE expected:

Input:  "Japan Core CPI slowed to 1.4% y/y in April vs 1.7% expected"
GOOD →
  headline_th: "Core CPI ญี่ปุ่นชะลอเหลือ 1.4% y/y ต่ำกว่าคาด"
  body_th:
    - "Core CPI ออกมาที่ 1.4% ต่ำกว่าคาด 1.7% และต่ำกว่าก่อนหน้า 1.8%"
    - "เงินเฟ้อที่อ่อนลงลดแรงกดดันต่อ BoJ ในการเร่งขึ้นดอกเบี้ย"
  impact_th: "กดดัน JPY และอาจหนุน USD ทางอ้อม ซึ่งเป็นลบต่อทองหาก USD แข็งต่อ"
  category: Inflation
  tone: dovish

Input:  "ING expects BoJ may hike in June despite weak Japan CPI"
GOOD →
  headline_th: "ING คาด BoJ อาจขึ้นดอกเบี้ยมิ.ย. แม้ CPI ญี่ปุ่นต่ำกว่าคาด"
  body_th:
    - "Core CPI ญี่ปุ่นชะลอลง แต่ ING ยังมองว่า BoJ มีโอกาสขึ้นดอกเบี้ยใน มิ.ย."
    - "ตลาดจะจับตาสัญญาณจาก BoJ ว่าให้น้ำหนัก inflation หรือ wage growth มากกว่า"
  impact_th: "หากตลาดเพิ่มคาดการณ์ BoJ hawkish อาจหนุน JPY และกด USD ทางอ้อม"
  category: Central Bank          ← NOT Inflation, because the story is about BoJ outlook
  tone: hawkish

Input:  "US stocks rise Friday but US-Iran talks weigh"
GOOD →
  headline_th: "หุ้นสหรัฐบวก แต่ตลาดจับตาความเสี่ยงเจรจาสหรัฐ-อิหร่าน"
  body_th:
    - "Dow, S&P 500 และ Nasdaq 100 ปรับขึ้นระหว่างวัน"
    - "นักลงทุนยังระวังความเสี่ยงจากการเจรจาสหรัฐ-อิหร่านที่ยังไม่ชัดเจน"
  impact_th: "หากความตึงเครียดเพิ่มขึ้น อาจหนุนทองและกด risk assets; แต่ USD แข็งอาจจำกัด upside ทอง"
  category: Geopolitics
  tone: risk_off

Input:  "What's on the docket today? European session..."
GOOD →
  action: reject
  reason: "calendar preview / session wrap — no specific event"

============================================================
GLOSSARY — PRESERVE these terms EXACTLY in English
============================================================
Data: CPI, Core CPI, PCE, Core PCE, PPI, NFP, GDP, PMI, ISM, FOMC
Banks: Fed, FOMC, ECB, BoJ, BoE, PBOC, SNB, BoC, RBA, RBNZ, IMF, OPEC
Currencies: USD, EUR, JPY, GBP, CNY, AUD, CAD, CHF, NZD
Markets: DXY, RSI, MACD, S&P 500, Nasdaq, Dow
Sentiment: hawkish, dovish, risk-on, risk-off, yield, yields, soft landing, hard landing, safe-haven, dot plot, forward guidance

INSTITUTION SHORT FORMS — never expand to long Thai names ANYWHERE
(headline_th, every body_th bullet, and impact_th):
- Bank of Japan → BoJ           (NEVER "ธนาคารกลางญี่ปุ่น" anywhere in output)
- Federal Reserve → Fed         (NEVER "ธนาคารกลางสหรัฐ" / "ธนาคารกลางสหรัฐฯ")
- European Central Bank → ECB   (NEVER "ธนาคารกลางยุโรป")
- Bank of England → BoE         (NEVER "ธนาคารกลางอังกฤษ")
- People's Bank of China → PBOC

BANNED Thai phrasings — these are unnatural / machine-translation artifacts:
- "ดึงเอียง"        → use "เป็นปัจจัยกดดัน" / "ถ่วงตลาด" / "เป็นปัจจัยเสี่ยง"
- "ในซื้อขายวันศุกร์" → use "ระหว่างวัน" / "ในวันศุกร์"
- "ขึ้นในซื้อขาย"   → use "ปรับขึ้นระหว่างวัน"
- "ขึ้นในการ"       → rephrase
- Any literal back-translation of "amid" / "drag" / "weighing on" that produces awkward Thai.
Use natural Thai trading-desk vocabulary instead.

NAMES — Thai-script transliteration:
ทรัมป์ / ไบเดน / แฮร์ริส / สี จิ้นผิง / หลี่ เฉียง / ปูติน / เซเลนสกี / เนทันยาฮู /
คิม จอง อึน / ยุน ซอกยอล / โมดี / พาวเวลล์ / ลาการ์ด / อูเอดะ / เบลีย์ /
แมคเลม / จอร์แดน / เยลเลน

============================================================
HARD RULES — non-negotiable
============================================================
- DATES: ALWAYS Gregorian year (2026, 2027). NEVER convert to Buddhist Era. NEVER write "2563", "2568", "2569", "พ.ศ.", "BE".
- No hallucinations — only facts present in the source.
- No emojis. No casual language. No quoting article voice. No meta references.
- Body must NOT contain Chinese / Japanese / Korean script characters — transliterate to Thai instead. Specifically:
    休戦 → ใช้ "หยุดยิง"
    避險 → ใช้ "สินทรัพย์ปลอดภัย" or "safe-haven"
    休会 → ใช้ "หยุดประชุม"
    Any kanji name → use the Thai-script transliteration from the names list above.

JSON FORMATTING — non-negotiable:
- Output ONE JSON object only — no leading prose, no trailing prose, no markdown fence.
- Use double quotes for all keys and string values.
- Escape any double-quote inside a string as \\".
- Escape any newline inside a string as \\n (do NOT emit raw newlines inside body_th bullets).
- No trailing commas. No comments. Standard RFC-8259 JSON.

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
    # Version prefix — bump to invalidate ALL old cached classifications when the
    # classifier prompt / rules change. a3 (2026-06-16): new strict reject rules
    # + relevance gate + complete-sentence summaries; also purges the fallback
    # (Google-translate "Other") rows that used to be cached and re-served.
    return f"a3{h}"   # total 16 chars to fit existing cache_key column width


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


_CACHE_HARD_CAP = 3000   # in-memory cap; maintain mode does the 24h TTL pass

# Proactive spacing between Claude calls within a run (see _classify_claude_*).
# 0.5s ≈ ≤120 calls/min, under typical Haiku per-minute limits, so the
# classify burst stops tripping the rate limiter into the noisy fallback.
_MIN_CLAUDE_GAP_S = 0.5
_LAST_CLAUDE_CALL = [0.0]   # mutable module-level timestamp of the last call


def _cache_write(store: "Store | None", key: str, src_title: str, alert: MarketAlert) -> None:
    """Upsert the alert into the translation_cache tab. Enforces a
    hard cap of `_CACHE_HARD_CAP` rows in-memory — when exceeded, the
    oldest entry (by updated_at) is evicted before the upsert. This
    prevents intra-day cache bloat between maintain runs."""
    if store is None:
        return
    from .utils_time import iso_utc, now_utc
    existing = store.get("translation_cache", (key,)) or {}
    hits = int(existing.get("hits") or 0) + 1
    created = existing.get("created_at") or iso_utc(now_utc())

    # Cap enforcement — only when we're about to ADD a new row (not on
    # repeat writes to an existing key). Without the cap, a single
    # high-volume day could grow translation_cache to many thousands of
    # rows, slowing every subsequent load_all.
    if not existing:
        cache_tab = store.data.get("translation_cache", {})
        if len(cache_tab) >= _CACHE_HARD_CAP:
            # Find the single oldest row (by updated_at) and drop it.
            # Linear scan is fine — happens at most once per write at the
            # cap edge, and the cap is small enough (3000) that it's fast.
            oldest_rk = min(
                cache_tab.keys(),
                key=lambda rk: cache_tab[rk].get("updated_at") or "",
            )
            cache_tab.pop(oldest_rk, None)
            store.dirty.setdefault("translation_cache", set()).add("__evict__")

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
    so the pipeline still publishes during ANTHROPIC_API_KEY outages.

    Side-effects (when `store` is provided):
    - Per-source counters incremented in source_state so watchdog can
      flag sources whose reject rate gets too high (>90% over 7d → that
      source is just noise, consider disabling).
    - Classifier-health counters incremented in a synthetic
      _classifier_health row so watchdog can flag silent classifier
      degradation (Claude key invalidated → all calls fall through to
      permissive Google translate, channel goes noisy without anyone
      knowing)."""
    if not title:
        return _REJECTED_NO_TITLE

    key = _cache_key_alert(title, summary or "")

    cached = _cache_lookup(store, key)
    if cached is not None:
        _cache_write(store, key, title, cached)
        _record_classifier_outcome(store, source_id, cached,
                                    used_fallback=False, cache_hit=True)
        return cached

    age_h_str = f"{age_hours:.1f}" if age_hours is not None else "unknown"
    result, tin, tout = _classify_claude_with_usage(title, summary or "", source_id, age_h_str)
    used_fallback = False
    if result is None:
        result = _fallback_alert(title, summary or "", store=store)
        used_fallback = True

    # NEVER cache the fallback — it's a permissive Google-translate accept used
    # only during a Claude outage. Caching it poisoned the digest: a single
    # rate-limited classify cached an "Other"/garbled keep that was then re-served
    # for 24h, bypassing the real classifier + reject rules.
    if not used_fallback:
        _cache_write(store, key, title, result)
    _record_classifier_outcome(store, source_id, result,
                                used_fallback=used_fallback, cache_hit=False,
                                tokens_in=tin, tokens_out=tout)
    return result


def _record_classifier_outcome(
    store: "Store | None",
    source_id: str,
    alert: MarketAlert,
    used_fallback: bool,
    cache_hit: bool,
    tokens_in: int = 0,
    tokens_out: int = 0,
) -> None:
    """Increment classifier counters with a 24h rolling window so
    degradation alerts trigger on RECENT failure rate, not a months-old
    average that's slow to react.

    Schema in `items_last_hour` (JSON blob):
      {
        "buckets": [
          {"hour": "2026-05-23T01", "kept": 5, "rejected": 3, "fallback": 0,
           "cache_hits": 2, "tokens_in": 8000, "tokens_out": 1200},
          ...
        ],
        "month": "2026-05",  // for monthly token totals
        "month_tokens_in": 250000,
        "month_tokens_out": 32000
      }
    Buckets older than 24 hours are pruned on every write.

    Per-source rows track only outcomes (no tokens). The
    "_classifier_health" global row tracks everything."""
    if store is None:
        return
    from .utils_time import iso_utc, now_ict, now_utc
    ts_now = now_utc()
    ts_iso = iso_utc(ts_now)
    hour_key = ts_now.strftime("%Y-%m-%dT%H")
    month_key = now_ict().strftime("%Y-%m")

    def _bump(row_id: str, include_tokens: bool) -> None:
        row = store.get("source_state", (row_id,)) or {"source_id": row_id}
        blob = _parse_blob(row.get("items_last_hour"))

        # 24h rolling buckets
        buckets = blob.get("buckets", [])
        # Drop old buckets (>24h ago)
        from datetime import timedelta
        cutoff_hour = (ts_now - timedelta(hours=24)).strftime("%Y-%m-%dT%H")
        buckets = [b for b in buckets if b.get("hour", "") >= cutoff_hour]
        # Find or create current hour bucket
        cur = next((b for b in buckets if b.get("hour") == hour_key), None)
        if cur is None:
            cur = {"hour": hour_key, "kept": 0, "rejected": 0,
                   "fallback": 0, "cache_hits": 0}
            buckets.append(cur)
        if cache_hit:
            cur["cache_hits"] = cur.get("cache_hits", 0) + 1
        if used_fallback:
            cur["fallback"] = cur.get("fallback", 0) + 1
        if alert.action == "keep":
            cur["kept"] = cur.get("kept", 0) + 1
        else:
            cur["rejected"] = cur.get("rejected", 0) + 1
        if include_tokens and (tokens_in or tokens_out):
            cur["tokens_in"] = cur.get("tokens_in", 0) + tokens_in
            cur["tokens_out"] = cur.get("tokens_out", 0) + tokens_out

        blob["buckets"] = buckets

        # Monthly token tally — separate from rolling window
        if include_tokens:
            if blob.get("month") != month_key:
                blob["month"] = month_key
                blob["month_tokens_in"] = 0
                blob["month_tokens_out"] = 0
            blob["month_tokens_in"] = int(blob.get("month_tokens_in", 0)) + tokens_in
            blob["month_tokens_out"] = int(blob.get("month_tokens_out", 0)) + tokens_out

        row["source_id"] = row_id
        row["items_last_hour"] = json.dumps(blob)
        row["last_validation_ts"] = ts_iso
        store.upsert("source_state", row)

    if source_id:
        _bump(f"_class:{source_id[:30]}", include_tokens=False)
    _bump("_classifier_health", include_tokens=True)


def _parse_blob(blob) -> dict:
    """Decode items_last_hour. Returns {} on any parse failure."""
    if not blob:
        return {}
    try:
        d = json.loads(blob)
        if isinstance(d, dict):
            return d
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return {}


def get_classifier_counters(store, source_id: str | None = None) -> dict[str, int]:
    """Read 24h ROLLING classifier counters from the bucket history.

    `source_id=None` returns global (_classifier_health). The rolling
    window means watchdog catches degradation on RECENT activity, not
    diluted by historical averages."""
    row_id = f"_class:{source_id[:30]}" if source_id else "_classifier_health"
    row = store.get("source_state", (row_id,)) or {}
    blob = _parse_blob(row.get("items_last_hour"))
    buckets = blob.get("buckets", [])
    # Filter to last-24h buckets
    if buckets:
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        cutoff = (_dt.now(_tz.utc) - _td(hours=24)).strftime("%Y-%m-%dT%H")
        buckets = [b for b in buckets if b.get("hour", "") >= cutoff]
    totals = {"kept": 0, "rejected": 0, "fallback": 0, "cache_hits": 0,
              "tokens_in": 0, "tokens_out": 0}
    for b in buckets:
        for k in totals:
            totals[k] += int(b.get(k, 0) or 0)
    # Carry monthly token tally on the global row only
    if source_id is None:
        totals["month"] = blob.get("month", "")
        totals["month_tokens_in"] = int(blob.get("month_tokens_in", 0))
        totals["month_tokens_out"] = int(blob.get("month_tokens_out", 0))
    return totals


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


def _parse_json_lenient(text: str) -> dict | None:
    """Parse JSON with a few cheap repairs for the failure modes we've
    seen Claude produce despite explicit instructions:
      1. Raw newlines inside string values (RFC-8259 invalid)
      2. Stray prose before/after the object
      3. Trailing commas before } or ]
    Returns None when even the lenient pass fails."""
    text = _strip_codefence(text)
    if not text:
        return None

    # First try strict
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Repair attempt 1: extract the {...} block
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0 or end <= start:
        return None
    candidate = text[start:end + 1]

    # Repair attempt 2: strip trailing commas before ] / }
    import re as _re
    candidate = _re.sub(r",(\s*[}\]])", r"\1", candidate)

    # Repair attempt 3: convert raw newlines inside string literals to \n
    # Walk character by character to track whether we're inside a string.
    out_chars: list[str] = []
    in_string = False
    prev = ""
    for c in candidate:
        if c == '"' and prev != "\\":
            in_string = not in_string
            out_chars.append(c)
        elif c == "\n" and in_string:
            out_chars.append("\\n")
        elif c == "\r" and in_string:
            out_chars.append("\\r")
        else:
            out_chars.append(c)
        prev = c
    candidate = "".join(out_chars)

    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


def _classify_claude(title: str, summary: str, source_id: str, age_h: str) -> MarketAlert | None:
    """Back-compat wrapper — returns alert only."""
    alert, _, _ = _classify_claude_with_usage(title, summary, source_id, age_h)
    return alert


def _classify_claude_with_usage(
    title: str, summary: str, source_id: str, age_h: str,
) -> tuple["MarketAlert | None", int, int]:
    """Claude call with built-in retry + token usage reporting.
    Returns (alert, input_tokens, output_tokens). Tokens default to 0
    when call fails / fallback used."""
    import time as _t
    from .translator import _get_anthropic_client
    client = _get_anthropic_client()
    if not client:
        return None, 0, 0
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
    for attempt in range(4):
        try:
            # Proactive spacing — a single news-cron run classifies dozens of
            # breaking/alert/digest candidates back-to-back; without a gap they
            # burst past Anthropic's per-minute limit and ~30% fall back to the
            # noisy Google-translate path. Keep ≥_MIN_CLAUDE_GAP_S between calls.
            _gap = _t.time() - _LAST_CLAUDE_CALL[0]
            if _gap < _MIN_CLAUDE_GAP_S:
                _t.sleep(_MIN_CLAUDE_GAP_S - _gap)
            _LAST_CLAUDE_CALL[0] = _t.time()
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=900,
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.content[0].text
            d = _parse_json_lenient(text)
            if d is None:
                raise ValueError(f"unparseable JSON from Claude: {text[:200]}...")
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
            # Token usage — Anthropic SDK exposes it on resp.usage
            usage = getattr(resp, "usage", None)
            tin = int(getattr(usage, "input_tokens", 0) or 0)
            tout = int(getattr(usage, "output_tokens", 0) or 0)
            return alert, tin, tout
        except Exception as e:
            last_exc = e
            s = str(e)
            transient = any(c in s for c in (" 429", " 529", " 503", " 502", " 504",
                                             "overloaded", "rate_limit"))
            if attempt < 3 and transient:
                wait = 2.0 * (attempt + 1)
                log.info("claude transient (attempt %d): %s — sleeping %ss",
                         attempt + 1, s[:80], wait)
                _t.sleep(wait)
                continue
            log.warning("claude classify+rewrite failed: %s", e)
            return None, 0, 0
    if last_exc:
        log.warning("claude classify+rewrite exhausted retries: %s", last_exc)
    return None, 0, 0


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
        is_fallback=True,
    )
