import json

from src import daily_brief as db
from src.utils_time import now_utc, to_ict


def _ts_recent(hours_ago=1):
    from datetime import timedelta
    return (to_ict(now_utc()) - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")


class _FakeStore:
    def __init__(self, rows):
        self._rows = rows

    def read_feed(self, tab):
        headers = ["ts_ict", "type", "tone", "impact_level",
                   "headline_th", "summary_th", "impact_th", "source"]
        return headers, self._rows


def _row(headline, typ="breaking", hours_ago=1, tone="hawkish", src="CNBC"):
    return {"ts_ict": _ts_recent(hours_ago), "type": typ, "tone": tone,
            "impact_level": "HIGH", "headline_th": headline,
            "summary_th": "รายละเอียด", "impact_th": "กดดันทอง", "source": src}


def test_collect_filters_route_and_recap():
    store = _FakeStore([
        _row("ทองร่วงหลุด 4300"),
        _row("recap ข่าว", typ="recap"),
        _row("ทดสอบ", typ="test"),
        _row("yield 10 ปีแตะ 5%", typ="alert"),
    ])
    ev = db.collect_brief_events(store)
    assert len(ev) == 2                         # recap + test dropped
    assert {e["headline_th"] for e in ev} == {"ทองร่วงหลุด 4300", "yield 10 ปีแตะ 5%"}


def test_collect_dedups_near_identical_headlines():
    store = _FakeStore([
        _row("ทองร่วงหลุด 4300 ดอลลาร์"),
        _row("ทองร่วงหลุด 4300 ดอลลาร์"),      # exact dup
        _row("BoJ เตรียมขึ้นดอกเบี้ย"),
    ])
    ev = db.collect_brief_events(store)
    assert len(ev) == 2


def test_collect_drops_outside_window():
    store = _FakeStore([
        _row("ข่าวเก่า", hours_ago=48),
        _row("ข่าวใหม่", hours_ago=2),
    ])
    ev = db.collect_brief_events(store)
    assert len(ev) == 1
    assert ev[0]["headline_th"] == "ข่าวใหม่"


def test_normalize_brief_fits_tweets_and_adds_tags():
    raw = {"theme": "Fed วันชี้ชะตา",
           "tweets": ["🔴 " + "ทองคำ " * 200, "🟡 สั้นๆ"],  # first overflows
           "fb_article": "ย่อหน้าหนึ่ง\n\nย่อหน้าสอง",
           "video_script": "[ฉาก 1] พูด"}
    out = db._normalize_brief(raw)
    assert all(len(t) <= db.__dict__.get("NOTION_RICHTEXT_LIMIT", 2000) for t in out["tweets"])
    from src.tweet_writer import TAGS, TWEET_LIMIT
    for t in out["tweets"]:
        assert t.endswith(TAGS)
        assert len(t) <= TWEET_LIMIT
    assert "—" not in out["fb_article"]


def test_chunks_respects_limit():
    long = ("ก" * 1500 + "\n\n" + "ข" * 2500)
    pieces = db._chunks(long, n=2000)
    assert all(len(p) <= 2000 for p in pieces)
    assert len(pieces) >= 2


def test_render_notion_blocks_has_todo_per_tweet_and_artifacts():
    brief = {"theme": "t", "tweets": ["🔴 a", "🟡 b", "🟢 c"],
             "fb_article": "บทความ", "video_script": "[ฉาก 1] พูด"}
    blocks = db.render_notion_blocks(brief, date_label="16 Sep 2026", event_count=5)
    todos = [b for b in blocks if b["type"] == "to_do"]
    # 3 tweet to_dos + 1 fb + 1 video = 5 approval checkboxes
    assert len(todos) == 5
    assert any(b["type"] == "callout" for b in blocks)
    assert sum(1 for b in blocks if b["type"] == "heading_2") == 3


def test_clean_source_collapses_x_relays_keeps_publications():
    assert db._clean_source("X Deitaone") == "wire"
    assert db._clean_source("X Firstsquawk") == "wire"
    assert db._clean_source("CNBC") == "CNBC"
    assert db._clean_source("FXStreet") == "FXStreet"
    # relays must not leak a citable handle into the prompt
    ev = [{"headline_th": "ทองร่วง", "impact_th": "กด", "tone": "hawkish",
           "source": "X Deitaone"}]
    assert "Deitaone" not in db._headlines_block(ev)


def test_render_notion_blocks_artwork_section():
    brief = {"theme": "t", "tweets": ["🔴 a"], "fb_article": "art",
             "video_script": "scr"}
    # with an artwork prompt → an Artwork heading + a code block carrying it
    blocks = db.render_notion_blocks(brief, date_label="d", event_count=1,
                                     artwork_prompt="CALENDAR-PROMPT-XYZ")
    codes = [b for b in blocks if b["type"] == "code"]
    assert len(codes) == 1
    assert codes[0]["code"]["rich_text"][0]["text"]["content"].startswith("CALENDAR-PROMPT")
    assert sum(1 for b in blocks if b["type"] == "heading_2") == 4
    # without a prompt → no artwork section (back to 3 headings, no code block)
    plain = db.render_notion_blocks(brief, date_label="d", event_count=1)
    assert not any(b["type"] == "code" for b in plain)
    assert sum(1 for b in plain if b["type"] == "heading_2") == 3


def test_render_markdown_smoke():
    brief = {"theme": "t", "tweets": ["🔴 a"], "fb_article": "art",
             "video_script": "scr"}
    md = db.render_markdown(brief, date_label="16 Sep 2026", event_count=3)
    assert "Twitter" in md and "Facebook" in md and "Video" in md
    assert "- [ ]" in md


class _FakeResp:
    def __init__(self, text):
        self.content = [type("C", (), {"text": text})()]


class _FakeClient:
    def __init__(self, text):
        self.messages = type("M", (), {"create": lambda self, **kw: _FakeResp(text)})()


def test_compose_brief_parses_and_normalizes():
    payload = json.dumps({
        "theme": "Fed วันชี้ชะตา",
        "tweets": ["🔴 ทองร่วง", "🟡 จับตา dot plot"],
        "fb_article": "ย่อหน้า",
        "video_script": "[ฉาก 1] สวัสดีครับ",
    })
    ev = [{"headline_th": "ทองร่วง", "impact_th": "กดดัน", "tone": "hawkish",
           "source": "CNBC"}]
    out = db.compose_brief(ev, client=_FakeClient(payload))
    assert out is not None
    assert len(out["tweets"]) == 2
    from src.tweet_writer import TAGS
    assert all(t.endswith(TAGS) for t in out["tweets"])


def test_compose_brief_none_on_garbage():
    ev = [{"headline_th": "x", "impact_th": "y", "tone": "", "source": ""}]
    assert db.compose_brief(ev, client=_FakeClient("not json")) is None


def test_compose_brief_none_without_events():
    assert db.compose_brief([], client=_FakeClient("{}")) is None
