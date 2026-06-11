"""Compose a @tradetongkam-voice Thai tweet from a news item's context.

INDEPENDENT from the LINE rewrite (news_alert): the LINE flex cards keep their
structured headline/bullets/impact untouched. This module takes the SAME news
context and writes a separate, free-standing tweet in the brand's own
announcement voice for the social_feed. Best-effort — returns None on any
failure so social_feed falls back to its simple template.

Voice anchored on real @tradetongkam tweets (style copied, never content):
analytical, actor-led opening, explains the stakes, never blurts "gold up/down",
no emoji, no links, no source, brand hashtags.
"""
from __future__ import annotations

import json
import logging

from .translator import _get_anthropic_client

log = logging.getLogger("tweet_writer")

# Brand hashtag line for NEWS posts (their #ข่าวทอง set, not #XAUUSD).
TAGS = "#ทองวันนี้ #ข่าวทอง #เทรดทอง #ทองคำ"
TWEET_LIMIT = 280

_PROMPT = """You write Thai tweets for @tradetongkam, a gold (XAU/USD) trading brand.
Given one news item, write ONE tweet in the brand's exact voice.

Study these REAL @tradetongkam tweets. Copy the STYLE and rhythm, never the content:
---
ทรัมป์กดดันเนทันยาฮู ไม่ให้ตอบโต้กลับในทันที เหตุผลมีข้อเดียว เพราะดีลสันติภาพกับอิหร่าน กำลังใกล้ที่สุดในรอบหลายเดือน ปัญหาคือ อิหร่านต้องการให้การหยุดยิงในเลบานอน เป็นส่วนหนึ่งของข้อตกลง แต่อิสราเอลยังมองว่า ฮิซบอลเลาะห์คืออีกสงครามหนึ่ง
---
มีรายงานว่าทรัมป์ไม่พอใจอย่างหนัก ต่อแผนโจมตีเลบานอนของอิสราเอล เพราะมองว่าอาจทำลายโอกาสการเจรจายุติสงครามในภูมิภาค ประเด็นนี้สำคัญ เพราะครั้งนี้ สหรัฐฯ อาจกำลังส่งสัญญาณว่า การควบคุมความขัดแย้งสำคัญกว่าการขยายแนวรบ
---
สหรัฐฯ โจมตีเรดาร์ชายฝั่งอิหร่าน หลังยิงสกัดโดรนได้ 4 ลำ ที่ถูกมองว่ามุ่งเป้าการเดินเรือในช่องแคบฮอร์มุซ จุดสำคัญคือ แม้ทรัมป์บอกว่าศักยภาพโดรนและขีปนาวุธอิหร่านถูกทำลายไปมาก แต่อิหร่านยังมีไพ่เหลืออยู่
---

VOICE RULES (follow exactly):
- Open with the actor + action, directly: e.g. "ทรัมป์กดดัน...", "สหรัฐฯ โจมตี...", "เฟดส่งสัญญาณ...", "จีนเพิ่มทุนสำรองทอง...". No preamble, no "วันนี้มีข่าวว่า", no "ล่าสุด".
- Then explain the SIGNIFICANCE / tension analytically with connectors like "เหตุผลคือ", "ปัญหาคือ", "ประเด็นสำคัญคือ", "จุดที่ต้องจับตาคือ", "เพราะ...".
- Do NOT blurt "ทองขึ้น" / "ทองลง". Explain the situation and let the reader connect it to gold. You MAY weave in safe-haven / ดอลลาร์ / บอนด์ยีลด์ / เฟด when it genuinely fits.
- Confident trader Thai. Tight. No hype, no clickbait, no question marks, no quotes from the article.
- NO emoji anywhere. NO links. NO source names. NO em-dash (—).
- Keep the ENTIRE tweet including the hashtag line under 270 Thai characters.
- End with exactly this final line: {TAGS}

NEWS CONTEXT:
Category: {category}
Thai headline: {headline_th}
Thai detail: {body}
English title: {en_title}
English summary: {en_summary}

Return ONLY JSON, nothing else: {"tweet": "<full tweet text including the hashtag line>"}"""


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        pass
    a, b = text.find("{"), text.rfind("}")
    if a >= 0 and b > a:
        try:
            return json.loads(text[a:b + 1])
        except Exception:  # noqa: BLE001
            return None
    return None


def _sanitize(t: str) -> str:
    t = (t or "").replace("—", " ").replace("–", " ").replace("―", " ").strip()
    # Ensure the brand hashtag line is present exactly once at the end.
    if TAGS not in t:
        t = t.rstrip() + "\n" + TAGS
    return t


def compose_tweet(*, headline_th: str | None, body_th: list[str] | None,
                  impact_th: str | None, category: str | None,
                  en_title: str | None, en_summary: str | None) -> str | None:
    """Return a @tradetongkam-voice Thai tweet, or None if Claude is unavailable
    or the call fails (caller falls back to the simple template)."""
    client = _get_anthropic_client()
    if not client:
        return None
    body = " ".join(body_th or [])
    if impact_th:
        body = (body + " " + impact_th).strip()
    prompt = (
        _PROMPT
        .replace("{TAGS}", TAGS)
        .replace("{category}", category or "")
        .replace("{headline_th}", headline_th or "")
        .replace("{body}", body[:600])
        .replace("{en_title}", (en_title or "")[:300])
        .replace("{en_summary}", (en_summary or "")[:600])
    )
    for attempt in range(2):
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}],
            )
            d = _extract_json(resp.content[0].text)
            tweet = (d or {}).get("tweet") if isinstance(d, dict) else None
            if not tweet:
                raise ValueError("no tweet field in Claude output")
            tweet = _sanitize(str(tweet))
            if len(tweet) > TWEET_LIMIT:
                # Trim the body but keep the hashtag line intact.
                head = tweet[: -len(TAGS)].rstrip()
                room = TWEET_LIMIT - len(TAGS) - 2
                tweet = head[: max(0, room - 1)].rstrip() + "…\n" + TAGS
            return tweet
        except Exception as e:  # noqa: BLE001
            s = str(e)
            transient = any(c in s for c in (" 529", " 503", " 502", " 504", "overloaded", "rate_limit"))
            if attempt < 1 and transient:
                continue
            log.warning("compose_tweet failed: %s", e)
            return None
    return None
