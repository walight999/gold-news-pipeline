from src import social_feed as sf


def _weighted(tweet: str) -> int:
    """Approximate Twitter weighted length: each URL (last line, starts http)
    counts as 23 regardless of length; everything else counts per char."""
    total = 0
    for i, line in enumerate(tweet.split("\n")):
        if line.startswith("http"):
            total += 23
        else:
            total += len(line)
    total += tweet.count("\n")  # newlines count as 1 each
    return total


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
    t = sf.build_tweet("เฟดส่งสัญญาณ—ผ่อนคลาย", "ทองมีแนวโน้มขึ้น", "Reuters",
                       "https://example.com/x", "dovish")
    assert "—" not in t
    emojis = [c for c in t if c in ("🟢", "🔴", "🟡")]
    assert len(emojis) == 1 and emojis[0] == "🟢"
    assert "#ทองคำ" in t and "Reuters" in t


def test_build_tweet_within_limit_long_input():
    long_head = "ข่าว" * 200
    long_impact = "ผลกระทบ" * 200
    t = sf.build_tweet(long_head, long_impact, "MarketWatch",
                       "https://example.com/very/long/url/path", "hawkish")
    assert _weighted(t) <= 280
    assert t.endswith("https://example.com/very/long/url/path")


def test_build_tweet_no_url():
    t = sf.build_tweet("สั้น", "ผลกระทบสั้น", "Kitco", "", "neutral")
    assert _weighted(t) <= 280
    assert "http" not in t


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
