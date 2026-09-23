from datetime import datetime, timezone

from src import squawk_mirror as sq

UTC = timezone.utc
NOW = datetime(2026, 9, 22, 3, 0, tzinfo=UTC)   # 10:00 ICT → today = 2026-09-22


class FakeStore:
    """Minimal feed-tab store: append_feed stores dict rows, read_feed returns them."""
    def __init__(self, rows=None):
        self.rows = list(rows or [])

    def read_feed(self, tab):
        return sq.MIRROR_HEADERS, [dict(r) for r in self.rows]

    def append_feed(self, tab, headers, rows):
        for r in rows:
            self.rows.append(dict(zip(headers, r)))


def _entry(fid, text, minute):
    return {"title": text, "url": f"https://x.com/FirstSquawk/status/{fid}",
            "published_ts": datetime(2026, 9, 22, 2, minute, tzinfo=UTC)}


def _compose(**kw):
    # Stand-in for tweet_writer.compose_tweet — proves en_title is what we pass.
    return "ทอง: " + str(kw["en_title"])[:40]


# --------------------------------------------------------------------------
# pure helpers
# --------------------------------------------------------------------------

def test_is_relevant_keeps_gold_movers_and_drops_offtopic():
    kw = [k.lower() for k in sq.DEFAULT_KEYWORDS]
    assert sq.is_relevant("*FED RAISES RATES 25BPS", kw)
    assert sq.is_relevant("Gold slips as US 10-year yield hits 5%", kw)
    assert sq.is_relevant("BOJ hikes to 1.25%", kw)
    # geopolitics / safe-haven — now in scope (a top gold catalyst)
    assert sq.is_relevant("TRUMP SAYS HE URGED IRAN TO NEGOTIATE", kw)
    assert sq.is_relevant("UNITED NATIONS-TRUMP: IRAN WILL NEVER HAVE A NUCLEAR WEAPON", kw)
    assert sq.is_relevant("OIL PRICES WILL PLUMMET AFTER CONFLICT IS OVER", kw)
    # off-brand for a gold channel
    assert not sq.is_relevant("Apple unveils new iPhone in Cupertino", kw)
    assert not sq.is_relevant("Champions League final kicks off tonight", kw)


def test_is_relevant_word_boundary_and_plurals():
    kw = [k.lower() for k in sq.DEFAULT_KEYWORDS]
    # word-boundary: "war" is not a keyword, and mid-word noise never fires
    assert not sq.is_relevant("Retailer issues a profit warning toward year-end", kw)
    assert not sq.is_relevant("Steady forward guidance from the boardroom", kw)  # no 'fed' here
    # singular stems still catch the plural
    assert sq.is_relevant("US Treasury yields climb", kw)          # yield→yields
    assert sq.is_relevant("New sanctions on Tehran announced", kw)  # sanction→sanctions


def test_is_relevant_gold_is_whole_word_not_goldman():
    kw = [k.lower() for k in sq.DEFAULT_KEYWORDS]
    # "gold" must not fire on "Goldman"/"golden" (bank/name, not the metal)
    assert not sq.is_relevant("UBS faces extra capital, GOLDMAN SAYS - BBG", kw)
    assert not sq.is_relevant("Golden Globes ceremony tonight", kw)
    # but the metal itself still matches
    assert sq.is_relevant("Gold slips to 4,300", kw)
    assert sq.is_relevant("Spot gold rebounds", kw)


def test_fs_id_from_url_and_fallback():
    assert sq._fs_id({"url": "https://x.com/FirstSquawk/status/1234"}) == "1234"
    assert sq._fs_id({"url": "https://firstsquawk.com/x"}) == "https://firstsquawk.com/x"


def test_seen_and_today_count_only_counts_posted_today():
    rows = [
        {"fs_id": "1", "ts_ict": "2026-09-22 09:00:00", "posted": "http://x/1"},
        {"fs_id": "2", "ts_ict": "2026-09-22 09:05:00", "posted": ""},          # not posted
        {"fs_id": "3", "ts_ict": "2026-09-21 23:00:00", "posted": "http://x/3"}, # yesterday
    ]
    seen, today = sq._seen_and_today_count(rows, "2026-09-22")
    assert seen == {"1", "2", "3"}
    assert today == 1


# --------------------------------------------------------------------------
# mirror orchestration
# --------------------------------------------------------------------------

def test_mirror_no_token_is_noop():
    assert sq.mirror(FakeStore(), token="") == 0


def test_mirror_posts_relevant_only_and_dedups_on_rerun(monkeypatch):
    entries = [
        _entry("101", "*FED RAISES RATES 25BPS, SIGNALS MORE", 10),
        _entry("102", "Man City wins the derby", 11),               # off-topic
        _entry("103", "US 10-year yield climbs to 5%", 12),
    ]
    monkeypatch.setattr(sq.apify_source, "fetch_tweets",
                        lambda *a, **k: list(entries))
    posted = []
    store = FakeStore()
    n = sq.mirror(store, token="T", cfg={"cap_per_day": 20},
                  composer=_compose, poster=lambda t: (posted.append(t), "http://x/ok")[1],
                  now=NOW)
    assert n == 2                                   # 2 relevant, 1 dropped
    assert len(store.rows) == 2
    assert all(r["posted"] == "http://x/ok" for r in store.rows)
    assert {r["fs_id"] for r in store.rows} == {"101", "103"}
    # tweets are the re-voiced Thai composition, not the raw English
    assert all(t.startswith("ทอง:") for t in posted)

    # Re-run over the SAME entries → all deduped, nothing re-posts.
    n2 = sq.mirror(store, token="T", composer=_compose,
                   poster=lambda t: (posted.append(t), "http://x/ok2")[1], now=NOW)
    assert n2 == 0
    assert len(store.rows) == 2


def test_mirror_respects_daily_cap(monkeypatch):
    entries = [_entry(str(200 + i), f"Fed official {i} backs rate hike", i)
               for i in range(5)]
    monkeypatch.setattr(sq.apify_source, "fetch_tweets", lambda *a, **k: list(entries))
    store = FakeStore()
    n = sq.mirror(store, token="T", cfg={"cap_per_day": 2},
                  composer=_compose, poster=lambda t: "http://x/ok", now=NOW)
    assert n == 2
    assert len(store.rows) == 2


def test_mirror_skips_when_composer_returns_none(monkeypatch):
    entries = [_entry("300", "Gold falls on hawkish Fed", 10)]
    monkeypatch.setattr(sq.apify_source, "fetch_tweets", lambda *a, **k: list(entries))
    posted = []
    store = FakeStore()
    n = sq.mirror(store, token="T", composer=lambda **k: None,
                  poster=lambda t: (posted.append(t), "http://x/ok")[1], now=NOW)
    assert n == 0
    assert posted == []            # never posted the raw English headline
    assert store.rows == []


def test_mirror_cap_already_reached_skips_scrape(monkeypatch):
    called = {"n": 0}

    def _fetch(*a, **k):
        called["n"] += 1
        return []
    monkeypatch.setattr(sq.apify_source, "fetch_tweets", _fetch)
    rows = [{"fs_id": str(i), "ts_ict": "2026-09-22 08:00:00", "posted": "http://x"}
            for i in range(20)]
    store = FakeStore(rows)
    n = sq.mirror(store, token="T", cfg={"cap_per_day": 20},
                  composer=_compose, poster=lambda t: "x", now=NOW)
    assert n == 0
    assert called["n"] == 0        # cap reached → we don't even pay to scrape
