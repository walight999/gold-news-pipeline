"""squawk events mode (2026-09-27): X posts exactly the classifier-approved
breaking/alert events — one editorial brain for LINE and X."""
from datetime import datetime, timedelta, timezone

from src import social_feed
from src import squawk_mirror as sq

UTC = timezone.utc
NOW = datetime(2026, 9, 28, 3, 0, tzinfo=UTC)   # 10:00 ICT Mon


class FakeStore:
    def __init__(self, rows=None, fail=False):
        self.rows = list(rows or [])
        self.fail = fail

    def read_feed(self, tab):
        if self.fail:
            raise RuntimeError("sheets down")
        return sq.MIRROR_HEADERS, [dict(r) for r in self.rows]

    def append_feed(self, tab, headers, rows):
        for r in rows:
            self.rows.append(dict(zip(headers, r)))


def _cand(i, title, route="breaking"):
    return {"event_id": f"e{i}", "route": route, "en_title": title,
            "headline_th": f"หัวข่าว {i}", "body_th": ["เนื้อหา"],
            "impact_th": "กดดันทองคำ", "category": "Central Bank"}


TITLES = ["FED RAISES RATES 25BPS", "GOLD HITS RECORD HIGH",
          "IRAN MISSILE STRIKE REPORTED", "US 10-YEAR YIELD JUMPS",
          "ECB LAGARDE SIGNALS PAUSE", "OPEC CUTS OUTPUT", "DOLLAR INDEX SLIDES"]


def _row(minutes_ago, fid="ev:old", text="SOMETHING ELSE ENTIRELY HERE"):
    ts = NOW - timedelta(minutes=minutes_ago)
    ict = ts + timedelta(hours=7)
    return {"ts_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ts_ict": ict.strftime("%Y-%m-%d %H:%M:%S"),
            "fs_id": fid, "fs_text": text, "tweet_text": "x", "posted": "https://x.com/i/0"}


def test_posts_classifier_events_with_thai_context_and_logs_them():
    got = []

    def composer(**kw):
        got.append(kw)
        return "ทวีต " + kw["headline_th"]

    st = FakeStore()
    n = sq.mirror_events(st, [_cand(1, TITLES[0])], {"cap_per_day": 20},
                         composer=composer, poster=lambda t: "https://x.com/i/1", now=NOW)
    assert n == 1
    # composed from the LINE classifier's Thai output, not just the English line
    assert got[0]["headline_th"] == "หัวข่าว 1" and got[0]["impact_th"] == "กดดันทองคำ"
    assert got[0]["model"] == sq.DEFAULT_COMPOSE_MODEL
    assert st.rows[0]["fs_id"] == "ev:e1" and st.rows[0]["posted"] == "https://x.com/i/1"


def test_same_event_never_posts_twice():
    st = FakeStore()
    kw = dict(composer=lambda **k: "t", poster=lambda t: "u", now=NOW)
    assert sq.mirror_events(st, [_cand(1, TITLES[0])], {}, **kw) == 1
    assert sq.mirror_events(st, [_cand(1, TITLES[0])], {}, **kw) == 0


def test_hourly_cap_spreads_posts():
    rows = [_row(10 + i, fid=f"ev:x{i}", text=f"UNRELATED STORY NUMBER {i} ALPHA BETA") for i in range(3)]
    st = FakeStore(rows)
    cands = [_cand(i, t) for i, t in enumerate(TITLES)]
    n = sq.mirror_events(st, cands, {"cap_per_hour": 4, "max_items": 10},
                         composer=lambda **k: "t", poster=lambda t: "u", now=NOW)
    assert n == 1   # 3 already this hour → only 1 more


def test_posts_older_than_an_hour_do_not_count_toward_hourly_cap():
    rows = [_row(90 + i, fid=f"ev:x{i}", text=f"UNRELATED STORY NUMBER {i} ALPHA BETA") for i in range(4)]
    n = sq.mirror_events(FakeStore(rows), [_cand(1, TITLES[0])], {"cap_per_hour": 4},
                         composer=lambda **k: "t", poster=lambda t: "u", now=NOW)
    assert n == 1


def test_daily_cap_and_per_run_limit():
    rows = [_row(120 + i, fid=f"ev:x{i}", text=f"UNRELATED STORY NUMBER {i} ALPHA BETA") for i in range(19)]
    cands = [_cand(i, t) for i, t in enumerate(TITLES)]
    kw = dict(composer=lambda **k: "t", poster=lambda t: "u", now=NOW)
    one_session = [{"name": "all", "start": 5, "cap": 99}]   # isolate the DAILY cap
    assert sq.mirror_events(FakeStore(rows), cands, {"cap_per_day": 20, "cap_per_hour": 9,
                                                     "sessions": one_session}, **kw) == 1
    assert sq.mirror_events(FakeStore(), cands, {"cap_per_hour": 9, "max_items": 2}, **kw) == 2


def test_near_duplicate_of_todays_post_is_skipped():
    rows = [_row(200, fid="ev:x", text="FED RAISES RATES BY 25BPS TODAY")]
    n = sq.mirror_events(FakeStore(rows), [_cand(1, "FED RAISES RATES 25BPS")], {},
                         composer=lambda **k: "t", poster=lambda t: "u", now=NOW)
    assert n == 0


def test_compose_or_post_failure_skips_and_unreadable_log_posts_nothing():
    def boom(t):
        raise RuntimeError("x down")
    st = FakeStore()
    assert sq.mirror_events(st, [_cand(1, TITLES[0])], {}, composer=lambda **k: None,
                            poster=lambda t: "u", now=NOW) == 0
    assert sq.mirror_events(st, [_cand(1, TITLES[0])], {}, composer=lambda **k: "t",
                            poster=boom, now=NOW) == 0
    assert st.rows == []
    # can't read the dedup log ⇒ refuse rather than risk a double-post
    assert sq.mirror_events(FakeStore(fail=True), [_cand(1, TITLES[0])], {},
                            composer=lambda **k: "t", poster=lambda t: "u", now=NOW) == 0


def test_record_without_compose_makes_no_claude_call(monkeypatch):
    from src import tweet_writer

    def boom(**kw):
        raise AssertionError("compose=False must not call Claude")
    monkeypatch.setattr(tweet_writer, "compose_tweet", boom)
    rec = social_feed.record_news_event(
        route="breaking", category="Central Bank", tone="hawkish", impact_level="HIGH",
        headline_th="หัวข่าว", body_th=["เนื้อหา"], impact_th="ผลต่อทอง",
        source="FXStreet", url="https://example.com", compose=False)
    assert rec["tweet_text"] == "" and rec["headline_th"] == "หัวข่าว"


# ---------------- session quotas (2026-10-02) ----------------

def _at(ict_hour, day=2, minute=0):
    """UTC datetime for 2026-10-<day> <ict_hour>:<minute> ICT."""
    return datetime(2026, 10, day, ict_hour, minute, tzinfo=UTC) - timedelta(hours=7)         if ict_hour >= 7 else datetime(2026, 10, day - 1, ict_hour + 17, minute, tzinfo=UTC)


def test_current_session_boundaries_and_wrap():
    S = sq.DEFAULT_SESSIONS
    s, start, day = sq.current_session(_at(10), S)
    assert s["name"] == "asia" and start == _at(5) and day == _at(5)
    s, start, _ = sq.current_session(_at(14), S)
    assert s["name"] == "europe" and start == _at(14)
    s, start, day = sq.current_session(_at(19, minute=30), S)
    assert s["name"] == "us" and start == _at(19) and day == _at(5)
    # 02:00 ICT on the 3rd is still the 2nd's US session and trading day
    s, start, day = sq.current_session(_at(2, day=3), S)
    assert s["name"] == "us" and start == _at(19) and day == _at(5)


def _row_at(ts, i):
    ict = ts + timedelta(hours=7)
    return {"ts_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ts_ict": ict.strftime("%Y-%m-%d %H:%M:%S"), "fs_id": f"ev:p{i}",
            "fs_text": f"UNRELATED STORY NUMBER {i} ALPHA BETA", "tweet_text": "x",
            "posted": "https://x.com/i/0"}


def test_full_asia_session_does_not_starve_us_session():
    # NFP-day replay: asia already used its 5 (and more under the old flat cap)
    rows = [_row_at(_at(6) + timedelta(minutes=10 * i), i) for i in range(8)]
    cands = [_cand(i, t) for i, t in enumerate(TITLES)]
    kw = dict(composer=lambda **k: "t", poster=lambda t: "u")
    cfg = {"cap_per_day": 20, "cap_per_hour": 9, "max_items": 9}
    assert sq.mirror_events(FakeStore(rows), cands, cfg, now=_at(12), **kw) == 0   # asia full
    assert sq.mirror_events(FakeStore(rows), cands, cfg, now=_at(20), **kw) == 7   # us open


def test_us_session_cap_spans_midnight():
    rows = [_row_at(_at(22) + timedelta(minutes=10 * i), i) for i in range(10)]   # us: 10 used
    n = sq.mirror_events(FakeStore(rows), [_cand(1, TITLES[0])], {"cap_per_hour": 99},
                         composer=lambda **k: "t", poster=lambda t: "u", now=_at(1, day=3))
    assert n == 0
