"""2026-10-19: recovered FXStreet events all scored 0.5 (vs 0.9-3.0 when it was
fetched directly every 5 min). Two causes, two fixes:
1. a 15-22 min poll can't see an item before it is 10-30 min old → the feed's
   `freshness_grace_min` shifts the freshness anchor by the poll latency;
2. the proxy returns the whole feed every call → only items newer than the last
   poll's newest are passed on, so old items aren't re-scored at a decayed
   freshness (score overwrites the first sighting in event_state)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from src import main as m
from src import scorer
from src import store as store_mod
from src.normalizer import Item

NOW = datetime(2026, 10, 19, 13, 40, tzinfo=timezone.utc)
KW = {"topics": {"inflation": {"base_impact": 5}}}


def _event(age_min, grace):
    it = Item(source_id="fxstreet_apify", tier=2, role="gold_bias", title="t", summary="",
              url="https://x/a", published_ts=NOW - timedelta(minutes=age_min),
              first_seen_ts=NOW, organization="fxstreet", freshness_grace_min=grace)
    return SimpleNamespace(topic_bucket="inflation", items=[it], first_seen_ts=NOW)


def test_grace_restores_direct_fetch_score(monkeypatch):
    monkeypatch.setattr(scorer, "now_utc", lambda: NOW)
    import src.utils_time as ut
    monkeypatch.setattr(ut, "now_utc", lambda: NOW)
    # 20 min old at the poll: 0.3 without grace; with 15 min grace it counts as
    # 5 min old → 0.6, what a 5-min-polled feed would typically have scored
    assert scorer.score_event(_event(20, 0), KW) == 5 * 0.3
    assert scorer.score_event(_event(20, 15), KW) == 5 * 0.6
    # grace never reaches the breaking band: 17 min old would be 2 min after a
    # raw 15-min shift, but is capped at 0.6 → 3.0 (< breaking 4.5)
    assert scorer.score_event(_event(17, 15), KW) == 5 * 0.6
    # a genuinely fresh recovered item keeps its real age → full score
    assert scorer.score_event(_event(2, 15), KW) == 5 * 1.0
    # genuinely stale stays stale: 3 h old is >30 min even after grace
    assert scorer.score_event(_event(180, 15), KW) == 5 * 0.1


def _store():
    s = store_mod.Store(sheet_id="x", creds_json="{}")
    for tab in store_mod.SCHEMAS:
        s.data[tab] = {}
    return s


def _rss(*items):
    body = "".join(
        f"<item><title>{t}</title><link>https://www.fxstreet.com/news/{t}</link>"
        f"<pubDate>{p.strftime('%a, %d %b %Y %H:%M:%S GMT')}</pubDate></item>" for t, p in items)
    return f'<rss version="2.0"><channel><title>FX</title>{body}</channel></rss>'.encode()


def test_recover_passes_only_new_items_and_keeps_cursor(monkeypatch):
    cfg = {"apify_recover": {"enabled": True, "actor_id": "a", "min_interval_min": 15,
                             "feeds": [{"id": "fxstreet_apify", "url": "https://www.fxstreet.com/rss",
                                        "organization": "fxstreet", "role": "gold_bias",
                                        "freshness_grace_min": 15}]},
           "x_accounts": {"enabled": False}, "truth_social": {}}
    t1, t2, t3 = NOW - timedelta(minutes=40), NOW - timedelta(minutes=20), NOW - timedelta(minutes=5)
    bodies = iter([_rss(("a", t2), ("b", t1)), _rss(("c", t3), ("a", t2), ("b", t1))])
    import src.apify_source as ap
    monkeypatch.setenv("APIFY_TOKEN", "tok")
    monkeypatch.setattr(ap, "fetch_url_via_proxy", lambda *a, **k: next(bodies))
    monkeypatch.setattr(m, "_apify_due", lambda store, key, interval: True)
    s = _store()

    def run():
        out = m._collect_apify_entries(s, cfg, "cron")
        return [e for e in out if e["source_id"] == "fxstreet_apify"]

    first = run()
    assert sorted(e["title"] for e in first) == ["a", "b"]          # first poll: everything
    assert all(e["freshness_grace_min"] == 15 for e in first)
    row = s.get("source_state", ("_apify_recover:fxstreet_apify",))
    assert row["last_item_ts"] and row["items_last_hour"] == 2
    second = run()
    assert [e["title"] for e in second] == ["c"]                    # only the new item
    assert s.get("source_state", ("_apify_recover:fxstreet_apify",))["items_last_hour"] == 3


def test_mark_apify_without_cursor_unchanged():
    s = _store()
    m._mark_apify(s, "_apify", 7)
    row = s.get("source_state", ("_apify",))
    assert row["items_last_hour"] == 7 and not row.get("last_item_ts")
