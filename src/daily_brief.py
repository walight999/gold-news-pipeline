"""Daily brief — one news pool → three channels, one review surface.

Reads the day's curated social_feed breaking/alert rows (already gold-classified
and Thai-translated), composes THREE @tradetongkam artifacts in one Claude call:

    1. tweet picks   — a curated handful for Twitter (per-event, short)
    2. fb_article    — a long-form Thai wrap for Facebook
    3. video_script  — a scene-marked TTS-ready narration for a daily brief video

and publishes them to ONE Notion review page (checkbox per artifact) so the
operator approves in one place each morning instead of scanning 1000+ feed rows.

Design (mirrors the rest of the pipeline):
- Best-effort + env-gated. No NOTION_TOKEN → DRY RUN: the markdown is written to
  snapshots/ and logged, nothing is posted, exit 0. No ANTHROPIC key → returns
  None, the run logs and exits 0. Never crashes.
- no-ai-slop on all three artifacts (public copy): no em-dash, one direction
  emoji per tweet, source attribution, no engagement-bait.
- Notion blocks are built PROGRAMMATICALLY (not by parsing markdown) so the
  output is robust; long article/script text is chunked to Notion's 2000-char
  rich_text limit.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import timedelta
from typing import Any

from .social_feed import FEED_TAB
from .tweet_writer import _extract_json, _fit, _sanitize
from .utils_time import now_utc, parse_iso, to_ict

log = logging.getLogger("daily_brief")

BRIEF_ICON = "🗞️"
# Once/day, public long-form brand copy → quality matters more than the per-cron
# cost. Haiku garbled Thai words in long-form (เญาปี่/หนี้เงิน); Sonnet is fluent
# and costs ~a few cents/day at 1 call. Override with BRIEF_MODEL.
DEFAULT_MODEL = "claude-sonnet-4-6"
NOTION_VERSION = "2022-06-28"
NOTION_RICHTEXT_LIMIT = 2000
BRIEF_ROUTES = {"breaking", "alert"}


# --------------------------------------------------------------------------
# 1. Collect — the day's curated events from social_feed
# --------------------------------------------------------------------------

def _norm(s: str) -> str:
    """Loose headline key for dedup: keep Thai/alnum, drop spaces/punct/digits."""
    return "".join(c for c in (s or "").lower() if c.isalpha())[:48]


def collect_brief_events(store, hours: int = 24) -> list[dict[str, Any]]:
    """Return the last `hours` of breaking/alert social_feed rows (recap and
    test excluded), deduped by loose headline key, oldest-first. Each item is a
    compact dict the composer prompt consumes."""
    try:
        headers, rows = store.read_feed(FEED_TAB)
    except Exception:  # noqa: BLE001 — feed read is best-effort
        log.exception("daily_brief: read_feed failed")
        return []
    if not rows:
        return []
    cutoff = to_ict(now_utc()) - timedelta(hours=hours)
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for r in rows:
        if str(r.get("type") or "").strip().lower() not in BRIEF_ROUTES:
            continue
        # ts_ict is naive ICT wall-clock ("%Y-%m-%d %H:%M:%S"); compare as ICT.
        ts = parse_iso(str(r.get("ts_ict") or "").replace(" ", "T"))
        if ts is None:
            continue
        if ts.tzinfo is not None:
            ts = ts.replace(tzinfo=None)
        if ts < cutoff.replace(tzinfo=None):
            continue
        key = _norm(r.get("headline_th"))
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append({
            "ts_ict": r.get("ts_ict") or "",
            "tone": r.get("tone") or "",
            "impact_level": r.get("impact_level") or "",
            "headline_th": (r.get("headline_th") or "").strip(),
            "summary_th": (r.get("summary_th") or "").strip(),
            "impact_th": (r.get("impact_th") or "").strip(),
            "source": r.get("source") or "",
        })
    return out


# --------------------------------------------------------------------------
# 2. Compose — one Claude call → tweets + article + video script
# --------------------------------------------------------------------------

_PROMPT = """You are the editor of @tradetongkam, a Thai gold (XAU/USD) trading brand.
Below are today's gold-relevant breaking headlines (already in Thai). Produce a
DAILY BRIEF in the brand's analytical voice for THREE channels.

VOICE + RULES (follow exactly, all Thai output):
- Analytical, confident trader Thai. No hype, no clickbait, no question marks.
- NO em-dash (—). NO AI-isms ("มาดูกันว่า", "น่าตื่นเต้น", engagement-bait closings).
- ATTRIBUTION: cite ONLY the primary institution or publication named IN the
  headline text (Fed, FOMC, BoJ, BoE, ECB, CNBC, WSJ, รอยเตอร์, FXStreet, TD
  Securities, OCBC, RBC, ...). NEVER cite an X/Twitter handle or a wire relay
  (a headline tagged "wire" has no citable source — state the fact without
  attribution). Do not attribute the SAME source in more than 2 tweets; vary it.
- Do NOT invent numbers or levels beyond what the headlines state.

Return ONLY JSON with these keys:
{
 "theme": "<one Thai sentence: the day's dominant driver>",
 "tweets": [
   "<3 to 4 tweets. Each starts with ONE direction emoji 🔴 (gold-bearish) / 🟢 (bullish) / 🟡 (neutral/mixed), then analytical Thai, and ends with this exact line: {TAGS}. Keep each tweet under 270 chars. ONE clear angle per tweet (do not cram two topics). Pick the highest-signal, non-duplicate angles.>"
 ],
 "fb_article": "<Facebook long-form. LINE 1 = a strong one-line Thai hook/headline (no label, no emoji, no hashtag) that makes someone stop scrolling. Then a blank line, then 4 to 6 paragraphs separated by blank lines: state of gold → the driver → the cross-currents → what to watch + key levels. No hashtags. No emoji. Keep paragraphs readable (2-4 sentences each, avoid one giant wall).>",
 "video_script": "<a spoken Thai narration for a ~75-second MACRO-EXPLAINER video, 4-5 scenes. This is a macro news explainer, NOT a gold-chart channel. Each scene: a marker line '[ฉาก N: <visual cue>]' then the spoken lines. The <visual cue> must describe a MACRO-EXPLAINER visual — a central-bank building (Fed/BoJ/ECB), a trading floor or world-markets b-roll, a news desk, OR a clean typographic data card of a PUBLIC number (e.g. 'US 10Y 5%', 'Fed 92.5% → 25bp'). NEVER cue a XAU/USD price chart, candlestick, or any trading-signal chart — those are internal desk data and must not appear. TTS-FRIENDLY THAI (this is read aloud): spell ALL numbers in Thai words, and render English phrases in Thai — safe-haven → สินทรัพย์ปลอดภัย, price in → สะท้อนในราคาแล้ว, dot plot → ดอตพล็อต, basis points/bp → เบสิสพอยต์, hawkish → สายเข้มงวด, dovish → สายผ่อนคลาย. Keep only short well-known tickers spoken as-is (Fed, BoJ, ดอลลาร์, ทอง). End on a soft sign-off, no engagement-bait.>"
}

TODAY'S HEADLINES:
{HEADLINES}
"""


def _clean_source(src: str) -> str:
    """Normalize the feed source tag for the prompt so the model never sees a
    citable X-handle. Wire relays ("X Deitaone", "X Firstsquawk", ...) collapse
    to "wire" (the model is told not to cite those); real publications
    (CNBC / FXStreet / WSJ / รอยเตอร์ ...) pass through as the primary source."""
    s = (src or "").strip()
    if s.lower().startswith("x ") or s.lower() in {"deitaone", "firstsquawk"}:
        return "wire"
    return s


def _headlines_block(events: list[dict[str, Any]], cap: int = 24) -> str:
    lines = []
    for i, e in enumerate(events[:cap], 1):
        src = _clean_source(e.get("source") or "")
        tone = e.get("tone") or ""
        lines.append(
            f"{i}. [{tone}/{src}] {e['headline_th']} :: {e.get('impact_th','')}"
        )
    return "\n".join(lines)


def _get_brief_client():
    """Dedicated Anthropic client with a LONG timeout. The shared
    translator client is timeout=30s (tuned for fast Haiku news calls); a
    once-a-day Sonnet long-form generation needs more, so the brief uses its
    own client rather than retuning the news path."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        import anthropic
        return anthropic.Anthropic(api_key=key, timeout=120.0)
    except Exception:  # noqa: BLE001
        return None


def compose_brief(events: list[dict[str, Any]], *, model: str | None = None,
                  client=None) -> dict[str, Any] | None:
    """One Claude call → {theme, tweets[], fb_article, video_script}. Returns
    None if the client is unavailable or the call/parse fails (caller logs and
    exits cleanly). Tweets are sanitized + length-fit through tweet_writer."""
    if not events:
        return None
    client = client or _get_brief_client()
    if not client:
        log.warning("daily_brief: no Anthropic client — skipping compose")
        return None
    prompt = (_PROMPT
              .replace("{TAGS}", "#ทองวันนี้ #ข่าวทอง #เทรดทอง #ทองคำ")
              .replace("{HEADLINES}", _headlines_block(events)))
    # NB: get(k, DEFAULT) returns "" when the workflow sets BRIEF_MODEL to an
    # empty secret (present-but-empty), so chain with `or` to reach the default.
    model = model or os.environ.get("BRIEF_MODEL") or DEFAULT_MODEL
    for attempt in range(2):
        try:
            resp = client.messages.create(
                model=model, max_tokens=4096,
                messages=[{"role": "user", "content": prompt}],
            )
            data = _extract_json(resp.content[0].text)
            if not isinstance(data, dict) or "tweets" not in data:
                raise ValueError("brief JSON missing tweets")
            return _normalize_brief(data)
        except Exception as e:  # noqa: BLE001
            s = str(e)
            transient = any(c in s for c in (" 529", " 503", " 502", " 504",
                                             "overloaded", "rate_limit"))
            if attempt < 1 and transient:
                continue
            log.warning("daily_brief compose failed: %s", e)
            return None
    return None


def _normalize_brief(data: dict[str, Any]) -> dict[str, Any]:
    """Coerce/sanitize the model output: fit tweets to ≤280 with brand tags,
    strip dashes from article/script, drop empties."""
    tweets = []
    for t in (data.get("tweets") or []):
        t = _sanitize(str(t))       # ensures tags present + strips dashes
        t = _fit(t)                 # ≤280, phrase-boundary trim
        if t.strip():
            tweets.append(t)
    return {
        "theme": _sanitize_plain(data.get("theme")),
        "tweets": tweets,
        "fb_article": _sanitize_plain(data.get("fb_article")),
        "video_script": _sanitize_plain(data.get("video_script")),
    }


def _sanitize_plain(text: Any) -> str:
    """Dash-strip for non-tweet copy (keeps newlines, unlike tweet _sanitize).
    Collapses the double-space an em-dash→space swap leaves behind, without
    touching line breaks."""
    s = (str(text or "")
         .replace("—", " ").replace("–", " ").replace("―", " "))
    s = re.sub(r"[ \t]{2,}", " ", s)          # collapse runs, keep newlines
    return "\n".join(line.strip() for line in s.split("\n")).strip()


# --------------------------------------------------------------------------
# 3a. Render — Notion blocks (built programmatically, not parsed)
# --------------------------------------------------------------------------

def _rt(content: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": content}}]


def _chunks(text: str, n: int = NOTION_RICHTEXT_LIMIT) -> list[str]:
    """Split text into ≤n-char pieces, preferring paragraph then newline breaks."""
    out: list[str] = []
    for para in (text or "").split("\n\n"):
        para = para.strip()
        if not para:
            continue
        while len(para) > n:
            cut = para.rfind("\n", 0, n)
            if cut < int(n * 0.5):
                cut = n
            out.append(para[:cut].strip())
            para = para[cut:].strip()
        out.append(para)
    return out


def _para(text: str) -> dict[str, Any]:
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": _rt(text)}}


def _h2(text: str) -> dict[str, Any]:
    return {"object": "block", "type": "heading_2",
            "heading_2": {"rich_text": _rt(text)}}


def _h3(text: str) -> dict[str, Any]:
    return {"object": "block", "type": "heading_3",
            "heading_3": {"rich_text": _rt(text)}}


def _todo(text: str) -> dict[str, Any]:
    return {"object": "block", "type": "to_do",
            "to_do": {"rich_text": _rt(text), "checked": False}}


def _divider() -> dict[str, Any]:
    return {"object": "block", "type": "divider", "divider": {}}


def _code(text: str) -> dict[str, Any]:
    return {"object": "block", "type": "code",
            "code": {"rich_text": _rt(text[:1990]), "language": "plain text"}}


def render_notion_blocks(brief: dict[str, Any], *, date_label: str,
                         event_count: int,
                         artwork_prompt: str = "") -> list[dict[str, Any]]:
    """Build the Notion page body for a daily brief. One to_do per tweet + one
    per long-form artifact = the operator's single approval surface."""
    blocks: list[dict[str, Any]] = []
    theme = brief.get("theme") or ""
    blocks.append({"object": "block", "type": "callout", "callout": {
        "rich_text": _rt(f"บรีฟข่าวทองประจำวัน · จาก {event_count} ข่าว breaking "
                         f"ย้อน 24 ชม. · ธีม: {theme}"),
        "icon": {"emoji": BRIEF_ICON}}})

    blocks.append(_h2("1. Twitter — tweet ที่คัดแล้ว (ติ๊กเพื่ออนุมัติ)"))
    for i, tw in enumerate(brief.get("tweets") or [], 1):
        blocks.append(_todo(f"Tweet {i}"))
        for piece in _chunks(tw):
            blocks.append(_para(piece))
    blocks.append(_divider())

    blocks.append(_h2("2. Facebook — บทความ (ติ๊กเพื่ออนุมัติ)"))
    blocks.append(_todo("อนุมัติบทความ Facebook"))
    fb_pieces = _chunks(brief.get("fb_article") or "")
    for j, piece in enumerate(fb_pieces):
        # First chunk = the scroll-stopper hook → render as a sub-heading.
        blocks.append(_h3(piece) if j == 0 else _para(piece))
    blocks.append(_divider())

    blocks.append(_h2("3. Video — บทพูด (ติ๊กเพื่ออนุมัติ)"))
    blocks.append(_todo("อนุมัติบทพูด video"))
    for piece in _chunks(brief.get("video_script") or ""):
        blocks.append(_para(piece))

    if artwork_prompt:
        blocks.append(_divider())
        blocks.append(_h2("4. Artwork — gen ใน ChatGPT แล้วลากรูปมาวางใต้นี้"))
        blocks.append(_para("คัดลอก prompt ด้านล่าง → สร้างรูปใน ChatGPT (เว็บ) → "
                            "ลากรูปที่ได้มาวางในหน้านี้ (ใต้ prompt). ตอนโพส ระบบจะ "
                            "หยิบรูปแรกในหน้านี้ไปแนบกับโพส FB + ทวีตนำ อัตโนมัติ"))
        blocks.append(_code(artwork_prompt))
    return blocks


# --------------------------------------------------------------------------
# 3b. Render — plain markdown (for the dry-run local snapshot)
# --------------------------------------------------------------------------

def render_markdown(brief: dict[str, Any], *, date_label: str,
                    event_count: int) -> str:
    lines = [f"# {BRIEF_ICON} Gold Daily Brief — {date_label} (@tradetongkam)", ""]
    lines.append(f"> จาก {event_count} ข่าว breaking ย้อน 24 ชม. · ธีม: "
                 f"{brief.get('theme','')}")
    lines += ["", "## 1. Twitter"]
    for i, tw in enumerate(brief.get("tweets") or [], 1):
        lines += [f"- [ ] **Tweet {i}**", "", tw, ""]
    lines += ["## 2. Facebook", "- [ ] อนุมัติบทความ", "",
              brief.get("fb_article") or "", ""]
    lines += ["## 3. Video (บทพูด)", "- [ ] อนุมัติบทพูด", "",
              brief.get("video_script") or ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 4. Publish — Notion API (env-gated)
# --------------------------------------------------------------------------

def post_to_notion(*, title: str, blocks: list[dict[str, Any]],
                   token: str, parent_id: str) -> dict[str, str] | None:
    """Create a Notion page under `parent_id` with `blocks`. Returns
    {"id":..., "url":...} (the id is needed later to read approval checkboxes),
    or None on failure. Notion caps children at 100 per create call; extra
    blocks are appended in follow-up PATCHes."""
    import httpx

    # Strip the token — a pasted secret often carries a trailing newline/space,
    # which makes an "Illegal header value" error (the 2026-09-17 first-live fail).
    headers = {"Authorization": f"Bearer {(token or '').strip()}",
               "Notion-Version": NOTION_VERSION,
               "Content-Type": "application/json"}
    payload = {
        "parent": {"type": "page_id", "page_id": (parent_id or "").strip()},
        "icon": {"emoji": BRIEF_ICON},
        "properties": {"title": {"title": [{"text": {"content": title}}]}},
        "children": blocks[:100],
    }
    try:
        with httpx.Client(timeout=30) as c:
            r = c.post("https://api.notion.com/v1/pages", headers=headers,
                       json=payload)
            r.raise_for_status()
            page = r.json()
            page_id = page.get("id")
            for i in range(100, len(blocks), 100):     # append the overflow
                c.patch(f"https://api.notion.com/v1/blocks/{page_id}/children",
                        headers=headers, json={"children": blocks[i:i + 100]}
                        ).raise_for_status()
            return {"id": page_id or "", "url": page.get("url") or page_id or ""}
    except httpx.HTTPStatusError as e:
        # Surface Notion's exact complaint (invalid parent, unsupported block, …).
        body = e.response.text[:600] if e.response is not None else ""
        log.error("daily_brief: Notion post failed %s: %s",
                  e.response.status_code if e.response is not None else "?", body)
        return None
    except Exception:  # noqa: BLE001 — publishing is best-effort
        log.exception("daily_brief: Notion post failed")
        return None


# --------------------------------------------------------------------------
# 5. Log — persist page id + artifacts so a later fb_post can read approval
# --------------------------------------------------------------------------

LOG_TAB = "daily_brief_log"
LOG_HEADERS = ["date", "page_id", "page_url", "fb_article", "video_script",
               "fb_posted", "video_url", "reel_posted", "artwork",
               "tweets", "tweets_posted", "notes"]


def log_brief(store, *, date_label: str, page_id: str, page_url: str,
              brief: dict[str, Any]) -> bool:
    """Append one row to daily_brief_log so `--mode fb_post` / `reel_post` /
    `tweet_post` can later read the Notion approval checkboxes for this page and
    publish. `video_url`/`artwork` are filled by their modes after they render;
    `tweets` carries the JSON list the tweet checkboxes gate. Never raises."""
    try:
        row = [date_label, page_id, page_url, brief.get("fb_article", ""),
               brief.get("video_script", ""), "", "", "", "",
               json.dumps(brief.get("tweets", []), ensure_ascii=False), "", ""]
        store.append_feed(LOG_TAB, LOG_HEADERS, [row])
        return True
    except Exception:  # noqa: BLE001
        log.exception("daily_brief: log_brief append failed")
        return False
