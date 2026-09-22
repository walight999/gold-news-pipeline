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

HOW TO WRITE (this is the whole point — read it):
You are a Thai market analyst, NOT a translator. Write natural Thai that a Thai
reader understands in ONE read. Never translate English word-for-word.

Each post answers these three, IN THIS ORDER — but only the ones that actually apply:
1) เกิดอะไรขึ้น — the fact, said directly.
2) ตลาดควรสนใจประเด็นใด — what the market will watch or price in.
3) เกี่ยวกับทองคำอย่างไร — the gold / safe-haven / ดอลลาร์ / บอนด์ยีลด์ / เฟด angle, IF a real one exists.

RULES:
- ประโยคสั้น กระชับ: ประธาน + กริยา + นัยต่อตลาด. เลี่ยงประโยคซ้อนหลายชั้น. รวม 2-4 ประโยค.
- อย่าใช้โครงประโยคซ้ำเดิมทุกโพสต์. โดยเฉพาะ อย่าเปิดประโยคที่สองด้วย "เหตุผลคือ" /
  "ประเด็นสำคัญคือ" / "จุดที่ต้องจับตาคือ" เป็นสูตรตายตัว — บางโพสต์ไม่ต้องอธิบายเหตุผลเลย.
- สลับวิธีเปิดเรื่องในแต่ละโพสต์: บางอันเปิดด้วยผู้กระทำ+การกระทำ ("ทรัมป์ประกาศ…"),
  บางอันเปิดด้วยธีม ("จับตาอิหร่าน–จีน หลัง…"), บางอันเปิดด้วยความเสี่ยง
  ("ความเสี่ยงภูมิรัฐศาสตร์เพิ่มอีกจุด หลัง…").
- อย่าอธิบายสิ่งที่ผู้อ่านเข้าใจอยู่แล้ว. อย่าตีความข่าวชิ้นเดียวให้เป็น narrative ใหญ่
  (เช่น "สหรัฐฯ อาจหันไปนโยบายแยกตัว") เว้นแต่ต้นทางระบุชัดเจน.
- อย่า blurt "ทองขึ้น"/"ทองลง" ให้ผู้อ่านเชื่อมโยงเอง. ถ้าข่าวไม่กระทบทองอย่างมีนัยสำคัญ
  ให้บอกสั้นๆ ว่า "ผลต่อทองจำกัด" แทนการฝืนเชื่อมโยง.
- ห้ามคำ/วลีแปลตรงตัวที่คนไทยไม่พูด (เช่น "ขจัดสถานการณ์ข่มขี่", "การรีบาลานซ์อำนาจชาติ",
  "เป็นตัวจริง", "สำเร็จการศึกษา"). ไม่มั่นใจว่าคำไหนฟังธรรมชาติ ให้ตัดทิ้งแล้วเขียนใหม่ด้วยคำง่ายๆ.
- Confident trader Thai. ไม่มี hype, ไม่ clickbait, ไม่มีเครื่องหมายคำถาม, ไม่ยกคำพูดตรงจากข่าว.
- NO emoji. NO links. NO source names. NO em-dash (—).
- สาระมาก่อน แล้วปิดท้ายด้วยบรรทัดแฮชแท็ก. Keep the ENTIRE post incl. hashtags under 270 Thai characters.
- End with exactly this final line: {TAGS}

FINAL CHECK before you return: อ่านทุกประโยคแล้วถามตัวเองว่า "คนไทยอ่านครั้งเดียวเข้าใจไหม?"
ถ้าไม่ ให้เขียนใหม่ด้วยภาษาที่ง่ายกว่า. เกณฑ์คุณภาพ: 5 วินาที รู้ว่าเกิดอะไร ·
10 วินาที รู้ว่ากระทบตลาด/ทองยังไง · ไม่ต้องอ่านซ้ำ.

NEWS CONTEXT:
Category: {category}
Thai headline: {headline_th}
Thai detail: {body}
English title: {en_title}
English summary: {en_summary}

Return ONLY JSON, nothing else: {"tweet": "<full post text including the hashtag line>"}"""


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


def _fit(tweet: str) -> str:
    """Ensure the tweet fits TWEET_LIMIT, trimming the body (never the hashtag
    line) at a PHRASE boundary so it never ends mid-word. Thai separates phrases
    with spaces, so we back off to the last space/newline/sentence mark rather
    than a hard character cut — the 2026-09 defect where drafts ended mid-word
    (e.g. 'ทองหา…', 'จุดสำคัญ…'). Adds '…' only when content was actually cut."""
    if len(tweet) <= TWEET_LIMIT:
        return tweet
    head = tweet[: -len(TAGS)].rstrip() if tweet.endswith(TAGS) else tweet
    room = TWEET_LIMIT - len(TAGS) - 2          # head + "…\n" + tags
    if room <= 0:
        return TAGS
    clipped = head[:room]
    # Back off to the last phrase boundary; only honor it if it keeps enough of
    # the sentence (>=60% of room) so we don't collapse to a stub.
    cut = max(clipped.rfind(" "), clipped.rfind("\n"),
              clipped.rfind("."), clipped.rfind("ฯ"))
    if cut >= int(room * 0.6):
        clipped = clipped[:cut]
    clipped = clipped.rstrip(" \n.,")
    return clipped + "…\n" + TAGS


DEFAULT_MODEL = "claude-haiku-4-5-20251001"


def compose_tweet(*, headline_th: str | None, body_th: list[str] | None,
                  impact_th: str | None, category: str | None,
                  en_title: str | None, en_summary: str | None,
                  model: str = DEFAULT_MODEL) -> str | None:
    """Return a @tradetongkam-voice Thai tweet, or None if Claude is unavailable
    or the call fails (caller falls back to the simple template).

    `model` defaults to Haiku (cheap, high-volume social_feed drafts). Public
    auto-posted channels (squawk_mirror) pass Sonnet: Haiku garbles Thai in
    free-standing copy (the daily_brief lesson — เญาปี่/สำเร็จการศึกษา), and these
    posts ship with no human review, so fluent Thai matters more than the cents."""
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
                model=model,
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}],
            )
            d = _extract_json(resp.content[0].text)
            tweet = (d or {}).get("tweet") if isinstance(d, dict) else None
            if not tweet:
                raise ValueError("no tweet field in Claude output")
            tweet = _sanitize(str(tweet))
            return _fit(tweet)
        except Exception as e:  # noqa: BLE001
            s = str(e)
            transient = any(c in s for c in (" 529", " 503", " 502", " 504", "overloaded", "rate_limit"))
            if attempt < 1 and transient:
                continue
            log.warning("compose_tweet failed: %s", e)
            return None
    return None
