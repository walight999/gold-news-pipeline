"""Thai translation for digest / breaking / alert content.

Backends (waterfall):
  0. translation_cache Sheet tab — SHA-keyed exact-match cache. Cuts
     Claude token cost ~70% across cron iterations because the same RSS
     items keep reappearing for hours before they age out.
  1. Anthropic Claude Haiku — set ANTHROPIC_API_KEY to enable.
     Best quality for finance jargon ("patient stance" → "ท่าทีอดทน"
     instead of "ผู้ป่วยยืน").
  2. Google Translate via deep-translator — free, no key, used when the
     LLM call fails or the key isn't set. Good for long-form, mediocre
     on short finance titles.
  3. None — caller renders the English original.

Translation only — never AI summarisation or rephrasing.

Cache lifecycle:
  - to_thai_cached(text, store) checks the cache first, on miss calls the
    waterfall, on success writes back. Maintain mode prunes >24h-old
    entries + caps at 2000 most-recent rows.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .store import Store

log = logging.getLogger(__name__)


def _cache_key(text: str) -> str:
    """16-char SHA-256 prefix. 16 chars = 64 bits → collision-free across
    any reasonable cache size (~2k rows; birthday paradox needs ~2^32)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _cache_lookup(store: "Store | None", key: str) -> str | None:
    if store is None:
        return None
    row = store.get("translation_cache", (key,))
    if not row:
        return None
    return row.get("thai_text") or None


def _cache_write(store: "Store | None", key: str, src_preview: str, thai: str) -> None:
    if store is None:
        return
    from .utils_time import iso_utc, now_utc
    existing = store.get("translation_cache", (key,)) or {}
    hits = int(existing.get("hits") or 0) + 1
    created = existing.get("created_at") or iso_utc(now_utc())
    store.upsert("translation_cache", {
        "cache_key": key,
        "source_preview": src_preview[:80],
        "thai_text": thai,
        "hits": str(hits),
        "created_at": created,
    })

_LLM_PROMPT = """Translate this English financial news to natural Thai for a Thai-speaking trader audience. Rules:

GENERAL
- Faithful translation — preserve every fact, number, percentage, entity, and ticker symbol.
- Thai financial journalism style, NOT literal word-for-word.
- Output ONLY the Thai translation. No preamble, no explanation, no quotes.

KEEP IN ENGLISH (do NOT translate or transliterate):
- Data: CPI, Core CPI, PCE, Core PCE, PPI, NFP, GDP, PMI, ISM
- Central banks: Fed, FOMC, ECB, BoJ, BoE, PBOC, SNB, BoC, RBA, RBNZ, IMF
- Currencies: USD, EUR, GBP, JPY, CNY, AUD, CAD, CHF, NZD
- Markets: DXY, RSI, MACD, S&P 500, Nasdaq, Dow, NYSE, ETF
- Companies / tickers: keep ticker symbols (AAPL, TSLA, etc.)

NAMES — use these Thai forms exactly:
- Donald Trump / Trump → ทรัมป์
- Joe Biden / Biden → ไบเดน
- Kamala Harris → แฮร์ริส
- Xi Jinping → สี จิ้นผิง
- Li Qiang → หลี่ เฉียง
- Vladimir Putin / Putin → ปูติน
- Volodymyr Zelensky / Zelenskyy → เซเลนสกี
- Benjamin Netanyahu / Netanyahu → เนทันยาฮู
- Kim Jong Un → คิม จอง อึน
- Yoon Suk Yeol → ยุน ซอกยอล
- Narendra Modi / Modi → โมดี
- Jerome Powell / Powell → พาวเวลล์
- Christine Lagarde / Lagarde → ลาการ์ด
- Kazuo Ueda / Ueda → อูเอดะ
- Andrew Bailey / Bailey → เบลีย์
- Tiff Macklem / Macklem → แมคเลม
- Thomas Jordan → จอร์แดน
- Janet Yellen / Yellen → เยลเลน
- John Williams / Williams (Fed) → วิลเลียมส์
- Raphael Bostic → บอสติก
- Austan Goolsbee → กูลส์บี
- Christopher Waller → วอลเลอร์
- Neel Kashkari → คาชคารี
- Lael Brainard → เบรนาร์ด
- Mary Daly → ดาลี
- Lisa Cook → คุก

PLACES — use Thai forms (NEVER leave a country / region / strait name in English):
- China → จีน         Hong Kong → ฮ่องกง         Taiwan → ไต้หวัน
- Russia → รัสเซีย    Ukraine → ยูเครน           Iran → อิหร่าน
- Israel → อิสราเอล   Saudi Arabia → ซาอุดิอาระเบีย
- Japan → ญี่ปุ่น     South Korea → เกาหลีใต้    North Korea → เกาหลีเหนือ
- United States / US → สหรัฐ   Europe / EU / Eurozone → ยุโรป / สหภาพยุโรป / ยูโรโซน
- United Kingdom / UK / Britain → อังกฤษ / สหราชอาณาจักร
- Geopolitical chokepoints / hotspots (translate, do NOT keep in English):
  Strait of Hormuz → ช่องแคบฮอร์มุส   Red Sea → ทะเลแดง   Suez Canal → คลองสุเอซ
  Taiwan Strait → ช่องแคบไต้หวัน      South China Sea → ทะเลจีนใต้
  Gaza → ฉนวนกาซา   Yemen → เยเมน   Lebanon → เลบานอน   Syria → ซีเรีย   Iraq → อิรัก
  Venezuela → เวเนซุเอลา   Qatar → กาตาร์   UAE → สหรัฐอาหรับเอมิเรตส์

KEEP BRAND NAMES IN ENGLISH (do NOT translate the brand word — translate only the surrounding sentence):
- Prediction / betting markets: Polymarket, Kalshi, PredictIt. (e.g. "Polymarket คาด..." NOT "ตลาดเทพเจ้า"/"ตลาดทำนาย")

FINANCE TERMS — use these Thai forms:
- inflation → เงินเฟ้อ
- deflation → เงินฝืด
- disinflation → การชะลอตัวของเงินเฟ้อ
- stagflation → ภาวะเศรษฐกิจชะลอ-เงินเฟ้อสูง
- recession → ภาวะถดถอย
- soft landing → เศรษฐกิจชะลอแบบนุ่มนวล
- hard landing → เศรษฐกิจชะลอแบบรุนแรง
- yields → อัตราผลตอบแทน
- treasury yields → อัตราผลตอบแทนพันธบัตรสหรัฐ
- real yields → อัตราผลตอบแทนที่แท้จริง
- bond auction → การประมูลพันธบัตร
- safe-haven / safe-haven bid → สินทรัพย์ปลอดภัย / แรงซื้อสินทรัพย์ปลอดภัย
- monetary policy → นโยบายการเงิน
- fiscal policy → นโยบายการคลัง
- rate cut / cuts → ลดดอกเบี้ย
- rate hike / hikes → ขึ้นดอกเบี้ย
- pause hikes → หยุดขึ้นดอกเบี้ย
- higher for longer → ดอกเบี้ยสูงนานขึ้น
- hawkish → ท่าทีเข้มงวด
- dovish → ท่าทีผ่อนคลาย
- patient stance → ท่าทีอดทน / รอดู
- data dependent → ขึ้นกับข้อมูล
- sticky inflation → เงินเฟ้อที่อยู่ในระดับสูงต่อเนื่อง
- cooling inflation → เงินเฟ้อชะลอตัว
- labor market cools → ตลาดแรงงานชะลอตัว
- supply shock → ช็อกด้านอุปทาน
- demand shock → ช็อกด้านอุปสงค์
- tariff / tariffs → อัตราภาษีนำเข้า
- trade war → สงครามการค้า
- sanctions → มาตรการคว่ำบาตร
- ceasefire → ข้อตกลงหยุดยิง
- de-escalation → การลดความตึงเครียด
- escalation → การยกระดับความตึงเครียด
- breaks support → หลุดแนวรับ
- breaks resistance → ทะลุแนวต้าน
- rally / rallies → ปรับขึ้น / พุ่ง
- plunge / plunges → ร่วง / ดิ่ง
- ETF inflows → กระแสเงินไหลเข้า ETF
- ETF outflows → กระแสเงินไหลออกจาก ETF
- central bank buying → การซื้อของธนาคารกลาง

CHINESE / JAPANESE / KOREAN NAMES IN SOURCE:
- If the source contains a Chinese / Japanese / Korean character name
  (e.g., "习近平", "ウエダ"), ALWAYS transliterate it to Thai script.
  Do NOT pass the original CJK characters through.

Now translate:

English: {text}

Thai:"""


# Lazy clients — avoid the imports until we actually need them.
_anthropic_client = None
_google_instance = None


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not key:
            _anthropic_client = False
            return False
        try:
            import anthropic
            _anthropic_client = anthropic.Anthropic(api_key=key)
        except ImportError:
            log.warning("anthropic SDK not installed — falling back to Google")
            _anthropic_client = False
    return _anthropic_client


def _get_google():
    global _google_instance
    if _google_instance is None:
        try:
            from deep_translator import GoogleTranslator
            _google_instance = GoogleTranslator(source="en", target="th")
        except ImportError:
            log.warning("deep-translator not installed")
            _google_instance = False
    return _google_instance


# Last-mile safety net for the Google Translate fallback path. Claude
# follows the in-prompt glossary; Google does not, so we patch the most
# common political / central-bank names after-the-fact. Regex uses word
# boundaries so we don't touch "Trumpets" or "Powellette".
_NAME_PATCH = [
    (re.compile(r"\bDonald\s+(?:J\.?\s+)?Trump\b", re.I), "ทรัมป์"),
    (re.compile(r"\bTrump\b", re.I), "ทรัมป์"),
    (re.compile(r"\bJoe\s+Biden\b", re.I), "ไบเดน"),
    (re.compile(r"\bBiden\b", re.I), "ไบเดน"),
    (re.compile(r"\bKamala\s+Harris\b", re.I), "แฮร์ริส"),
    (re.compile(r"\bXi\s+Jinping\b", re.I), "สี จิ้นผิง"),
    (re.compile(r"\bLi\s+Qiang\b", re.I), "หลี่ เฉียง"),
    (re.compile(r"\bVladimir\s+Putin\b", re.I), "ปูติน"),
    (re.compile(r"\bPutin\b", re.I), "ปูติน"),
    (re.compile(r"\bVolodymyr\s+Zelensky+\b", re.I), "เซเลนสกี"),
    (re.compile(r"\bZelensky+\b", re.I), "เซเลนสกี"),
    (re.compile(r"\bBenjamin\s+Netanyahu\b", re.I), "เนทันยาฮู"),
    (re.compile(r"\bNetanyahu\b", re.I), "เนทันยาฮู"),
    (re.compile(r"\bKim\s+Jong\s*Un\b", re.I), "คิม จอง อึน"),
    (re.compile(r"\bNarendra\s+Modi\b", re.I), "โมดี"),
    (re.compile(r"\bJerome\s+Powell\b", re.I), "พาวเวลล์"),
    (re.compile(r"\bPowell\b", re.I), "พาวเวลล์"),
    (re.compile(r"\bChristine\s+Lagarde\b", re.I), "ลาการ์ด"),
    (re.compile(r"\bLagarde\b", re.I), "ลาการ์ด"),
    (re.compile(r"\bKazuo\s+Ueda\b", re.I), "อูเอดะ"),
    (re.compile(r"\bUeda\b", re.I), "อูเอดะ"),
    (re.compile(r"\bAndrew\s+Bailey\b", re.I), "เบลีย์"),
    (re.compile(r"\bJanet\s+Yellen\b", re.I), "เยลเลน"),
    (re.compile(r"\bYellen\b", re.I), "เยลเลน"),
]


def _patch_names(text: str) -> str:
    """Apply the name glossary post-translation. Only meaningful for the
    Google Translate fallback path; Claude already produces these names
    correctly via the in-prompt glossary. Safe to run on Claude output
    too — it's idempotent if Claude already used the canonical form."""
    for pat, repl in _NAME_PATCH:
        text = pat.sub(repl, text)
    return text


# Place / geography glossary for the Google fallback path. Google Translate
# routinely leaves geopolitical proper nouns in English ("Hormuz", "Ukraine")
# or picks an odd literal ("สัญจร" for transit), producing cards that read as
# machine output. We can't fix Google's grammar, but we CAN force the canonical
# Thai place name so a degraded outage card doesn't carry raw English. Order
# matters: multi-word forms (Strait of Hormuz) must come BEFORE the bare form
# (Hormuz) so the longer match wins. English-word \b regexes never match Thai
# script, so this is a safe no-op on Claude output that already used Thai.
_PLACE_PATCH = [
    (re.compile(r"\bStrait\s+of\s+Hormuz\b", re.I), "ช่องแคบฮอร์มุส"),
    (re.compile(r"\bHormuz\s+Strait\b", re.I), "ช่องแคบฮอร์มุส"),
    (re.compile(r"\bHormuz\b", re.I), "ฮอร์มุส"),
    (re.compile(r"\bRed\s+Sea\b", re.I), "ทะเลแดง"),
    (re.compile(r"\bSuez\s+Canal\b", re.I), "คลองสุเอซ"),
    (re.compile(r"\bTaiwan\s+Strait\b", re.I), "ช่องแคบไต้หวัน"),
    (re.compile(r"\bSouth\s+China\s+Sea\b", re.I), "ทะเลจีนใต้"),
    (re.compile(r"\bUkraine\b", re.I), "ยูเครน"),
    (re.compile(r"\bGaza\b", re.I), "ฉนวนกาซา"),
    (re.compile(r"\bYemen\b", re.I), "เยเมน"),
    (re.compile(r"\bLebanon\b", re.I), "เลบานอน"),
    (re.compile(r"\bSyria\b", re.I), "ซีเรีย"),
]


def _patch_places(text: str) -> str:
    """Apply the place glossary post-translation. Same rationale as
    `_patch_names` — the Google fallback leaves geographic proper nouns in
    English; force the canonical Thai form. Idempotent on Thai output."""
    for pat, repl in _PLACE_PATCH:
        text = pat.sub(repl, text)
    return text


def _has_cjk(text: str | None) -> bool:
    """True if `text` contains any Chinese / Japanese / Korean script
    characters. We want Thai-only output — Chinese leaks via Google
    Translate when the source RSS item carries Chinese names of officials
    or places (Reuters/Yahoo Finance does this a lot)."""
    if not text:
        return False
    for c in text:
        if "一" <= c <= "鿿":            # CJK Unified Ideographs
            return True
        if "぀" <= c <= "ヿ":            # Japanese Hiragana + Katakana
            return True
        if "가" <= c <= "힯":            # Korean Hangul
            return True
    return False


def _clean_translation(out: str | None) -> str | None:
    """Reject empty / CJK-leaked outputs. Caller falls back to next
    backend (or English original) when this returns None."""
    if not out:
        return None
    out = out.strip()
    if not out:
        return None
    if _has_cjk(out):
        log.info("translation rejected — CJK leak detected")
        return None
    return _patch_places(_patch_names(out))


def _translate_claude(text: str) -> str | None:
    client = _get_anthropic_client()
    if not client:
        return None
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": _LLM_PROMPT.format(text=text)}],
        )
        return _clean_translation(resp.content[0].text)
    except Exception as e:
        log.warning("claude translate failed: %s", e)
        return None


def _translate_google(text: str) -> str | None:
    g = _get_google()
    if not g:
        return None
    try:
        return _clean_translation(g.translate(text))
    except Exception as e:
        log.warning("google translate failed: %s", e)
        return None


def to_thai(text: str | None, max_len: int = 600, store: "Store | None" = None) -> str | None:
    """Translate English to Thai. Cache-first, then Claude, then Google.
    Returns None if every backend fails so callers render the English
    original.

    `store` is optional — when provided, exact-match translations are
    cached in the `translation_cache` Sheet tab to avoid re-translating
    the same RSS title every cron run (~70% token-cost reduction).

    Output is validated to ensure no Chinese / Japanese / Korean script
    leaks through (Google passes through Chinese names from Reuters-style
    sources; Claude transliterates them properly to Thai)."""
    if not text:
        return None
    text = re.sub(r"\s+", " ", text).strip()[:max_len]
    if not text:
        return None

    key = _cache_key(text)
    cached = _cache_lookup(store, key)
    if cached:
        # Bump hits counter for cache visibility but don't re-translate.
        _cache_write(store, key, text, cached)
        return cached

    out = _translate_claude(text) or _translate_google(text)
    if out:
        _cache_write(store, key, text, out)
    return out
