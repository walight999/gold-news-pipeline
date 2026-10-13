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
    # Structured gold call (2026-09-27): the card showed tone (hawkish/dovish)
    # and left the trader to convert it into a gold direction. "" = not
    # provided (older cached results, fallback path) → no pill is rendered.
    gold_bias: str = ""          # "bullish" | "bearish" | "mixed" | ""
    gold_confidence: str = ""    # "high" | "medium" | "low" | ""
    reason: str = ""
    is_fallback: bool = False    # True = permissive Google-translate accept (Claude down)
    # Which model wrote the Thai (e.g. "claude-haiku-4-5-20251001",
    # "gemini-2.0-flash", "+repair:claude-sonnet-4-6" appended when the grammar
    # repair rewrote it). Serialized, so a cache hit keeps its origin. Logged to
    # content_log.flags — the 2026-10-13 quality read could not tell which
    # model produced the worst cards.
    model: str = ""
    # Transient (NOT serialized): $ this result cost (classify + any repair),
    # fed to the monthly $ brake. 0 for cache hits / fallback / unpriced models.
    cost_usd: float = 0.0
    # Transient (NOT serialized): grammar-QC issue labels found on this rewrite,
    # for logging + content_log visibility. Empty after a successful repair. The
    # cached copy is the post-repair text, so cache hits need no flags.
    grammar_flags: list[str] = field(default_factory=list)

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
            "gold_bias": self.gold_bias,
            "gold_confidence": self.gold_confidence,
            "reason": self.reason,
            "model": self.model,
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
                gold_bias=_norm_bias(d.get("gold_bias")),
                gold_confidence=_norm_conf(d.get("gold_confidence")),
                reason=str(d.get("reason") or ""),
                model=str(d.get("model") or ""),
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            return None


_BIASES = ("bullish", "bearish", "mixed")
_CONFS = ("high", "medium", "low")


def _norm_bias(v) -> str:
    v = str(v or "").strip().lower()
    return v if v in _BIASES else ""


def _norm_conf(v) -> str:
    v = str(v or "").strip().lower()
    return v if v in _CONFS else ""


_REJECTED_NO_TITLE = MarketAlert(action="reject", reason="empty title")

# Secondary provider. gemini-2.0-flash was shut down 2026-06-01 (Google
# deprecations page) — every call since then errored and the chain silently fell
# to the literal Google-Translate path. gemini-3.6-flash is Google's listed
# replacement. Verify with `--mode llm_probe` after changing it.
GEMINI_DEFAULT_MODEL = "gemini-3.6-flash"


# $ per 1M tokens (input, output), Anthropic first-party rates. Cache writes
# (5-min TTL) bill at 1.25x input, cache reads at 0.1x. Prefix-matched so a
# dated id ("claude-haiku-4-5-20251001") resolves. Unknown model → $0 and a
# warning, so a new model never silently disables the brake's arithmetic.
_PRICES_PER_MTOK = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-opus-5": (5.0, 25.0),
}


def claude_cost_usd(model: str, usage) -> float:
    """Dollar cost of one Messages API response from its `usage`."""
    price = next((v for k, v in _PRICES_PER_MTOK.items() if (model or "").startswith(k)), None)
    if price is None:
        log.warning("no price for model %s — cost counted as $0", model)
        return 0.0
    p_in, p_out = price
    g = lambda f: int(getattr(usage, f, 0) or 0)  # noqa: E731
    return (g("input_tokens") * p_in + g("cache_creation_input_tokens") * p_in * 1.25
            + g("cache_read_input_tokens") * p_in * 0.1 + g("output_tokens") * p_out) / 1e6


# Models that run adaptive thinking when `thinking` is omitted (Sonnet 5,
# Opus 5…). CLAUDE_CLASSIFY_THINKING "low" (default) = adaptive at effort low;
# "off" disables thinking. 60-headline A/B (2026-10-14): low read a hawkish
# "Fed will act to ensure inflation resumes its slowdown" correctly where off
# called it gold-positive, and cost LESS ($0.0053 vs $0.0056/item — fewer output
# tokens). Older models (Haiku 4.5, Sonnet 4.6) don't think by default → no kwargs.
_THINKS_BY_DEFAULT = ("claude-sonnet-5", "claude-opus-5")


def _claude_thinking_kwargs(model: str) -> dict:
    if not (model or "").startswith(_THINKS_BY_DEFAULT):
        return {}
    if os.environ.get("CLAUDE_CLASSIFY_THINKING", "low").strip().lower() == "off":
        return {"thinking": {"type": "disabled"}}
    return {"thinking": {"type": "adaptive"}, "output_config": {"effort": "low"}}


def _response_text(resp) -> str:
    """Concatenated text blocks (a thinking block may come first)."""
    return "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")


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
4. Opinion piece without a fresh market catalyst (no data print, no policy signal). EXCEPTION: a NAMED Fed / central-bank official's remark on inflation, rates, or policy stance is a central-bank signal (see KEEP), NOT opinion — keep it.
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
- Fresh central-bank signal — speech, decision, minutes, dot plot, FX intervention rhetoric (Fed/ECB/BoJ/BoE/SNB/BoC speakers naming a policy direction). This INCLUDES a NAMED Fed / central-bank official (a sitting member OR a widely-watched figure, e.g. Powell, Waller, Bowman, Warsh) commenting on inflation, rates, or a hawkish/dovish stance — that IS a gold catalyst even without a formal decision or a specific number (their tone moves USD/yields). Do NOT bucket such a remark as "opinion".
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
  "gold_bias": "bullish | bearish | mixed",
  "gold_confidence": "high | medium | low",
  "reason": "..."
}

============================================================
REWRITE CONSTRAINTS — only when action="keep"
============================================================
- headline_th MUST be ≤ 90 Thai characters. Lead with the entity + the result. A reader sees this FIRST and must grasp the story from it alone. No clickbait, no question marks, no mid-thought cut-offs.
- body_th: 1-2 COMPLETE, self-contained Thai sentences (each ≤ 150 chars). The reader must FULLY UNDERSTAND the story from headline + body WITHOUT opening the link. Each sentence is a whole thought with its figures and context — NOT chopped keyword fragments, NOT a phrase that ends mid-idea. Include the actual numbers (with units / %) when present.
- impact_th: ONE clear sentence on the XAU/USD/JPY/yields impact. If genuinely unclear: "ผลกระทบต่อทองคำยังไม่ชัดเจน". It MUST follow GOLD DIRECTION RULES below.
- gold_bias: the net direction this news pushes XAU/USD right now — "bullish", "bearish", or "mixed" when the drivers conflict or the effect is unclear. MUST agree with impact_th.
- gold_confidence: how clear that transmission is — "high" (direct: Fed/CPI/NFP surprise, major escalation), "medium" (indirect or partly priced in), "low" (speculative, second-order). Use "low" rather than overstating.
- NO restating the exact headline in the body — the body ADDS detail (the numbers, the why, the next thing to watch).
- NO "ในซื้อขายวันศุกร์" / "ดึงเอียง" / "ผลกระทบของการเดินทาง" / awkward direct translations. Use natural Thai trading desk language.
- ใช้ตัวย่อไทยมาตรฐานเพื่อประหยัดพื้นที่การ์ด: ประธานาธิบดี→ปธน., นายกรัฐมนตรี→นายกฯ, รัฐมนตรีว่าการกระทรวง→รมว., รัฐมนตรี→รมต., สหรัฐอเมริกา→สหรัฐฯ. ใช้เฉพาะตัวย่อที่ผู้อ่านไทยเข้าใจทันที.

============================================================
GOLD DIRECTION RULES — impact_th + gold_bias (non-negotiable)
============================================================
Gold is priced in US dollars. Every impact chain must obey these signs:
- USD weaker → gold UP.  USD stronger → gold DOWN.
- US yields / real yields UP → gold DOWN.  Down → gold UP.
- Fed hawkish (more/longer hikes) → USD + yields up → gold DOWN. Fed dovish → gold UP.
- War / escalation / sanctions → safe-haven buying → gold UP. De-escalation / peace deal → gold DOWN.
- ANOTHER currency stronger (EUR, JPY, GBP, AUD, CHF, CNY…) = USD WEAKER against it
  → small support for gold. Another currency weaker = USD stronger → small pressure on gold.
  NEVER write "JPY แข็ง … กดดันทอง", "EUR แข็ง … หนุน USD", or "หนุน JPY และหนุน USD" —
  those chains are backwards. A hawkish BoJ/ECB/BoE is NOT negative for gold.
- Non-US data or a non-Fed central bank (UK/EU/Japan/Korea/Nordic PPI, jobs, CPI; BoE/ECB/BoJ/RBA
  remarks): history shows gold barely reacts. Say so plainly, e.g. "ผลต่อทองจำกัด เพราะเป็นข่าว
  [ประเทศ] ไม่ใช่สหรัฐฯ", with gold_bias "mixed" and gold_confidence "low" — do NOT invent a
  USD or safe-haven chain to reach a call. Exception: a big surprise that clearly moves USD/JPY or
  EUR/USD — then give the small FX-driven call above.
- ONE direction per impact_th. Never say gold up and gold down in the same line; if drivers
  conflict, name the stronger one or write "ผลกระทบต่อทองคำยังไม่ชัดเจน".
- At most ONE hedge word (อาจ). Skip "ทางอ้อม" chains. A plain claim reads better than a hedged one.
- Minor geopolitics (local clash, routine statement, talks continuing) is NOT a gold driver —
  don't paste a generic "หนุน safe-haven" line onto it; say the effect is limited.

DATES & FACTS: TODAY (ICT date) is given in the INPUT block. Use only years/months that appear in the source or follow
from TODAY ("next year" = the year after TODAY's year). NEVER write an older year (e.g. 2024/2025)
unless the source says it. A scheduled release that has not happened yet is "ตลาดรอ…", never a result.
Keep the country right: a UK/EU/Japan story stays UK/EU/Japan — never "สหรัฐฯ".

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
  impact_th: "ผลต่อทองจำกัด เพราะเป็นข้อมูลญี่ปุ่น เยนอาจอ่อนเล็กน้อยเทียบดอลลาร์"
  gold_bias: mixed
  gold_confidence: low
  category: Inflation
  tone: dovish

Input:  "ING expects BoJ may hike in June despite weak Japan CPI"
GOOD →
  headline_th: "ING คาด BoJ อาจขึ้นดอกเบี้ยมิ.ย. แม้ CPI ญี่ปุ่นต่ำกว่าคาด"
  body_th:
    - "Core CPI ญี่ปุ่นชะลอลง แต่ ING ยังมองว่า BoJ มีโอกาสขึ้นดอกเบี้ยใน มิ.ย."
    - "ตลาดจะจับตาสัญญาณจาก BoJ ว่าให้น้ำหนัก inflation หรือ wage growth มากกว่า"
  impact_th: "ถ้าตลาดเชื่อว่า BoJ จะขึ้นดอกเบี้ย เยนแข็ง ดอลลาร์อ่อนเทียบเยน เป็นบวกเล็กน้อยต่อทอง"
  gold_bias: bullish
  gold_confidence: low
  category: Central Bank          ← NOT Inflation, because the story is about BoJ outlook
  tone: hawkish                   ← hawkish BoJ ≠ bad for gold: stronger JPY = weaker USD

Input:  "US stocks rise Friday but US-Iran talks weigh"
GOOD →
  headline_th: "หุ้นสหรัฐบวก แต่ตลาดจับตาความเสี่ยงเจรจาสหรัฐ-อิหร่าน"
  body_th:
    - "Dow, S&P 500 และ Nasdaq 100 ปรับขึ้นระหว่างวัน"
    - "นักลงทุนยังระวังความเสี่ยงจากการเจรจาสหรัฐ-อิหร่านที่ยังไม่ชัดเจน"
  impact_th: "ถ้าความตึงเครียดสหรัฐฯ-อิหร่านเพิ่มขึ้น เป็นบวกต่อทองจากแรงซื้อ safe-haven"
  gold_bias: bullish
  gold_confidence: medium
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
  ⚠ hawkish / dovish: keep the ENGLISH word (or, if Thai, ONLY "สายเหยี่ยว" / "สายพิราบ"). NEVER a literal or invented Thai noun — e.g. rendering hawkish as "เงาะ" (rambutan) is WRONG.

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

NATURAL THAI GRAMMAR — write like a Thai desk analyst, not a translator:
- NEVER leave an English verb/preposition untranslated (amid, weighing, ahead of,
  despite, soaring, plunging, dragging, surging). Translate the whole action.
- Prefer active Thai verbs over English-style passive "ถูก...โดย".
  BAD: "ทองถูกกดดันโดยดอลลาร์ที่แข็งค่า"  → GOOD: "ดอลลาร์แข็งค่ากดดันทอง"
- Don't over-nominalise with "การ..." when a plain verb reads better.
  BAD: "การปรับตัวขึ้นของราคาทองเกิดขึ้นหลังการประกาศตัวเลข"
  GOOD: "ราคาทองปรับขึ้นหลังตัวเลขออก"
- Lead with subject + action; keep one clear idea per sentence. No sentence may
  end dangling on a connector (และ / แต่ / ที่ / ของ / เพื่อ).
- Connect clauses with natural Thai (หลัง / หลังจาก / เนื่องจาก / ส่งผลให้ /
  ขณะที่) — not a literal "as" / "amid" / "while".
Grammar example:
  Input:  "Gold slips as dollar firms ahead of CPI"
  BAD  →  "ทองลื่นไถล as ดอลลาร์แข็ง ahead of CPI"   (English leaked, not Thai)
  BAD  →  "ทองถูกลดลงโดยการแข็งค่าของดอลลาร์"          (awkward passive + over-nominalised)
  GOOD →  "ทองอ่อนตัว หลังดอลลาร์แข็งค่าก่อนตัวเลข CPI"

NAMES — Thai-script transliteration:
ทรัมป์ / ไบเดน / แฮร์ริส / สี จิ้นผิง / หลี่ เฉียง / ปูติน / เซเลนสกี / เนทันยาฮู /
คิม จอง อึน / ยุน ซอกยอล / โมดี / พาวเวลล์ / ลาการ์ด / อูเอดะ / เบลีย์ /
แมคเลม / จอร์แดน / เยลเลน

PLACES — ALWAYS write the Thai form; NEVER leave a country / region / strait name in English:
- China→จีน, Hong Kong→ฮ่องกง, Taiwan→ไต้หวัน, Russia→รัสเซีย, Ukraine→ยูเครน, Iran→อิหร่าน, Israel→อิสราเอล, Saudi Arabia→ซาอุดิอาระเบีย, Japan→ญี่ปุ่น, South Korea→เกาหลีใต้, North Korea→เกาหลีเหนือ, US→สหรัฐฯ, Europe/EU→ยุโรป/สหภาพยุโรป, UK/Britain→อังกฤษ
- Chokepoints / hotspots: Strait of Hormuz→ช่องแคบฮอร์มุส, Red Sea→ทะเลแดง, Suez Canal→คลองสุเอซ, Taiwan Strait→ช่องแคบไต้หวัน, South China Sea→ทะเลจีนใต้, Gaza→ฉนวนกาซา, Yemen→เยเมน, Lebanon→เลบานอน, Syria→ซีเรีย, Iraq→อิรัก, Venezuela→เวเนซุเอลา
- WRONG: "ตลาดจับตา Hormuz", "สถานการณ์ Ukraine" → RIGHT: "ตลาดจับตาช่องแคบฮอร์มุส", "สถานการณ์ยูเครน"

KEEP BRAND NAMES IN ENGLISH — translate the sentence around them, never the brand word itself:
- Prediction / betting markets: Polymarket, Kalshi, PredictIt. Correct: "Polymarket คาดโอกาส 98%..." — WRONG: "ตลาดเทพเจ้า" / "ตลาดทำนาย" / "ตลาดพนัน".

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
TODAY: {today}
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
    # + relevance gate + complete-sentence summaries. a4 (2026-09-10): natural-Thai
    # grammar section added to the prompt + grammar QC/repair pass, so a3 rows hold
    # pre-QC copy — invalidate them so the naturalised text takes effect at once.
    # a5 (2026-09-19): named Fed/CB-official commentary is a KEEP catalyst (not
    # "opinion"); hawkish/dovish must stay English/สายเหยี่ยว — re-classify old rows.
    return f"a5{h}"   # total 16 chars to fit existing cache_key column width


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


# Per-process classifier cache counters.
#
# The `hits` COLUMN in translation_cache counts WRITES, not reads: _cache_write
# increments it and _cache_lookup deliberately does not (bumping it would dirty
# a ~2-3 MB tab that flush() rewrites whole, every run, for a vanity metric).
# Consequence: every row in the live tab reads `hits: 1` and the cache hit RATE
# was simply not measurable — which is why the "is the 1-day TTL right?" question
# had no data behind it. These in-process counters cost nothing (no sheet write,
# no API call) and put the real ratio in the Actions log, which is what any
# future TTL / cap change has to be argued from.
_RUN_STATS = {"hits": 0, "misses": 0, "fallbacks": 0}


def run_cache_stats() -> dict[str, int]:
    """Snapshot of this process's classifier cache counters.

    `hit_pct` is -1 when no classify call happened at all, so an idle run reads
    as "no data" instead of a misleading 0%."""
    hits = _RUN_STATS["hits"]
    misses = _RUN_STATS["misses"]
    total = hits + misses
    return {**_RUN_STATS, "total": total,
            "hit_pct": round(100.0 * hits / total) if total else -1}


def reset_cache_stats() -> None:
    """Zero the counters. Only tests need this — a cron process is one run."""
    for k in _RUN_STATS:
        _RUN_STATS[k] = 0


def _month_spend_over_cap(store: "Store | None") -> bool:
    """True when this month's classifier spend has crossed a hard cap — the
    caller then skips Claude and drops to Gemini / literal fallback.

    The only in-repo brake on classifier LLM spend. Since 2026-10-14 it is a
    DOLLAR cap (CLASSIFIER_MONTHLY_USD_CAP, default $40; 0 disables): with prompt
    caching a token count no longer tracks cost (a cached read is 0.1x), and the
    old 8M-token default was on pace to trip ~Oct 26 and push the last days of
    every month onto the fallback. A token cap still applies when
    CLASSIFIER_MONTHLY_TOKEN_CAP is set explicitly. Self-releases on month
    rollover."""
    if store is None:
        return False
    try:
        c = get_classifier_counters(store, source_id=None)
    except Exception:  # noqa: BLE001 — the brake must never crash classification
        return False
    try:
        usd_cap = float(os.environ.get("CLASSIFIER_MONTHLY_USD_CAP", "40"))
    except ValueError:
        usd_cap = 40.0
    spent = float(c.get("month_cost_usd", 0) or 0)
    if usd_cap > 0 and spent >= usd_cap:
        log.warning("classifier monthly $ cap reached ($%.2f >= $%.2f) — skipping "
                    "Claude, using Gemini/fallback until month rolls over", spent, usd_cap)
        return True
    tok_env = os.environ.get("CLASSIFIER_MONTHLY_TOKEN_CAP", "").strip()
    if tok_env:
        try:
            tok_cap = int(tok_env)
        except ValueError:
            tok_cap = 0
        total = int(c.get("month_tokens_in", 0)) + int(c.get("month_tokens_out", 0))
        if tok_cap > 0 and total >= tok_cap:
            log.warning("classifier monthly token cap reached (%d >= %d) — skipping "
                        "Claude, using Gemini/fallback until month rolls over", total, tok_cap)
            return True
    return False


# ------------------------------------------------------------------ grammar QC
_GRAMMAR_REPAIR_PROMPT = """You are a senior Thai financial-news copy editor. The Thai below was machine-rewritten and reads unnatural (literal English word order, leaked English words, or awkward phrasing). Rewrite it into fluent, natural Thai the way a professional Bangkok trading desk writes.

HARD RULES:
- Preserve EVERY fact, number, %, date, ticker, and entity exactly. Add nothing, drop nothing.
- Keep already-correct English terms (CPI, Fed, FOMC, USD, DXY, hawkish, dovish, safe-haven, yields).
- Canonical Thai names/places (ทรัมป์, พาวเวลล์, ลาการ์ด, ยูเครน, ช่องแคบฮอร์มุส).
- NO Chinese/Japanese/Korean characters. NO em-dash. Gregorian years only (2026), never พ.ศ.
- Thai + English only: NO Arabic / Hebrew / Cyrillic / Greek / Indic letters. If a word has one (e.g. "ดовish", "นেตันยาฮู"), rewrite that whole word correctly ("dovish", "เนทันยาฮู").
- Fix ONLY grammar/naturalness. Same meaning, same number of body bullets.

Issues detected: {issues}

Return STRICT JSON, ONE object, no prose or code fence:
{"headline_th": "...", "body_th": ["..."], "impact_th": "..."}

Current text:
HEADLINE: {headline}
BODY: {body}
IMPACT: {impact}

Corrected JSON:"""


def _grammar_repair_enabled() -> bool:
    """Hybrid default ON: the rule check flags every card for free; the LLM
    repair fires ONLY on flagged cards, so cost is bounded to genuinely-bad
    output. GRAMMAR_LLM_REPAIR=0 disables the LLM pass (rule warnings still log)."""
    return os.environ.get("GRAMMAR_LLM_REPAIR", "1").strip().lower() not in ("0", "false", "no", "")


def _repair_grammar_llm(alert: "MarketAlert", issues: list[str]) -> "MarketAlert | None":
    """One LLM pass (Sonnet by default via GRAMMAR_REPAIR_MODEL) that rewrites
    the flagged Thai fields naturally. Returns a NEW MarketAlert with repaired
    display fields (classification fields copied from `alert`), or None on any
    failure so the caller keeps the original. Best-effort, never raises."""
    import time as _t
    from .translator import (_get_anthropic_client, _has_cjk, _patch_names,
                             _patch_places, strip_em_dash)
    client = _get_anthropic_client()
    if not client:
        return None
    model = os.environ.get("GRAMMAR_REPAIR_MODEL", "claude-sonnet-5").strip()
    prompt = (_GRAMMAR_REPAIR_PROMPT
              .replace("{issues}", ", ".join(issues) or "awkward Thai")
              .replace("{headline}", alert.headline_th or "")
              .replace("{body}", " | ".join(alert.body_th or []))
              .replace("{impact}", alert.impact_th or ""))
    try:
        _gap = _t.time() - _LAST_CLAUDE_CALL[0]
        if _gap < _MIN_CLAUDE_GAP_S:
            _t.sleep(_MIN_CLAUDE_GAP_S - _gap)
        _LAST_CLAUDE_CALL[0] = _t.time()
        extra = _claude_thinking_kwargs(model)
        resp = client.messages.create(model=model,
                                      max_tokens=2000 if "output_config" in extra else 900,
                                      messages=[{"role": "user", "content": prompt}], **extra)
        d = _parse_json_lenient(_response_text(resp))
        repair_cost = claude_cost_usd(model, getattr(resp, "usage", None))
    except Exception as e:  # noqa: BLE001 — best-effort; keep original on any failure
        log.warning("grammar repair failed: %s", e)
        return None
    if not d:
        return None

    def _norm(t: str | None) -> str | None:
        return strip_em_dash(_patch_places(_patch_names(t))) if t else t
    headline = _norm(d.get("headline_th"))
    body = [b for b in (_norm(x) for x in (d.get("body_th") or []) if x) if b][:3]
    impact = _norm(d.get("impact_th"))
    if not headline:                       # a keep must retain its headline
        return None
    if any(_has_cjk(x) for x in (headline, impact, *body)):
        return None                        # never let repair introduce CJK
    return MarketAlert(
        action=alert.action, news_type=alert.news_type,
        relevance_to_gold=alert.relevance_to_gold, freshness=alert.freshness,
        tone=alert.tone, category=alert.category,
        headline_th=headline, body_th=body, impact_th=impact,
        gold_bias=alert.gold_bias, gold_confidence=alert.gold_confidence,
        reason=alert.reason, model=f"{alert.model}+repair:{model}",
        cost_usd=alert.cost_usd + repair_cost)


def _apply_grammar_qc(alert: "MarketAlert") -> "MarketAlert":
    """Rule-based grammar warnings on every kept card (free); on a flag, log it
    and — when repair is enabled (hybrid) — spend ONE LLM pass to fix it, keeping
    the repair only if it actually REDUCED the warning count (never a regression)."""
    from .thai_grammar import alert_grammar_warnings
    warnings = alert_grammar_warnings(alert.headline_th, alert.body_th, alert.impact_th)
    if not warnings:
        return alert
    _RUN_STATS["grammar_flags"] = _RUN_STATS.get("grammar_flags", 0) + 1
    log.warning("grammar QC: [%s] on '%s'", ", ".join(warnings), (alert.headline_th or "")[:70])
    alert.grammar_flags = warnings
    out = alert
    if _grammar_repair_enabled():
        repaired = _repair_grammar_llm(alert, warnings)
        if repaired is not None:
            remaining = alert_grammar_warnings(repaired.headline_th, repaired.body_th,
                                               repaired.impact_th)
            if len(remaining) < len(warnings):
                repaired.grammar_flags = remaining
                log.info("grammar QC: repaired %d→%d warning(s)", len(warnings), len(remaining))
                out = repaired
    # Foreign-script corruption the repair could not remove never ships (same
    # policy as the CJK guard): a card with "ดовish" in it reads as broken.
    if "foreign_script" in out.grammar_flags:
        log.warning("foreign script survived repair — rejecting '%s'", (out.headline_th or "")[:70])
        return MarketAlert(action="reject", reason="foreign-script-in-rewrite",
                           model=out.model, cost_usd=out.cost_usd)
    return out


def classify_and_rewrite(
    title: str,
    summary: str,
    source_id: str = "",
    age_hours: float | None = None,
    store: "Store | None" = None,
    high_quality: bool = False,
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

    # Cache-first. For high_quality (BREAKING) we still HONOR a cached REJECT —
    # re-running a rejected noisy item through Sonnet on every 5-min cron was the
    # single biggest wasted-spend multiplier: a sticky breaking-score item never
    # enters sent_log, so without this it re-classified on the stronger model
    # forever. A cached KEEP is still refreshed for high_quality so breaking gets
    # the sharper Sonnet summary on the cards we actually send.
    cached = _cache_lookup(store, key)
    if cached is not None and not (high_quality and cached.action == "keep"):
        # Do NOT _cache_write on a hit — bumping the `hits` counter dirties the
        # translation_cache tab, which flush() rewrites WHOLE (~2-3 MB) every run
        # for a vanity metric. The hit itself is the only work needed; the free
        # in-process counter below is how the hit RATE gets measured instead.
        _RUN_STATS["hits"] += 1
        _record_classifier_outcome(store, source_id, cached,
                                    used_fallback=False, cache_hit=True)
        return cached

    # A high_quality refresh of a cached KEEP lands here too, and counts as a
    # miss on purpose: the metric that matters is "how many provider calls did
    # the cache actually save", and a refresh costs a call like any other.
    _RUN_STATS["misses"] += 1

    age_h_str = f"{age_hours:.1f}" if age_hours is not None else "unknown"
    # Provider chain: Claude (primary) → Gemini (secondary, e.g. when the
    # Anthropic monthly spend cap is hit) → literal-translation fallback.
    # Gemini uses the SAME prompt + JSON contract, so a Claude outage no
    # longer drops card quality to "Other"/garbled — it just switches model.
    # Monthly spend brake: if this month's Claude tokens are over the cap, skip
    # Claude entirely and drop to Gemini (free) → fallback. There is NO other
    # in-repo gate on LLM spend, so a noisy news week could otherwise run unbounded.
    result, tin, tout = (None, 0, 0)
    if not _month_spend_over_cap(store):
        result, tin, tout = _classify_claude_with_usage(
            title, summary or "", source_id, age_h_str, high_quality=high_quality)
    if result is None:
        g_result, g_tin, g_tout = _classify_gemini_with_usage(
            title, summary or "", source_id, age_h_str)
        if g_result is not None:
            result, tin, tout = g_result, g_tin, g_tout
    used_fallback = False
    if result is None:
        result = _fallback_alert(title, summary or "", store=store)
        used_fallback = True
        _RUN_STATS["fallbacks"] += 1

    # Grammar QC (hybrid) — rule-check every real keep for free; repair the
    # flagged minority with one LLM pass BEFORE caching, so the cached copy (and
    # every 24h cache hit) serves the naturalised text. Fallbacks are skipped
    # (they're a known-degraded outage path, not worth a repair call).
    if not used_fallback and result.action == "keep":
        result = _apply_grammar_qc(result)

    # NEVER cache the fallback — it's a permissive Google-translate accept used
    # only during a Claude outage. Caching it poisoned the digest: a single
    # rate-limited classify cached an "Other"/garbled keep that was then re-served
    # for 24h, bypassing the real classifier + reject rules.
    if not used_fallback:
        _cache_write(store, key, title, result)
    _record_classifier_outcome(store, source_id, result,
                                used_fallback=used_fallback, cache_hit=False,
                                tokens_in=tin, tokens_out=tout,
                                high_quality=high_quality, cost_usd=result.cost_usd)
    return result


def _record_classifier_outcome(
    store: "Store | None",
    source_id: str,
    alert: MarketAlert,
    used_fallback: bool,
    cache_hit: bool,
    tokens_in: int = 0,
    tokens_out: int = 0,
    high_quality: bool = False,
    cost_usd: float = 0.0,
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

        # Monthly token tally — separate from rolling window. high-quality
        # (Sonnet/breaking) tokens are ALSO tallied separately so the Sonnet
        # share of monthly cost is measurable, not buried in the aggregate.
        if include_tokens:
            if blob.get("month") != month_key:
                blob["month"] = month_key
                blob["month_tokens_in"] = 0
                blob["month_tokens_out"] = 0
                blob["month_hq_tokens_in"] = 0
                blob["month_hq_tokens_out"] = 0
                blob["month_cost_usd"] = 0.0
                blob["month_hq_cost_usd"] = 0.0
            blob["month_cost_usd"] = round(float(blob.get("month_cost_usd", 0) or 0) + cost_usd, 6)
            if high_quality:
                blob["month_hq_cost_usd"] = round(
                    float(blob.get("month_hq_cost_usd", 0) or 0) + cost_usd, 6)
            blob["month_tokens_in"] = int(blob.get("month_tokens_in", 0)) + tokens_in
            blob["month_tokens_out"] = int(blob.get("month_tokens_out", 0)) + tokens_out
            if high_quality:
                blob["month_hq_tokens_in"] = int(blob.get("month_hq_tokens_in", 0)) + tokens_in
                blob["month_hq_tokens_out"] = int(blob.get("month_hq_tokens_out", 0)) + tokens_out

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
        totals["month_cost_usd"] = float(blob.get("month_cost_usd", 0) or 0)
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


# The prompt splits at "INPUT:" into a STATIC part (rules, glossary, examples —
# ~5.9K tokens, identical on every call) and the per-item INPUT block. Claude
# gets the static part as a cached system block (prompt caching: cache reads
# bill at 0.1x), so it must contain NO per-call value — keep {placeholders}
# below the INPUT: line.
_PROMPT_SPLIT = "INPUT:"


def _prompt_parts(title: str, summary: str, source_id: str, age_h: str) -> tuple[str, str]:
    """(static rules, rendered per-item input) — see _PROMPT_SPLIT."""
    static, tail = _SYSTEM_PROMPT.split(_PROMPT_SPLIT, 1)
    return static.rstrip(), _render_input(_PROMPT_SPLIT + tail, title, summary, source_id, age_h)


def _build_prompt(title: str, summary: str, source_id: str, age_h: str) -> str:
    """Render the shared classify+rewrite prompt as ONE string (Gemini and
    tests). Claude uses _prompt_parts so the static part can be cached."""
    return _render_input(_SYSTEM_PROMPT, title, summary, source_id, age_h)


def _render_input(template: str, title: str, summary: str, source_id: str, age_h: str) -> str:
    # Avoid str.format() because the JSON schema in the prompt has many
    # unescaped braces — use .replace() for the placeholders.
    from .utils_time import now_ict
    return (
        template
        .replace("{source_id}", source_id or "unknown")
        .replace("{age_hours}", age_h)
        .replace("{today}", now_ict().strftime("%Y-%m-%d"))
        .replace("{title}", title[:300])
        .replace("{summary}", (summary or "")[:1500])
    )


def _alert_from_text(text: str) -> MarketAlert | None:
    """Parse an LLM response into a validated MarketAlert, applying the
    same guards regardless of which provider produced it: keep-requires-
    headline, and CJK-leak rejection. Returns None when the text can't be
    parsed into a usable object (caller then tries the next provider)."""
    d = _parse_json_lenient(text)
    if d is None:
        return None
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
        gold_bias=_norm_bias(d.get("gold_bias")),
        gold_confidence=_norm_conf(d.get("gold_confidence")),
        reason=str(d.get("reason") or ""),
    )
    if alert.action == "keep" and not alert.headline_th:
        log.warning("LLM returned keep without headline_th — downgrading to reject")
        return MarketAlert(action="reject", reason="malformed-keep-no-headline")
    # Place-name safety net — force canonical Thai place names on any English
    # geographic proper noun the model left untranslated (Gemini especially
    # leaks "Hormuz"/"Ukraine" despite the prompt glossary). English-word \b
    # regexes can't touch Thai script, so this is a no-op when the model already
    # used the Thai form, and never disturbs kept-English acronyms (Fed, CPI).
    if alert.action == "keep":
        from .translator import _patch_names, _patch_places, strip_em_dash

        def _norm(t: str) -> str:
            # Same net _clean_translation applies to the fallback path: force
            # canonical Thai names + places (Gemini leaks both despite the
            # prompt glossary), then strip the banned em-dash. All idempotent
            # when the model already used the Thai form / plain hyphens.
            return strip_em_dash(_patch_places(_patch_names(t)))
        if alert.headline_th:
            alert.headline_th = _norm(alert.headline_th)
        if alert.impact_th:
            alert.impact_th = _norm(alert.impact_th)
        if alert.body_th:
            alert.body_th = [_norm(b) for b in alert.body_th]
    # CJK leak guard — models occasionally reach for Japanese / Chinese
    # kanji when paraphrasing related-region news (e.g. ceasefire = 休戦).
    # Reject so the caller doesn't publish mixed-script Thai.
    if alert.action == "keep" and _has_cjk_in_alert(alert):
        log.warning("LLM rewrite contained CJK characters — downgrading to reject")
        return MarketAlert(action="reject", reason="cjk-leak-in-rewrite")
    return alert


def _gemini_error_reason(resp) -> str:
    """`error.status: error.message` from a Gemini error body (≤200 chars,
    digit runs ≥6 masked), or the raw text head when it isn't JSON."""
    import re as _re
    try:
        err = (resp.json() or {}).get("error") or {}
        text = f"{err.get('status', '')}: {err.get('message', '')}".strip(": ")
    except Exception:  # noqa: BLE001 — diagnostics only
        text = (getattr(resp, "text", "") or "")
    return _re.sub(r"\d{6,}", "<n>", text)[:200] or "(empty body)"


def _classify_gemini_with_usage(
    title: str, summary: str, source_id: str, age_h: str,
) -> tuple["MarketAlert | None", int, int]:
    """Secondary classifier — Google Gemini, used when Claude is
    unavailable (e.g. Anthropic monthly spend cap). Uses the SAME prompt
    and JSON contract as Claude so card quality is preserved, instead of
    dropping to the literal Google-Translate fallback.

    Gated by GEMINI_API_KEY. Calls GEMINI_DEFAULT_MODEL (override GEMINI_MODEL) via
    the REST endpoint (httpx — no extra SDK dependency). Returns
    (alert, input_tokens, output_tokens); tokens come from usageMetadata.
    Any failure returns (None, 0, 0) so the caller falls through."""
    import time as _t
    import httpx
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return None, 0, 0
    model = os.environ.get("GEMINI_MODEL", GEMINI_DEFAULT_MODEL).strip()
    prompt = _build_prompt(title, summary, source_id, age_h)
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            # Gemini 2.5+ count thinking inside maxOutputTokens — 900 (sized for
            # 2.0 Flash) could truncate the JSON on a thinking model.
            "maxOutputTokens": 4096,
            "responseMimeType": "application/json",
        },
    }
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(
                    url, json=body,
                    headers={"x-goog-api-key": api_key},
                )
            if resp.status_code == 429 or resp.status_code >= 500:
                raise RuntimeError(f"gemini transient {resp.status_code}")
            if resp.status_code >= 400:
                # Say WHY (2026-10-13: a bare "402 Payment Required" hid whether
                # it was billing, quota or the model). Logs of this public repo
                # are public, so long digit runs (project numbers) are masked.
                log.warning("gemini %s: %s", resp.status_code, _gemini_error_reason(resp))
            resp.raise_for_status()
            data = resp.json()
            cands = data.get("candidates") or []
            if not cands:
                log.warning("gemini returned no candidates: %s", str(data)[:200])
                return None, 0, 0
            parts = (cands[0].get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts)
            alert = _alert_from_text(text)
            if alert is None:
                raise ValueError(f"unparseable JSON from Gemini: {text[:200]}...")
            alert.model = model
            usage = data.get("usageMetadata") or {}
            tin = int(usage.get("promptTokenCount", 0) or 0)
            tout = int(usage.get("candidatesTokenCount", 0) or 0)
            return alert, tin, tout
        except Exception as e:
            last_exc = e
            s = str(e)
            transient = any(c in s for c in ("transient", " 429", " 503", " 502", " 504"))
            if attempt < 2 and transient:
                _t.sleep(1.5 * (attempt + 1))
                continue
            log.warning("gemini classify+rewrite failed: %s", e)
            return None, 0, 0
    if last_exc:
        log.warning("gemini classify+rewrite exhausted retries: %s", last_exc)
    return None, 0, 0


def _classify_claude(title: str, summary: str, source_id: str, age_h: str) -> MarketAlert | None:
    """Back-compat wrapper — returns alert only."""
    alert, _, _ = _classify_claude_with_usage(title, summary, source_id, age_h)
    return alert


def _classify_claude_with_usage(
    title: str, summary: str, source_id: str, age_h: str,
    high_quality: bool = False,
) -> tuple["MarketAlert | None", int, int]:
    """Claude call with built-in retry + token usage reporting.
    Returns (alert, input_tokens, output_tokens). Tokens default to 0
    when call fails / fallback used.

    `high_quality=True` (BREAKING route only) uses the stronger Sonnet model
    for a sharper summary on the few highest-impact cards/day; everything else
    stays on cheap Haiku. Override the breaking model via CLAUDE_BREAKING_MODEL."""
    import time as _t
    from .translator import _get_anthropic_client
    client = _get_anthropic_client()
    if not client:
        return None, 0, 0
    # high_quality (BREAKING) → Sonnet. Everything else (digest + alert, the bulk
    # of what the reader sees) → Haiku by default, but overridable via
    # CLAUDE_CLASSIFY_MODEL so grammar quality can be lifted to Sonnet without a
    # code change when Haiku's Thai reads too stiff (2026-09-10 quality report).
    # 2026-10-14: both routes default to Sonnet 5 ($2/$10, cheaper than the old
    # Sonnet 4.6 breaking model). The 2026-10-13 read of 1,499 cards scored
    # digest/Haiku 3.04/5 vs breaking/Sonnet 3.95; with the ~5.9K-token static
    # prompt cached, Sonnet 5 on every route costs less than Haiku+Sonnet 4.6 did.
    model = (os.environ.get("CLAUDE_BREAKING_MODEL", "claude-sonnet-5").strip()
             if high_quality
             else os.environ.get("CLAUDE_CLASSIFY_MODEL", "claude-sonnet-5").strip())
    static, user = _prompt_parts(title, summary, source_id, age_h)
    extra = _claude_thinking_kwargs(model)
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
                model=model,
                max_tokens=2000 if "output_config" in extra else 900,
                # Static rules as a cached system block (reads bill at 0.1x);
                # the per-item INPUT goes in the user turn.
                system=[{"type": "text", "text": static,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                **extra,
            )
            text = _response_text(resp)
            alert = _alert_from_text(text)
            if alert is None:
                raise ValueError(f"unparseable JSON from Claude: {text[:200]}...")
            alert.model = model
            # Token usage — input counts cached + uncached prompt tokens so the
            # volume metrics stay comparable; $ cost prices each kind properly.
            usage = getattr(resp, "usage", None)
            tin = sum(int(getattr(usage, f, 0) or 0) for f in (
                "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
            tout = int(getattr(usage, "output_tokens", 0) or 0)
            alert.cost_usd = claude_cost_usd(model, usage)
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


def _soft_trim(text: str, limit: int) -> str:
    """Trim Thai text without cutting mid-word. Thai has no inter-word
    spaces, so a hard `text[:limit]` slice almost always lands inside a
    word and produces garbled output. Instead we trim at the last phrase
    boundary (Thai DOES use spaces between clauses/phrases) at or before
    the limit; if there is none, we keep the whole string rather than
    cut it mid-word. An ellipsis marks a real truncation."""
    text = text.strip()
    if len(text) <= limit:
        return text
    # Prefer the last space-delimited phrase boundary within the budget.
    cut = text.rfind(" ", 0, limit)
    if cut >= limit * 0.5:        # a usable boundary reasonably close to the limit
        return text[:cut].rstrip() + "…"
    # No good boundary: keep the whole phrase intact rather than mangle it.
    return text


def _fallback_alert(title: str, summary: str, store: "Store | None" = None) -> MarketAlert:
    """Conservative fallback when Claude is unavailable. Accepts the item
    and runs a literal translation so the pipeline still publishes — we
    prefer noisy output over a silent channel during a Claude outage.

    Output is built from COMPLETE translated sentences so the card reads
    cleanly even though no Claude rewrite happened. We never hard-slice
    Thai (no inter-word spaces → mid-word cuts); see `_soft_trim`."""
    from .translator import strip_em_dash, to_thai
    th_title = strip_em_dash(to_thai(title, max_len=200, store=store) or title)
    th_summary = strip_em_dash(to_thai(summary, max_len=400, store=store)) if summary else ""
    body: list[str] = []
    if th_summary:
        # Split at sentence boundaries; keep whole sentences up to a budget.
        parts = [p.strip() for p in th_summary.replace("\n", " ").split(".") if p.strip()]
        used = 0
        for p in parts:
            sentence = _soft_trim(p, 150)
            if not sentence:
                continue
            body.append(sentence)
            used += len(sentence)
            # One full sentence is usually enough; take a 2nd only if short.
            if len(body) >= 2 or used >= 150:
                break
    return MarketAlert(
        action="keep",
        news_type="other",
        relevance_to_gold="medium",
        freshness="unknown",
        tone="neutral",
        category="Other",
        # Body-less fallback cards carry the whole story in the headline, so give
        # it room to be a complete thought (wraps to 2-3 lines) rather than a
        # 90-char "..." cut mid-sentence (White 2026-06-21).
        headline_th=_soft_trim(th_title, 180),
        body_th=body,
        impact_th=None,
        reason="claude-unavailable: literal-translation fallback",
        is_fallback=True,
        model="google-translate-fallback",
    )


# --------------------------------------------------------------------- calendar
#
# Released economic events (FOMC, CPI, NFP, ...) used to ship as Actual/Forecast/
# Previous + a direction pill only. White asked for the actual CONTENT: a short
# Thai read of what the print means for gold, not just bull/bear/flat.

_CAL_EXPLAIN_PROMPT = """You are a Thai trading-desk editor for an XAU/USD (gold) alert system.
A scheduled economic indicator just released. Write a SHORT Thai explanation a gold trader reads to understand what the number means for gold. This is NOT a translation and NOT generic filler — use ONLY the figures provided, never invent numbers.

OUTPUT — strict JSON, ONE object, no prose, no markdown fence:
{"detail_th": ["ประโยคที่ 1", "ประโยคที่ 2"]}

RULES:
- 2-3 COMPLETE Thai sentences, each <= 150 characters, each a whole thought.
- Sentence 1: the result — actual vs forecast vs previous, and whether it beat / missed / in-line.
- Sentence 2: what it implies for policy (Fed/central bank) or USD / yields.
- Sentence 3 (optional): the read-through to ทอง/XAU — direction and the why.
- If you mention how gold has moved, quote XAU_MOVE exactly as given (US dollars, spot, over the stated minutes). Never convert it to a percentage and never claim a time window other than the one given.
- Keep these EXACTLY in English: Fed, FOMC, ECB, BoJ, BoE, CPI, Core CPI, PCE, NFP, GDP, PMI, ISM, USD, DXY, yields, hawkish, dovish, safe-haven.
- Gregorian years only (2026). No Buddhist Era. No emoji. No Chinese/Japanese/Korean characters.
- If the figures are missing or unclear, return {"detail_th": []}.

INPUT:
EVENT: {title}
COUNTRY: {country}
IMPACT: {impact}
ACTUAL: {actual}
FORECAST: {forecast}
PREVIOUS: {previous}
SURPRISE: {surprise}
GOLD_VERDICT: {verdict}
XAU_MOVE: {xau}

JSON output (strict, single object, no surrounding text):"""


def _cal_cache_key(event_id: str, actual: str) -> str:
    """16-char cache key for a released-event explanation, in the shared
    translation_cache tab. 'cl' prefix keeps it distinct from the 'a3'
    classifier rows and the legacy plain-text rows."""
    h = hashlib.sha256(f"{event_id}\n{actual or ''}".encode("utf-8")).hexdigest()[:14]
    return f"cl{h}"


def _render_cal_prompt(title, country, impact, actual, forecast, previous,
                       surprise, verdict, xau) -> str:
    repl = {
        "{title}": str(title or "")[:200],
        "{country}": str(country or ""),
        "{impact}": str(impact or ""),
        "{actual}": str(actual or "—"),
        "{forecast}": str(forecast or "—"),
        "{previous}": str(previous or "—"),
        "{surprise}": str(surprise or "unknown"),
        "{verdict}": str(verdict or "unclear"),
        "{xau}": "unknown" if xau is None else (xau if isinstance(xau, str) else f"{xau:+.2f}%"),
    }
    out = _CAL_EXPLAIN_PROMPT
    for k, v in repl.items():
        out = out.replace(k, v)
    return out


def _cal_detail_from_text(text: str) -> list[str] | None:
    """Parse the explainer JSON → list of Thai sentences. Drops any bullet
    that leaks CJK script (same guard as the news rewrite). None on parse
    failure so the caller can fall back to the directional-only card."""
    from .translator import _has_cjk
    d = _parse_json_lenient(text)
    if not isinstance(d, dict):
        return None
    bullets = d.get("detail_th")
    if not isinstance(bullets, list):
        return None
    out: list[str] = []
    for b in bullets[:3]:
        s = str(b or "").strip()
        if s and not _has_cjk(s):
            out.append(s[:200])
    return out or None


def explain_calendar_release(
    event_id: str,
    title: str,
    country: str = "",
    impact: str = "",
    actual: str | None = None,
    forecast: str | None = None,
    previous: str | None = None,
    surprise: str | None = None,
    verdict: str | None = None,
    xau_reaction_pct: float | None = None,
    store: "Store | None" = None,
    xau_move_text: str | None = None,
) -> list[str] | None:
    """Short Thai explanation of a just-released economic event, for the
    Released-News card. Claude Haiku → Gemini → None (card degrades to the
    directional-only layout). Cached in translation_cache keyed on
    (event_id, actual); the post-release push is idempotent per event, so in
    practice this is at most one Claude call per High/Medium release.

    Best-effort: any failure returns None and is logged, never raised — the
    calendar run must not crash on the explainer."""
    if not title:
        return None
    key = _cal_cache_key(event_id, actual or "")
    # Cache lookup (same tab as the news rewrite).
    if store is not None:
        row = store.get("translation_cache", (key,))
        if row and row.get("thai_text"):
            cached = _cal_detail_from_text(row["thai_text"])
            if cached is not None:
                return cached
    prompt = _render_cal_prompt(title, country, impact, actual, forecast,
                                previous, surprise, verdict,
                                xau_move_text if xau_move_text is not None else xau_reaction_pct)
    text = _cal_explain_llm(prompt)
    if not text:
        return None
    detail = _cal_detail_from_text(text)
    if detail is None:
        return None
    # Sanitize the explainer output with the SAME net the news rewrite uses
    # (this path previously had none, so calendar Released-News cards could ship
    # raw English names, em-dashes, and — the 2026-09-10 report — leaked CJK).
    # Force canonical Thai names/places + strip em-dash on every line; if any
    # line still carries CJK the model failed to transliterate, so drop the
    # explanation (card degrades to the directional-only layout) rather than
    # ship foreign script.
    from .translator import _has_cjk, _patch_names, _patch_places, strip_em_dash
    cleaned = [strip_em_dash(_patch_places(_patch_names(s))) for s in detail if s]
    cleaned = [s for s in cleaned if s]
    if not cleaned:
        return None
    if any(_has_cjk(s) for s in cleaned):
        log.warning("calendar explainer contained CJK — dropping (event=%s)", event_id)
        return None
    detail = cleaned
    # Cache the rendered JSON so re-runs / retries don't re-call the model.
    if store is not None:
        try:
            from .utils_time import iso_utc, now_utc
            store.upsert("translation_cache", {
                "cache_key": key,
                "source_preview": str(title)[:80],
                "thai_text": json.dumps({"detail_th": detail}, ensure_ascii=False),
                "hits": "1",
                "created_at": iso_utc(now_utc()),
            })
        except Exception:
            log.exception("calendar explainer cache write failed event=%s", event_id)
    return detail


def _cal_explain_llm(prompt: str) -> str | None:
    """Claude Haiku → Gemini for the calendar explainer. Returns the raw
    model text (JSON), or None when both providers are unavailable."""
    import time as _t

    from .translator import _get_anthropic_client
    client = _get_anthropic_client()
    if client is not None:
        model = os.environ.get("CLAUDE_CALENDAR_MODEL", "claude-haiku-4-5-20251001").strip()
        for attempt in range(3):
            try:
                _gap = _t.time() - _LAST_CLAUDE_CALL[0]
                if _gap < _MIN_CLAUDE_GAP_S:
                    _t.sleep(_MIN_CLAUDE_GAP_S - _gap)
                _LAST_CLAUDE_CALL[0] = _t.time()
                resp = client.messages.create(
                    model=model, max_tokens=600,
                    messages=[{"role": "user", "content": prompt}],
                )
                return resp.content[0].text
            except Exception as e:
                s = str(e)
                transient = any(c in s for c in (" 429", " 529", " 503", " 502", " 504",
                                                 "overloaded", "rate_limit"))
                if attempt < 2 and transient:
                    _t.sleep(2.0 * (attempt + 1))
                    continue
                log.warning("calendar explainer (claude) failed: %s", e)
                break
    # Gemini secondary — reuse the same REST path as the classifier.
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if api_key:
        import httpx
        model = os.environ.get("GEMINI_MODEL", GEMINI_DEFAULT_MODEL).strip()
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{model}:generateContent")
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 2048,
                                 "responseMimeType": "application/json"},
        }
        try:
            with httpx.Client(timeout=30.0) as c:
                resp = c.post(url, json=body, headers={"x-goog-api-key": api_key})
            resp.raise_for_status()
            data = resp.json()
            cands = data.get("candidates") or []
            if cands:
                parts = (cands[0].get("content") or {}).get("parts") or []
                return "".join(p.get("text", "") for p in parts)
        except Exception as e:
            log.warning("calendar explainer (gemini) failed: %s", e)
    return None
