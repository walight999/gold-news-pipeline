"""Thai translation for digest / breaking / alert content.

Backends (waterfall):
  1. Anthropic Claude Haiku — set ANTHROPIC_API_KEY to enable.
     Best quality for finance jargon ("patient stance" → "ท่าทีอดทน"
     instead of "ผู้ป่วยยืน").
  2. Google Translate via deep-translator — free, no key, used when the
     LLM call fails or the key isn't set. Good for long-form, mediocre
     on short finance titles.
  3. None — caller renders the English original.

Translation only — never AI summarisation or rephrasing.
"""
from __future__ import annotations

import logging
import os
import re

log = logging.getLogger(__name__)

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

PLACES — use Thai forms:
- China → จีน         Hong Kong → ฮ่องกง         Taiwan → ไต้หวัน
- Russia → รัสเซีย    Ukraine → ยูเครน           Iran → อิหร่าน
- Israel → อิสราเอล   Saudi Arabia → ซาอุดิอาระเบีย
- Japan → ญี่ปุ่น     South Korea → เกาหลีใต้    North Korea → เกาหลีเหนือ
- United States / US → สหรัฐ   Europe / EU / Eurozone → ยุโรป / สหภาพยุโรป / ยูโรโซน
- United Kingdom / UK / Britain → อังกฤษ / สหราชอาณาจักร

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
    return _patch_names(out)


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


def to_thai(text: str | None, max_len: int = 600) -> str | None:
    """Translate English to Thai. Tries Claude first (when key is set),
    falls back to Google. Returns None if both fail so callers can show
    the English original.

    Output is validated to ensure no Chinese / Japanese / Korean script
    leaks through (Google Translate passes through Chinese names from
    Reuters-style sources; Claude transliterates them properly to Thai)."""
    if not text:
        return None
    text = re.sub(r"\s+", " ", text).strip()[:max_len]
    if not text:
        return None
    return _translate_claude(text) or _translate_google(text)
