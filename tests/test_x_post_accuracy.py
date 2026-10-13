"""X autopost accuracy + variety (2026-10-13 review of 61 live posts)."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from src import squawk_mirror as sq
from src import tweet_writer as tw

UTC = timezone.utc
NOW = datetime(2026, 10, 8, 13, 0, tzinfo=UTC)
CFG = {"cap_per_hour": 99, "cap_per_day": 99, "max_items": 9,
       "sessions": [{"name": "all", "start": 0, "cap": 99}]}


class Store:
    def __init__(self, rows=None):
        self.rows = list(rows or [])

    def read_feed(self, tab):
        return sq.MIRROR_HEADERS, [dict(r) for r in self.rows]

    def append_feed(self, tab, headers, rows):
        self.rows += [dict(zip(headers, r)) for r in rows]


def row(title, hours_ago, tweet="ข่าว\n#ทองวันนี้ #ทองคำ"):
    ts = NOW - timedelta(hours=hours_ago)
    return {"ts_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "ts_ict": "", "fs_id": f"ev:{title[:8]}",
            "fs_text": title, "tweet_text": tweet, "posted": "https://x.com/i/1"}


def run(store, titles, composer=None):
    seen = []
    comp = composer or (lambda **kw: seen.append(kw) or "โพสต์\n#ทองวันนี้ #ทองคำ")
    n = sq.mirror_events(store, [{"event_id": f"id{i}", "en_title": t, "route": "breaking"}
                                 for i, t in enumerate(titles)],
                         CFG, composer=comp, poster=lambda t: "u", now=NOW)
    return n, seen


# ---------------------------------------------------------------- topic keys

def test_topic_keys_real_titles():
    assert sq._topic_keys("Labor market faltered in September as jobs increased by just 29,000") == {"topic:US:jobs"}
    assert sq._topic_keys("NFP misses, stocks cheer, bonds push back — And now France adds") == {"topic:US:jobs"}
    assert sq._topic_keys("Eurozone Harmonized Index of Consumer Prices (YoY) below expectations") == {"topic:EU:cpi"}
    assert sq._topic_keys("Australian Dollar eyes 0.70 as three-day rally meets Fed minutes") == {"topic:US:minutes:preview"}
    assert sq._topic_keys("Gold slides below $4,100 as FOMC Minutes signal further Fed hikes") == {"topic:US:minutes"}
    assert sq._topic_keys("US JOBLESS CLAIMS -2K TO 197K") == {"topic:US:claims"}
    assert sq._topic_keys("ECB's Stournaras: The ECB may stick with raising rates gradually.") == set()


def test_same_topic_rehash_blocked_within_window():
    store = Store([row("Labor market faltered in September as jobs increased by just 29,000", 2)])
    n, _ = run(store, ["US labour market: Softer jobs ease Fed hike risk – ABN Amro"])
    assert n == 0


def test_same_topic_allowed_after_window():
    store = Store([row("Labor market faltered in September as jobs increased by just 29,000", 49)])
    n, _ = run(store, ["US Dollar: Softer jobs data shapes Fed path – UOB"])
    assert n == 1


def test_new_figure_passes_topic_block():
    store = Store([row("NY Fed: September 1-year ahead expected inflation highest since May 2023", 47)])
    n, _ = run(store, ["United States UoM 1-year Consumer Inflation Expectations increased to 4.7% in October"])
    assert n == 1
    # …but the same figure again is a rehash.
    n2, _ = run(store, ["UoM inflation expectations at 4.7% weigh on bonds"])
    assert n2 == 0


def test_preview_and_result_are_separate_slots():
    store = Store([row("Euro finds relief from softer US Dollar ahead of Fed minutes", 10)])
    n, _ = run(store, ["Swiss Franc retreats further, US Dollar firms up with Fed minutes in focus",
                       "Gold slides below $4,100 as FOMC Minutes signal further Fed hikes"])
    assert n == 1                                    # 2nd preview blocked, result posted


def test_region_keeps_topics_apart():
    store = Store([row("Eurozone Harmonized Index of Consumer Prices below expectations", 3)])
    n, _ = run(store, ["US CPI rises 0.4% in September"])
    assert n == 1


# ---------------------------------------------------------------- vary ending

def test_vary_ending_when_most_posts_closed_on_gold():
    gold = "ดอลลาร์แข็ง กดทองในระยะสั้น\n#ทองวันนี้ #ทองคำ"
    other = "ตลาดจับตา CPI วันพุธ\n#ทองวันนี้ #ทองคำ"
    _, seen = run(Store([row("A thing", 1, gold), row("B thing", 2, gold)]), ["Fed's Waller: x"])
    assert seen[0]["vary_ending"] is True
    _, seen = run(Store([row("A thing", 1, other), row("B thing", 2, other)]), ["Fed's Waller: x"])
    assert seen[0]["vary_ending"] is False
    _, seen = run(Store([row("A thing", 1, gold)]), ["Fed's Waller: x"])   # too few to judge
    assert seen[0]["vary_ending"] is False


def test_ends_on_gold_ignores_hashtags():
    assert sq._ends_on_gold("ยีลด์ลง ทองได้แรงหนุน\n#ทองวันนี้ #ทองคำ")
    assert not sq._ends_on_gold("ตลาดจับตา CPI วันพุธ\n#ทองวันนี้ #ทองคำ")


# ---------------------------------------------------------------- composer prompt

class FakeClient:
    def __init__(self, outputs):
        self.outputs, self.prompts = list(outputs), []
        self.messages = self

    def create(self, **kw):
        self.prompts.append(kw["messages"][0]["content"])
        return SimpleNamespace(content=[SimpleNamespace(text=self.outputs.pop(0))])


def _compose(monkeypatch, outputs, **kw):
    c = FakeClient(outputs)
    monkeypatch.setattr(tw, "_get_anthropic_client", lambda: c)
    args = dict(headline_th="หัวข่าว", body_th=["รายละเอียด"], impact_th=None, category="Fed",
                en_title="Fed's Waller: x", en_summary=None, autopost=True)
    args.update(kw)
    return tw.compose_tweet(**args), c


def test_prompt_has_accuracy_rules(monkeypatch):
    _, c = _compose(monkeypatch, ['{"tweet": "เฟดส่งสัญญาณ"}'])
    p = c.prompts[0]
    assert "สิ่งที่อาจเกิด" in p and "ห้ามเติมข้อมูลที่ไม่มีใน NEWS CONTEXT" in p
    assert "ไม่เกิน 1 คำต่อโพสต์" in p
    assert "มีแค่หัวข่าว" not in p and "จบด้วยผลต่อทองไปแล้ว" not in p


def test_title_only_and_vary_rules_added_when_needed(monkeypatch):
    _, c = _compose(monkeypatch, ['{"tweet": "ทรัมป์พูดถึงอิหร่าน"}'],
                    body_th=[], impact_th=None, vary_ending=True)
    p = c.prompts[0]
    assert "มีแค่หัวข่าว" in p and "จบด้วยผลต่อทองไปแล้ว" in p


def test_bare_skip_is_skip_not_failure(monkeypatch):
    out, _ = _compose(monkeypatch, ["SKIP"])
    assert out == tw.SKIP
    out, _ = _compose(monkeypatch, ['"SKIP"'])
    assert out == tw.SKIP


def test_garbage_output_still_fails_not_skips(monkeypatch):
    out, _ = _compose(monkeypatch, ["sorry, I can't"])
    assert out is None
