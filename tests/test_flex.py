"""Structural sanity checks for Flex builders.

We don't validate against the full LINE schema — just confirm the shape is what
LINE accepts: type bubble/carousel, header+body present, contents non-empty,
url buttons capped, etc.
"""
from __future__ import annotations

from datetime import datetime, timezone

from src.dedup import Event
from src.line_flex import (
    alert_bubble,
    alt_text_for_event,
    breaking_bubble,
    health_bubble,
    news_update_carousel,
)
from src.news_alert import MarketAlert
from src.normalizer import Item


def _item(sid: str, title: str, url: str = "", summary: str = "") -> Item:
    ts = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    return Item(source_id=sid, tier=1, role="macro", title=title, summary=summary,
                url=url or f"https://x/{sid}/{abs(hash(title))%9999}",
                published_ts=ts, first_seen_ts=ts)


def _ev(topic: str, direction: str, sources: list[str], title: str = "Hot CPI", summary: str = "summary") -> Event:
    items = [_item(s, f"{title} from {s}", summary=summary) for s in sources]
    return Event(event_id="e1", cluster_key="k", topic_bucket=topic,
                 entity="us", direction_label=direction, items=items)


def test_breaking_bubble_shape(kw_config):
    ev = _ev("inflation", "hawkish", ["forexlive", "bls"])
    b = breaking_bubble(ev, 5.0, kw_config)
    assert b["type"] == "bubble"
    assert "header" in b and "body" in b
    assert b["header"]["backgroundColor"] == "#DC2626"
    body_contents = b["body"]["contents"]
    assert len(body_contents) >= 3
    # Source link is anywhere in body — find any text component that's
    # clickable (has uri action). Emoji prefix was removed, so we just look
    # for the uri action.
    src_link = None
    def _walk(node):
        nonlocal src_link
        if src_link is not None:
            return
        if isinstance(node, dict):
            if node.get("type") == "text" and node.get("action", {}).get("type") == "uri":
                src_link = node
                return
            for c in node.get("contents", []) or []:
                _walk(c)
    for comp in body_contents:
        _walk(comp)
    assert src_link is not None, "expected clickable source link in body"
    assert src_link["action"]["uri"].startswith("https://")


def test_alert_bubble_shape(kw_config):
    ev = _ev("rate_policy", "dovish", ["fed"])
    b = alert_bubble(ev, 3.6, kw_config)
    assert b["type"] == "bubble"
    assert b["header"]["backgroundColor"] == "#D97706"


def _first_text(body_contents):
    """First text component (skips a chip row box if present)."""
    for c in body_contents:
        if c.get("type") == "text":
            return c
    return None


def test_alert_hides_other_and_neutral_chips(kw_config):
    # Fallback-style event: category "Other" + direction "neutral" → both chips
    # should be hidden, and the headline becomes the first body element.
    ev = _ev("other", "neutral", ["forexlive"], title="ทดสอบข่าว", summary="")
    body = alert_bubble(ev, 3.6, kw_config)["body"]["contents"]
    # Chip row (a leading horizontal box) is suppressed → headline text is first.
    assert body[0]["type"] == "text", "Other/neutral chip row should be hidden"
    assert "ทดสอบข่าว" in body[0]["text"]


def test_alert_headline_not_bold(kw_config):
    ev = _ev("other", "neutral", ["forexlive"], title="ทดสอบ", summary="")
    title = _first_text(alert_bubble(ev, 3.6, kw_config)["body"]["contents"])
    assert title is not None and title.get("weight") != "bold"


def test_alert_keeps_informative_chips(kw_config):
    # Real signal — Inflation / hawkish — both chips must still render.
    ev = _ev("inflation", "hawkish", ["bls"])
    body = breaking_bubble(ev, 5.0, kw_config)["body"]["contents"]
    assert body[0].get("type") == "box" and body[0].get("layout") == "horizontal", \
        "informative category/direction should still show a chip row"


def test_strip_filler_removes_samat():
    from src.line_flex import _strip_filler
    assert _strip_filler("ทองคำสามารถปรับขึ้น") == "ทองคำปรับขึ้น"
    assert _strip_filler("a  b   c") == "a b c"
    assert _strip_filler("") == ""


def test_compact_th_abbreviations():
    from src.line_flex import _compact_th
    out = _compact_th("ประธานาธิบดีทรัมป์ประกาศ")
    assert "ปธน." in out and "ประธานาธิบดี" not in out
    assert _compact_th("นายกรัฐมนตรีญี่ปุ่น") == "นายกฯญี่ปุ่น"
    assert "สหรัฐฯ" in _compact_th("สหรัฐอเมริกา")
    # longest-first: รัฐมนตรีว่าการกระทรวง → รมว. (not รมต.การ...)
    assert "รมว." in _compact_th("รัฐมนตรีว่าการกระทรวงการคลังกล่าว")
    # standalone รัฐมนตรี → รมต.
    assert _compact_th("รัฐมนตรีต่างประเทศ") == "รมต.ต่างประเทศ"
    # still strips filler, and is idempotent
    assert "สามารถ" not in _compact_th("ทองสามารถขึ้น")
    assert _compact_th("ปธน.ทรัมป์") == "ปธน.ทรัมป์"
    assert _compact_th("") == ""


def test_compact_th_no_substring_mangle():
    from src.line_flex import _compact_th
    # The dangerous case: ธนาคารกลางสหรัฐอเมริกา must become Fed cleanly, NOT
    # "Fedอเมริกา" (which a naive order would produce).
    assert _compact_th("ธนาคารกลางสหรัฐอเมริกาคงดอกเบี้ย") == "Fedคงดอกเบี้ย"
    assert _compact_th("ธนาคารกลางสหรัฐฯขึ้นดอกเบี้ย") == "Fedขึ้นดอกเบี้ย"
    assert _compact_th("ธนาคารกลางสหรัฐส่งสัญญาณ") == "Fedส่งสัญญาณ"
    # plain country name is NOT mistaken for the central bank
    assert _compact_th("เศรษฐกิจสหรัฐอเมริกาชะลอ") == "เศรษฐกิจสหรัฐฯชะลอ"


def test_compact_th_leader_names():
    from src.line_flex import _compact_th
    # drop given name → surname
    assert _compact_th("วลาดิเมียร์ ปูตินกล่าว") == "ปูตินกล่าว"
    assert _compact_th("โดนัลด์ ทรัมป์ประกาศ") == "ทรัมป์ประกาศ"
    assert _compact_th("โวโลดิมีร์ เซเลนสกีเรียกร้อง") == "เซเลนสกีเรียกร้อง"
    # variant transliterations normalised to canonical
    assert _compact_th("พูตินเตือน") == "ปูตินเตือน"
    assert _compact_th("เซเลนสกี้") == "เซเลนสกี"
    assert _compact_th("สีจิ้นผิงพบผู้นำ") == "สี จิ้นผิงพบผู้นำ"
    # full + variant combined collapses to canonical surname
    assert _compact_th("โวโลดิมีร์ เซเลนสกี้") == "เซเลนสกี"
    # already canonical → unchanged (idempotent)
    assert _compact_th("ปูตินพบสี จิ้นผิง") == "ปูตินพบสี จิ้นผิง"


def test_compact_th_orgs_and_data_terms():
    from src.line_flex import _compact_th
    assert _compact_th("ธนาคารกลางญี่ปุ่น") == "BoJ"
    assert _compact_th("ธนาคารกลางยุโรป") == "ECB"
    assert _compact_th("สหประชาชาติเรียกร้อง") == "UNเรียกร้อง"
    assert _compact_th("ดัชนีราคาผู้บริโภคพื้นฐาน") == "CPIพื้นฐาน"
    assert _compact_th("การจ้างงานนอกภาคเกษตร") == "NFP"
    assert _compact_th("ผลิตภัณฑ์มวลรวมภายในประเทศ") == "GDP"
    # "รอง" prefix preserved automatically
    assert _compact_th("รองประธานาธิบดี") == "รองปธน."


def _card(topic="inflation", tone="hawkish", score=3.0, headline="CPI ร้อนแรง",
          body=None, impact="กดดันทอง", category="Inflation",
          sources=None, url="https://x/a"):
    """Build a news-update card dict (the new 1-event-per-bubble input)."""
    alert = MarketAlert(
        action="keep", relevance_to_gold="high", tone=tone, category=category,
        headline_th=headline,
        body_th=body if body is not None else ["รายละเอียดบรรทัดหนึ่ง", "รายละเอียดบรรทัดสอง"],
        impact_th=impact,
    )
    return {
        "alert": alert, "score": score, "topic_bucket": topic,
        "source_list": sources or ["forexlive", "bls"], "url": url,
        "first_seen": datetime(2026, 6, 22, 5, 0, tzinfo=timezone.utc),
    }


def _has_uri_link(bubble):
    found = {"v": False}

    def _walk(node):
        if found["v"] or not isinstance(node, dict):
            return
        if node.get("type") == "text" and node.get("action", {}).get("type") == "uri":
            found["v"] = True
            return
        for c in node.get("contents", []) or []:
            _walk(c)
    for comp in bubble["body"]["contents"]:
        _walk(comp)
    return found["v"]


def test_news_update_single_bubble():
    b = news_update_carousel([_card()], "12:30")
    assert b is not None
    # One event → a single full-detail bubble, digest blue.
    assert b["type"] == "bubble"
    assert b["size"] == "giga"
    assert b["header"]["backgroundColor"] == "#2563EB"
    # Body carries the headline + the full body bullets + a clickable source.
    texts = [c.get("text", "") for c in b["body"]["contents"] if c.get("type") == "text"]
    assert any("CPI ร้อนแรง" in t for t in texts)
    assert any("รายละเอียดบรรทัดหนึ่ง" in t for t in texts)
    assert _has_uri_link(b)


def test_news_update_one_bubble_per_event():
    cards = [_card(topic=t, headline=f"ข่าว {t}", score=3.0 + i * 0.1)
             for i, t in enumerate(["inflation", "jobs", "rate_policy"])]
    b = news_update_carousel(cards, "16:30")
    assert b["type"] == "carousel"
    # 3 events → 3 bubbles, ONE event each (not packed).
    assert len(b["contents"]) == 3
    for bub in b["contents"]:
        assert bub["type"] == "bubble"


def test_news_update_caps_at_four():
    cards = [_card(headline=f"ข่าว {i}", score=3.0) for i in range(10)]
    b = news_update_carousel(cards, "12:30")
    assert b["type"] == "carousel"
    assert len(b["contents"]) == 4   # NEWS_MAX_CARDS


def test_news_update_empty_is_none():
    assert news_update_carousel([], "12:30") is None
    # cards missing an alert are dropped
    assert news_update_carousel([{"score": 3.0}], "12:30") is None


def _header_texts(bubble):
    """Collect text strings under a bubble's header box."""
    out = []

    def _walk(node):
        if isinstance(node, dict):
            if node.get("type") == "text":
                out.append(node.get("text", ""))
            for c in node.get("contents", []) or []:
                _walk(c)
    _walk(bubble.get("header", {}))
    return out


def test_news_update_degraded_marks_single_bubble():
    # Normal round: no "สำรอง" marker in the header.
    normal = news_update_carousel([_card()], "20:30")
    assert not any("สำรอง" in t for t in _header_texts(normal))
    # Degraded (classifier-outage fallback) round: header flags backup quality.
    degraded = news_update_carousel([_card()], "20:30", degraded=True)
    assert any("สำรอง" in t for t in _header_texts(degraded))


def test_news_update_degraded_marks_every_carousel_bubble():
    cards = [_card(headline=f"ข่าว {i}", score=3.0) for i in range(3)]
    b = news_update_carousel(cards, "12:30", degraded=True)
    assert b["type"] == "carousel"
    # Every bubble in the round carries the backup marker, not just the first.
    for bub in b["contents"]:
        assert any("สำรอง" in t for t in _header_texts(bub))


def test_health_bubble_shape():
    b = health_bubble([("forexlive", "tier2_no_item"), ("bls", "tier0_event_day_no_success")])
    assert b["type"] == "bubble"
    assert b["header"]["backgroundColor"] == "#6B7280"
    assert len(b["body"]["contents"]) == 2


def test_alt_text_truncates():
    ev = _ev("inflation", "hawkish", ["forexlive"], title="x" * 1000)
    t = alt_text_for_event("⚡ BREAKING", ev, 5.0)
    assert len(t) <= 380


def test_safe_http_url_allows_only_web_schemes():
    from src.line_flex import _safe_http_url
    assert _safe_http_url("https://example.com/article")
    assert _safe_http_url("http://example.com")
    assert _safe_http_url("  HTTPS://Example.com  ")  # trimmed + case-insensitive
    # rejected: non-web schemes + empties (audit M5 — scraped URLs are untrusted)
    assert not _safe_http_url("javascript:alert(1)")
    assert not _safe_http_url("mailto:x@y.com")
    assert not _safe_http_url("tel:123")
    assert not _safe_http_url("ftp://host/f")
    assert not _safe_http_url(None)
    assert not _safe_http_url("")
