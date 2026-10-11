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
import random
import re

from .translator import _get_anthropic_client

log = logging.getLogger("tweet_writer")

# Brand hashtag line for NEWS posts (their #ข่าวทอง set, not #XAUUSD).
TAGS = "#ทองวันนี้ #ข่าวทอง #เทรดทอง #ทองคำ"
TWEET_LIMIT = 280

# Auto-posts (squawk events mode) rotate the hashtag line so the feed doesn't
# carry the identical 4-tag block on every post (a bot tell, 2026-10-11 review).
# CORE tags always stay (they carry the search views); 2 extras are picked per
# post — topic tags first when the story matches, the rest from the brand pool.
CORE_TAGS = ("#ทองวันนี้", "#ทองคำ")
POOL_TAGS = ("#ข่าวทอง", "#เทรดทอง", "#ราคาทอง", "#ลงทุนทองคำ", "#ข่าวเศรษฐกิจ")
TOPIC_TAGS = (
    (re.compile(r"fed|fomc|เฟด|powell|พาวเวล", re.I), "#เฟด"),
    (re.compile(r"ดอลลาร์|dollar|dxy", re.I), "#ดอลลาร์"),
    (re.compile(r"เงินเฟ้อ|inflation|\bcpi\b|\bpce\b", re.I), "#เงินเฟ้อ"),
    (re.compile(r"ดอกเบี้ย|rate (hike|cut)|yields?\b|ยีลด์", re.I), "#ดอกเบี้ย"),
    (re.compile(r"อิหร่าน|อิสราเอล|iran|israel|สงคราม|ภูมิรัฐศาสตร์|hormuz|ฮอร์มุซ", re.I), "#ภูมิรัฐศาสตร์"),
)


def pick_tags(text: str = "", rng: random.Random | None = None) -> str:
    """CORE tags + 2 rotating extras (max 1 topic tag, rest from POOL_TAGS),
    extras shuffled so consecutive posts don't share one fixed line."""
    rng = rng or random
    topics = [t for rx, t in TOPIC_TAGS if rx.search(text or "")]
    extras = [rng.choice(topics)] if topics else []
    extras += rng.sample([t for t in POOL_TAGS if t not in extras], 2 - len(extras))
    rng.shuffle(extras)
    return " ".join(CORE_TAGS[:1] + tuple(extras) + CORE_TAGS[1:])


# The composer is told to SKIP weak-gold-link stories, but in the 2026-10-05..09
# live posts it still wrote ~1 in 6 that end by admitting the link is weak
# ("ผลต่อทองยังเล็กน้อย", "ผลต่อทองมีอยู่แต่ไม่ใช่แรงขับหลัก"). Deterministic
# backstop: an auto-post containing such a phrase is SKIPped, not posted.
_WEAK_LINK = re.compile(
    r"(ผล|ผลกระทบ|นัย)(ต่อ|กับ)?(ราคา)?ทอง[^\n#]{0,30}?"
    r"(เล็กน้อย|จำกัด|ไม่มาก|ไม่ชัด|ไม่ใช่(ปัจจัย|แรงขับ|ตัวขับ|ตัวแปร)(หลัก)?|ไม่ได้เป็น(ปัจจัย|แรงขับ)หลัก)"
    r"|ทอง[^\n#]{0,20}?ทางอ้อม[^\n#]{0,15}?(เล็กน้อย|จำกัด)")


def is_weak_link(tweet: str) -> bool:
    return bool(_WEAK_LINK.search(tweet or ""))

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


def _sanitize(t: str, tags: str = TAGS) -> str:
    t = (t or "").replace("—", " ").replace("–", " ").replace("―", " ").strip()
    if tags != TAGS:
        # Rotated tags: drop the model's own hashtag line(s) and use ours.
        lines = t.split("\n")
        while lines and lines[-1].strip().startswith("#"):
            lines.pop()
        t = "\n".join(lines).rstrip()
    # Ensure the brand hashtag line is present exactly once at the end.
    if tags not in t:
        t = t.rstrip() + "\n" + tags
    return t


def _fit(tweet: str, tags: str = TAGS) -> str:
    """Ensure the tweet fits TWEET_LIMIT, trimming the body (never the hashtag
    line) at a PHRASE boundary so it never ends mid-word. Thai separates phrases
    with spaces, so we back off to the last space/newline/sentence mark rather
    than a hard character cut — the 2026-09 defect where drafts ended mid-word
    (e.g. 'ทองหา…', 'จุดสำคัญ…'). Adds '…' only when content was actually cut."""
    if len(tweet) <= TWEET_LIMIT:
        return tweet
    head = tweet[: -len(tags)].rstrip() if tweet.endswith(tags) else tweet
    room = TWEET_LIMIT - len(tags) - 2          # head + "…\n" + tags
    if room <= 0:
        return tags
    clipped = head[:room]
    # Back off to the last phrase boundary; only honor it if it keeps enough of
    # the sentence (>=60% of room) so we don't collapse to a stub.
    cut = max(clipped.rfind(" "), clipped.rfind("\n"),
              clipped.rfind("."), clipped.rfind("ฯ"))
    if cut >= int(room * 0.6):
        clipped = clipped[:cut]
    clipped = clipped.rstrip(" \n.,")
    return clipped + "…\n" + tags


DEFAULT_MODEL = "claude-haiku-4-5-20251001"


SKIP = "SKIP"

# Extra rules for UNREVIEWED auto-posts (squawk events mode), from the
# 2026-10-02 review of 77 live posts: a guessed rate direction ("RBA คงดอกเบี้ย"
# for a 'meets forecasts' hike), a tweet about Tokyo CPI under an NFP headline
# (an FXStreet roundup whose summary led with another story), ~1/3 of posts
# closing "กดดันทองในระยะสั้น", 4 posts clipped mid-sentence, and ~10
# "ผลต่อทองจำกัด" posts spending the daily quota on non-gold news.
_AUTOPOST_RULES = """
EXTRA RULES (this post goes out automatically, nobody reviews it):
- ข้อเท็จจริงต้องมาจาก NEWS CONTEXT เท่านั้น. ห้ามเดาว่าธนาคารกลาง "ขึ้น/คง/ลด" ดอกเบี้ย
  ถ้าข้อมูลไม่ได้ระบุชัด. "meets forecasts" / "in line" แปลว่า "ตรงคาด" ไม่ได้แปลว่า "คงดอกเบี้ย"
  ในกรณีนั้นให้รายงานแค่ตัวเลขและว่าตรงคาด.
- เรื่องหลักของโพสต์ต้องเป็นเรื่องใน English title. ถ้า Thai headline/detail พูดถึงเรื่องอื่น
  (เช่นบทสรุปตลาดหลายเรื่อง) ให้ยึด English title แล้วใช้ข้อมูลส่วนที่เกี่ยวกับเรื่องนั้นเท่านั้น.
  ถ้า Thai headline/detail ไม่มีข้อมูลเรื่องเดียวกับ English title เลย (เช่น title พูดถึงรายงาน
  การจ้างงานสหรัฐฯ แต่ detail พูดถึง CPI ญี่ปุ่น) ห้ามเขียน ให้ตอบ {"tweet": "SKIP"}.
- ห้ามปิดท้ายด้วยวลีสำเร็จรูปซ้ำๆ. โพสต์ล่าสุดของแบรนด์ปิดแบบนี้ไปแล้ว ห้ามใช้ซ้ำหรือใกล้เคียง:
{AVOID}
- เนื้อหา (ไม่รวมบรรทัดแฮชแท็ก) ไม่เกิน 200 ตัวอักษร และต้องจบประโยคสมบูรณ์.
- ถ้าข่าวนี้ไม่มีผลต่อทองคำอย่างมีนัยสำคัญ (คือคุณจะต้องเขียนว่า "ผลต่อทองจำกัด" / "ผลต่อทองเล็กน้อย" /
  "ผลต่อทองมีแต่ไม่ใช่ปัจจัยหลัก" หรือความหมายเดียวกัน) ห้ามเขียนโพสต์ ให้ตอบ {"tweet": "SKIP"} แทน.
  แบรนด์โพสต์เฉพาะข่าวที่ขยับทองได้จริง. ข่าวธนาคารกลางอื่น (BoJ/BoE/ECB/BoC) ที่ไม่กระทบดอลลาร์
  หรือ yields สหรัฐฯ ชัดเจน มักเข้าข่ายนี้.
"""


def compose_tweet(*, headline_th: str | None, body_th: list[str] | None,
                  impact_th: str | None, category: str | None,
                  en_title: str | None, en_summary: str | None,
                  model: str = DEFAULT_MODEL, autopost: bool = False,
                  avoid_closings: list[str] | None = None) -> str | None:
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
    if autopost:
        avoid = "\n".join(f"  · {c}" for c in (avoid_closings or [])[:8]) or "  · (none yet)"
        prompt = prompt.replace("\nNEWS CONTEXT:", _AUTOPOST_RULES.replace("{AVOID}", avoid)
                                + "\nNEWS CONTEXT:")
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
            if autopost and str(tweet).strip().upper() == SKIP:
                return SKIP
            if autopost and is_weak_link(str(tweet)):
                log.info("compose_tweet: weak gold link phrase -> SKIP")
                return SKIP
            tags = pick_tags(f"{en_title or ''} {headline_th or ''} {tweet}") if autopost else TAGS
            tweet = _sanitize(str(tweet), tags)
            if autopost and len(tweet) > TWEET_LIMIT:
                # Never ship a mid-sentence "…" clip on an unreviewed post: ask
                # for a complete shorter version once, clip only as a last resort.
                tweet = _shorten(client, model, tweet, tags) or tweet
            return _fit(tweet, tags)
        except Exception as e:  # noqa: BLE001
            s = str(e)
            transient = any(c in s for c in (" 529", " 503", " 502", " 504", "overloaded", "rate_limit"))
            if attempt < 1 and transient:
                continue
            log.warning("compose_tweet failed: %s", e)
            return None
    return None


def _shorten(client, model: str, tweet: str, tags: str = TAGS) -> str | None:
    """One follow-up call: rewrite an over-long post into complete sentences
    under the limit. None on failure (caller falls back to _fit's clip)."""
    try:
        resp = client.messages.create(
            model=model, max_tokens=400,
            messages=[{"role": "user", "content": (
                "ย่อโพสต์ภาษาไทยนี้ให้เนื้อหา (ไม่รวมบรรทัดแฮชแท็ก) ไม่เกิน 190 ตัวอักษร "
                "จบประโยคสมบูรณ์ คงข้อเท็จจริงเดิมทั้งหมด ห้ามเพิ่มข้อมูลใหม่ ห้ามใช้ em-dash. "
                f"บรรทัดสุดท้ายต้องเป็น: {tags}\n\nโพสต์:\n{tweet}\n\n"
                'Return ONLY JSON: {"tweet": "<post>"}')}],
        )
        d = _extract_json(resp.content[0].text)
        t = (d or {}).get("tweet") if isinstance(d, dict) else None
        return _sanitize(str(t), tags) if t else None
    except Exception as e:  # noqa: BLE001 — best-effort
        log.warning("tweet shorten failed: %s", e)
        return None
