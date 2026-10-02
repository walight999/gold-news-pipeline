"""X autopost quality (2026-10-02 review of 77 live posts): story-level dedup,
weak-gold-link SKIP, no repeated closings, no guessed rate direction, no
mid-sentence clipping."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from src import squawk_mirror as sq
from src import tweet_writer as tw

UTC = timezone.utc
NOW = datetime(2026, 10, 2, 13, 0, tzinfo=UTC)   # 20:00 ICT — US session
CFG = {"cap_per_hour": 99, "max_items": 9}


class FakeStore:
    def __init__(self, rows=None):
        self.rows = list(rows or [])

    def read_feed(self, tab):
        return sq.MIRROR_HEADERS, [dict(r) for r in self.rows]

    def append_feed(self, tab, headers, rows):
        for r in rows:
            self.rows.append(dict(zip(headers, r)))


def _cand(i, title):
    return {"event_id": f"e{i}", "route": "breaking", "en_title": title,
            "headline_th": "หัวข่าว", "body_th": ["เนื้อหา"], "impact_th": "ผลต่อทอง",
            "category": "Central Bank"}


def _row(minutes_ago, title, tweet="ทวีตเดิม กดดันทองในระยะสั้น\n" + tw.TAGS):
    ts = NOW - timedelta(minutes=minutes_ago)
    return {"ts_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ts_ict": (ts + timedelta(hours=7)).strftime("%Y-%m-%d %H:%M:%S"),
            "fs_id": f"ev:old{minutes_ago}", "fs_text": title, "tweet_text": tweet,
            "posted": "https://x.com/i/0"}


# ---------------- story keys ----------------

def test_story_keys_pick_speakers_and_places_not_generic_words():
    assert sq._story_keys("Fed's Kashkari says inflation is still too high") == {"kashkari"}
    assert sq._story_keys("Logan: at minimum, several more rate hikes") == {"logan"}
    assert sq._story_keys("Tokyo core CPI jumps to 2.7%") == {"tokyo"}
    assert sq._story_keys("Japanese Yen rises due to hot Tokyo CPI, dovish Fed bets") == {"tokyo"}
    assert sq._story_keys("Gold falls to near $4,150 as higher Treasury yields") == set()
    assert sq._story_keys("Brent Oil: Conflict-driven surge – Deutsche Bank") == set()
    assert sq._story_keys("S. Korea September core CPI rises 2.8%") == set()


# ---------------- story-level dedup ----------------

def test_same_speaker_within_window_is_skipped_but_not_after():
    calls = []

    def composer(**kw):
        calls.append(kw["en_title"])
        return "ทวีตใหม่\n" + tw.TAGS

    rows = [_row(60, "Fed's Kashkari says inflation is still too high")]
    n = sq.mirror_events(FakeStore(rows), [_cand(1, "Fed's Kashkari warns supply shocks may lift expectations")],
                         CFG, composer=composer, poster=lambda t: "u", now=NOW)
    assert n == 0 and calls == []          # skipped before paying for a Sonnet call
    rows = [_row(240, "Fed's Kashkari says inflation is still too high")]   # 4h ago
    n = sq.mirror_events(FakeStore(rows), [_cand(1, "Fed's Kashkari warns supply shocks may lift expectations")],
                         CFG, composer=composer, poster=lambda t: "u", now=NOW)
    assert n == 1


def test_burst_in_one_run_posts_the_story_once():
    cands = [_cand(1, "Logan: at minimum, several more rate hikes would reverse cuts"),
             _cand(2, "Logan: without higher rates, inflation won't reach 2%"),
             _cand(3, "Logan: Economic expansion strengthening, labor market balanced")]
    n = sq.mirror_events(FakeStore(), cands, CFG, composer=lambda **k: "ทวีต\n" + tw.TAGS,
                         poster=lambda t: "u", now=NOW)
    assert n == 1


# ---------------- weak gold link + closings ----------------

def test_skip_verdict_posts_nothing_and_logs_nothing():
    st = FakeStore()
    n = sq.mirror_events(st, [_cand(1, "S. Korea September core CPI rises 2.8%")], CFG,
                         composer=lambda **k: tw.SKIP, poster=lambda t: 1 / 0, now=NOW)
    assert n == 0 and st.rows == []


def test_composer_gets_autopost_flag_and_recent_closings():
    got = {}

    def composer(**kw):
        got.update(kw)
        return "ทวีต\n" + tw.TAGS

    rows = [_row(300, "UNRELATED STORY ALPHA BETA GAMMA")]
    sq.mirror_events(FakeStore(rows), [_cand(1, "Fed's Cook warns on inflation expectations")], CFG,
                     composer=composer, poster=lambda t: "u", now=NOW)
    assert got["autopost"] is True
    assert any("กดดันทองในระยะสั้น" in c for c in got["avoid_closings"])


# ---------------- tweet_writer autopost mode ----------------

class _Client:
    """Fake Anthropic client returning queued texts and recording prompts."""
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.prompts.append(kw["messages"][0]["content"])
        text = self.outputs.pop(0)
        return SimpleNamespace(content=[SimpleNamespace(text=text)])


def _compose(monkeypatch, outputs, **kw):
    c = _Client(outputs)
    monkeypatch.setattr(tw, "_get_anthropic_client", lambda: c)
    out = tw.compose_tweet(headline_th="h", body_th=["b"], impact_th="i", category="c",
                           en_title="RBA Interest Rate Decision meets forecasts (4.6%)",
                           en_summary=None, **kw)
    return out, c


def test_autopost_prompt_carries_fact_and_topic_rules(monkeypatch):
    out, c = _compose(monkeypatch, ['{"tweet": "RBA ดอกเบี้ย 4.6% ตรงคาด"}'],
                      autopost=True, avoid_closings=["กดดันทองในระยะสั้น"])
    p = c.prompts[0]
    assert "meets forecasts" in p and "ห้ามเดา" in p           # no guessed direction
    assert "English title" in p                                # anchor on the title
    assert "· กดดันทองในระยะสั้น" in p                          # closings to avoid
    assert out.endswith(tw.TAGS)


def test_autopost_skip_verdict(monkeypatch):
    out, _ = _compose(monkeypatch, ['{"tweet": "SKIP"}'], autopost=True)
    assert out == tw.SKIP


def test_skip_word_is_not_special_outside_autopost(monkeypatch):
    out, c = _compose(monkeypatch, ['{"tweet": "SKIP"}'])
    assert out != tw.SKIP and "EXTRA RULES" not in c.prompts[0]


def test_overlong_autopost_is_rewritten_not_clipped(monkeypatch):
    long_body = "ประโยคยาวมาก " * 30
    short = "เวอร์ชันสั้นที่จบประโยคสมบูรณ์"
    out, c = _compose(monkeypatch, ['{"tweet": "%s"}' % long_body, '{"tweet": "%s"}' % short],
                      autopost=True)
    assert len(c.prompts) == 2 and out.startswith(short) and "…" not in out


def test_overlong_autopost_falls_back_to_clip_if_shorten_fails(monkeypatch):
    long_body = "ประโยคยาวมาก " * 30
    out, _ = _compose(monkeypatch, ['{"tweet": "%s"}' % long_body, "not json"], autopost=True)
    assert len(out) <= tw.TWEET_LIMIT and out.endswith(tw.TAGS)
