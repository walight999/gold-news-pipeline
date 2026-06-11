from src import social_feed as sf


def _weighted(tweet: str) -> int:
    """Weighted length: Thai/other chars count 1, newlines count 1. Drafts carry
    no URL anymore, so there's no 23-char URL weighting to apply."""
    return len(tweet)


def test_sanitize_strips_em_dash():
    assert "—" not in sf._sanitize("ทอง — พุ่ง")
    assert "–" not in sf._sanitize("a – b")
    assert sf._sanitize("a   b\n\nc") == "a b c"


def test_tone_emoji_gold_context():
    assert sf._tone_emoji("dovish") == "🟢"
    assert sf._tone_emoji("risk_off") == "🟢"
    assert sf._tone_emoji("hawkish") == "🔴"
    assert sf._tone_emoji("risk_on") == "🔴"
    assert sf._tone_emoji("neutral") == "🟡"
    assert sf._tone_emoji("") == "🟡"


def test_build_tweet_no_em_dash_and_has_one_emoji():
    t = sf.build_tweet("เฟดส่งสัญญาณ—ผ่อนคลาย", "ทองมีแนวโน้มขึ้น", "dovish")
    assert "—" not in t
    emojis = [c for c in t if c in ("🟢", "🔴", "🟡")]
    assert len(emojis) == 1 and emojis[0] == "🟢"
    assert "#ทองคำ" in t


def test_build_tweet_no_url_no_source():
    # PR-voice drafts carry no link and no source attribution (keeps X cost at
    # $0.015/post instead of $0.20 with a URL).
    t = sf.build_tweet("ทองพุ่ง", "ผลกระทบ", "neutral")
    assert "http" not in t
    assert "Reuters" not in t and "·" not in t


def test_build_tweet_within_limit_long_input():
    long_head = "ข่าว" * 200
    long_impact = "ผลกระทบ" * 200
    t = sf.build_tweet(long_head, long_impact, "hawkish")
    assert _weighted(t) <= 280
    assert t.endswith(sf.TAGS)


def test_record_news_event_has_all_headers():
    rec = sf.record_news_event(
        route="breaking", category="Inflation", tone="hawkish",
        impact_level="HIGH", headline_th="CPI สูง", body_th=["a", "b"],
        impact_th="ทองลง", source="BLS", url="https://x.co/a")
    for h in sf.FEED_HEADERS:
        assert h in rec
    assert rec["type"] == "breaking"
    assert rec["summary_th"] == "a b"
    assert rec["posted"] == ""
    row = sf._to_row(rec)
    assert len(row) == len(sf.FEED_HEADERS)
    assert row[sf.FEED_HEADERS.index("type")] == "breaking"


def test_record_recap():
    stats = {"breaking_n": 3, "alert_n": 1, "top_topics": [("inflation", 2, 4.1)]}
    rec = sf.record_recap(stats, "11/6/26")
    assert rec["type"] == "recap"
    assert "เงินเฟ้อ" in rec["tweet_text"]   # topic translated to Thai
    assert _weighted(rec["tweet_text"]) <= 280


def test_flush_noop_on_empty():
    assert sf.flush(None, []) == 0


class _FakeStore:
    def __init__(self):
        self.appended = []
    def append_feed(self, tab, headers, rows):
        self.appended.append((tab, headers, rows))


def test_flush_appends_and_swallows_errors():
    s = _FakeStore()
    rec = sf.record_news_event(route="alert", category="x", tone="neutral",
                               impact_level="MEDIUM", headline_th="h",
                               body_th=[], impact_th="i", source="s", url="")
    assert sf.flush(s, [rec]) == 1
    assert s.appended[0][0] == sf.FEED_TAB
    assert len(s.appended[0][2]) == 1

    class _Boom:
        def append_feed(self, *a):
            raise RuntimeError("sheets down")
    assert sf.flush(_Boom(), [rec]) == 0   # never raises


# ---------------- posting side (post_pending) ----------------

class _FeedStore:
    """Fake store exposing read_feed + set_feed_cell like Store."""
    def __init__(self, headers, rows):
        self._headers = headers
        # rows: list of dicts WITHOUT _row; we assign row numbers from 2
        self._rows = []
        for i, r in enumerate(rows, start=2):
            d = dict(r); d["_row"] = i
            self._rows.append(d)
        self.cell_writes = []  # (row, col, value)
    def read_feed(self, tab):
        return self._headers, [dict(r) for r in self._rows]
    def set_feed_cell(self, tab, row, col, value):
        self.cell_writes.append((row, col, value))


_HEADERS = sf.FEED_HEADERS


def _row(**kw):
    base = {h: "" for h in _HEADERS}
    base.update(kw)
    return base


def test_is_yes_variants():
    assert sf._is_yes("yes") and sf._is_yes("YES") and sf._is_yes(" Yes ")
    assert sf._is_yes("true") and sf._is_yes("1") and sf._is_yes("✓")
    assert not sf._is_yes("") and not sf._is_yes("no") and not sf._is_yes(None)


def test_post_pending_only_approved_unposted():
    rows = [
        _row(tweet_text="A", approved="yes", posted=""),     # post
        _row(tweet_text="B", approved="",    posted=""),     # skip (not approved)
        _row(tweet_text="C", approved="yes", posted="http"), # skip (already posted)
        _row(tweet_text="D", approved="YES", posted=""),     # post
    ]
    s = _FeedStore(_HEADERS, rows)
    posted = []
    def fake(text):
        posted.append(text)
        return f"https://x.com/i/web/status/{len(posted)}"
    n = sf.post_pending(s, poster=fake, limit=10)
    assert n == 2
    assert posted == ["A", "D"]
    posted_col = _HEADERS.index("posted") + 1
    # rows 2 (A) and 5 (D) get the posted URL written
    assert (2, posted_col, "https://x.com/i/web/status/1") in s.cell_writes
    assert (5, posted_col, "https://x.com/i/web/status/2") in s.cell_writes


def test_post_pending_respects_limit():
    rows = [_row(tweet_text=str(i), approved="yes", posted="") for i in range(10)]
    s = _FeedStore(_HEADERS, rows)
    n = sf.post_pending(s, poster=lambda t: "u", limit=3)
    assert n == 3
    assert len(s.cell_writes) == 3


def test_post_pending_failure_leaves_unposted():
    rows = [
        _row(tweet_text="boom", approved="yes", posted=""),
        _row(tweet_text="ok",   approved="yes", posted=""),
    ]
    s = _FeedStore(_HEADERS, rows)
    def flaky(text):
        if text == "boom":
            raise RuntimeError("X 403")
        return "https://x.com/i/web/status/9"
    n = sf.post_pending(s, poster=flaky, limit=10)
    assert n == 1                      # only the good one counted
    # the failed row was NOT marked posted (will retry next run)
    assert all(w[2] == "https://x.com/i/web/status/9" for w in s.cell_writes)
    assert len(s.cell_writes) == 1


def test_post_pending_empty_feed():
    assert sf.post_pending(_FeedStore([], []), poster=lambda t: "u") == 0
