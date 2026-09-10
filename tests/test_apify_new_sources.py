"""Tests for the 2026-09-10 Apify upgrades: generic run_actor, Truth Social
mapping, Cloudflare-recovery body fetch, and the event-mode burst wiring.

Third-party actor I/O can't be verified from code alone, so these tests mock
the actor call and prove the MAPPING + GATING logic — the parts we own. Going
live still needs a one-time `--mode apify_probe` run per source (a few cents).
"""
from __future__ import annotations

from src import apify_source as ap
from src import main


# ---------------- run_actor (generic) ----------------

def test_run_actor_noop_without_token_or_actor():
    assert ap.run_actor("", "some/actor", {}) == []
    assert ap.run_actor("tok", "", {}) == []


def test_run_actor_normalises_id_and_uses_auth_header(monkeypatch):
    captured = {}

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return [{"ok": 1}]

    class _Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, url, params=None, json=None, headers=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return _Resp()

    monkeypatch.setattr(ap.httpx, "Client", _Client)
    out = ap.run_actor("tok", "owner/name", {"a": 1})
    assert out == [{"ok": 1}]
    # owner/name → owner~name in the endpoint path
    assert "owner~name" in captured["url"]
    assert captured["headers"]["Authorization"] == "Bearer tok"
    assert captured["json"] == {"a": 1}


def test_run_actor_swallows_errors_and_redacts_token(monkeypatch, caplog):
    class _Boom:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, *a, **k): raise RuntimeError("boom tok leaked")

    monkeypatch.setattr(ap.httpx, "Client", _Boom)
    assert ap.run_actor("tok", "o/n", {}) == []
    assert "tok" not in caplog.text  # token redacted from the warning


def test_run_actor_non_list_response_is_empty(monkeypatch):
    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"error": "nope"}

    class _Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, *a, **k): return _Resp()

    monkeypatch.setattr(ap.httpx, "Client", _Client)
    assert ap.run_actor("tok", "o/n", {}) == []


# ---------------- Truth Social mapping ----------------

def test_truth_entry_basic_nested_account():
    e = ap._truth_to_entry({
        "content": "<p>TRUMP: new 25% tariff on all steel imports</p>",
        "url": "https://truthsocial.com/@realDonaldTrump/111",
        "created_at": "2026-09-10T12:00:00.000Z",
        "account": {"username": "realDonaldTrump"},
    })
    assert e["source_id"] == "truth_realdonaldtrump"
    assert e["organization"] == "truth_realdonaldtrump"
    assert e["source_class"] == "wire"
    assert e["tier"] == 2
    assert e["title"] == "TRUMP: new 25% tariff on all steel imports"  # HTML stripped
    assert e["published_ts"].year == 2026


def test_truth_entry_builds_url_from_id_and_flat_handle():
    e = ap._truth_to_entry({"text": "Fed should cut now",
                            "id": "999", "username": "realDonaldTrump"})
    assert e["url"] == "https://truthsocial.com/@realDonaldTrump/999"


def test_truth_entry_drops_no_author_empty_and_reblog():
    assert ap._truth_to_entry({"content": "no author", "id": "1"}) is None
    assert ap._truth_to_entry({"content": "  ", "account": {"username": "x"}}) is None
    assert ap._truth_to_entry({"content": "hi", "url": "u",
                               "account": {"username": "x"}, "reblog": {"id": 5}}) is None


def test_truth_field_map_override():
    e = ap._truth_to_entry(
        {"note": "custom body field", "permalink": "https://t/1", "acct": "trumper"},
        field_map={"text": ["note"], "url": ["permalink"], "handle": ["acct"]})
    assert e is not None
    assert e["source_id"] == "truth_trumper"
    assert e["title"] == "custom body field"


def test_fetch_truth_social_maps_and_drops(monkeypatch):
    def fake_run_actor(token, actor_id, payload, timeout=90.0):
        return [
            {"content": "TRUMP: tariffs incoming", "id": "1",
             "account": {"username": "realDonaldTrump"}},
            {"content": "", "account": {"username": "x"}},        # dropped (empty)
            {"content": "orphan", "id": "2"},                     # dropped (no author)
        ]
    monkeypatch.setattr(ap, "run_actor", fake_run_actor)
    out = ap.fetch_truth_social("tok", "parsebird/truth-social-scraper",
                                ["realDonaldTrump"])
    assert len(out) == 1
    assert out[0]["source_id"] == "truth_realdonaldtrump"


def test_fetch_truth_social_noop_without_config():
    assert ap.fetch_truth_social("", "actor", ["h"]) == []
    assert ap.fetch_truth_social("tok", "", ["h"]) == []
    assert ap.fetch_truth_social("tok", "actor", []) == []


def test_fetch_truth_social_per_handle_calls_once_per_handle(monkeypatch):
    """Default (parsebird) convention: ONE `username` per run → N calls for N
    handles, each payload carrying the single-username key."""
    calls = []

    def fake_run_actor(token, actor_id, payload, timeout=90.0):
        calls.append(payload)
        u = payload["username"]
        return [{"content": f"post from {u}", "id": "1",
                 "account": {"username": u}}]
    monkeypatch.setattr(ap, "run_actor", fake_run_actor)
    out = ap.fetch_truth_social("tok", "parsebird/truth-social-scraper",
                                ["realDonaldTrump", "DonaldJTrumpJr"])
    assert len(calls) == 2                       # one call per handle
    assert calls[0]["username"] == "realDonaldTrump"
    assert calls[0]["cleanContent"] is True
    assert {e["source_id"] for e in out} == {"truth_realdonaldtrump",
                                             "truth_donaldjtrumpjr"}


def test_fetch_truth_social_list_actor_single_call(monkeypatch):
    """per_handle=False → one call with the list under input_key (list actors)."""
    calls = []

    def fake_run_actor(token, actor_id, payload, timeout=90.0):
        calls.append(payload)
        return [{"content": "hi", "id": "1", "account": {"username": "realDonaldTrump"}}]
    monkeypatch.setattr(ap, "run_actor", fake_run_actor)
    ap.fetch_truth_social("tok", "some/list-actor", ["realDonaldTrump", "DonaldJTrumpJr"],
                          per_handle=False, input_key="usernames")
    assert len(calls) == 1
    assert calls[0]["usernames"] == ["realDonaldTrump", "DonaldJTrumpJr"]


# ---------------- Cloudflare-recovery body fetch ----------------

RSS_BODY = (
    "<rss><channel><item><title>Gold rips on soft CPI</title>"
    "<link>https://benzinga.com/x</link></item></channel></rss>"
)


def test_fetch_url_via_proxy_default_bare_string(monkeypatch):
    """Default = scrapeunblocker convention: {"url": "<str>"} (no list wrapper),
    body in the `html` field."""
    captured = {}

    def fake_run_actor(token, actor_id, payload, timeout=90.0):
        captured["payload"] = payload
        return [{"html": RSS_BODY}]
    monkeypatch.setattr(ap, "run_actor", fake_run_actor)
    body = ap.fetch_url_via_proxy("tok", "scrapeunblocker/scrapeunblocker",
                                  "https://www.benzinga.com/markets/feed")
    assert isinstance(body, bytes)
    assert b"Gold rips" in body
    assert captured["payload"]["url"] == "https://www.benzinga.com/markets/feed"


def test_fetch_url_via_proxy_list_of_strings(monkeypatch):
    """ecomscrape convention: {"urls": ["<str>"]}."""
    captured = {}

    def fake_run_actor(token, actor_id, payload, timeout=90.0):
        captured["payload"] = payload
        return [{"html": RSS_BODY}]
    monkeypatch.setattr(ap, "run_actor", fake_run_actor)
    ap.fetch_url_via_proxy("tok", "a", "https://u", input_key="urls",
                           url_as_object=False, url_as_list=True)
    assert captured["payload"]["urls"] == ["https://u"]


def test_fetch_url_via_proxy_list_of_objects(monkeypatch):
    """apify/*-scraper convention: {"startUrls": [{"url": "<str>"}]}."""
    captured = {}

    def fake_run_actor(token, actor_id, payload, timeout=90.0):
        captured["payload"] = payload
        return [{"body": RSS_BODY}]
    monkeypatch.setattr(ap, "run_actor", fake_run_actor)
    ap.fetch_url_via_proxy("tok", "a", "https://u", input_key="startUrls",
                           url_as_object=True, url_as_list=True)
    assert captured["payload"]["startUrls"] == [{"url": "https://u"}]


def test_fetch_url_via_proxy_none_on_empty_or_missing_body(monkeypatch):
    monkeypatch.setattr(ap, "run_actor", lambda *a, **k: [])
    assert ap.fetch_url_via_proxy("tok", "a", "u") is None
    monkeypatch.setattr(ap, "run_actor", lambda *a, **k: [{"nope": 1}])
    assert ap.fetch_url_via_proxy("tok", "a", "u") is None


# ---------------- event-mode burst wiring (_collect_apify_entries) ----------------

class _FakeStore:
    """Minimal store: fresh (no prior state) so every cost-gate is due."""
    def __init__(self): self._s = {}
    def get(self, tab, key): return self._s.get((tab, *key))
    def upsert(self, tab, row): self._s[(tab, row["source_id"])] = row


def _x_cfg():
    return {"x_accounts": {
        "enabled": True, "handles": ["DeItaone"],
        "min_interval_min": 12, "event_min_interval_min": 3,
        "since_minutes": 14, "event_since_minutes": 5,
        "event_extra_handles": ["LiveSquawk"]}}


def _stub_fetch_tweets(monkeypatch, sink):
    def fake(token, handles, since_minutes=0, max_per_handle=8, tier=2):
        sink["handles"] = list(handles)
        sink["since"] = since_minutes
        return []
    monkeypatch.setattr(ap, "fetch_tweets", fake)


def test_cron_mode_uses_base_handles_and_since(monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "tok")
    sink = {}
    _stub_fetch_tweets(monkeypatch, sink)
    main._collect_apify_entries(_FakeStore(), _x_cfg(), "cron")
    assert sink["handles"] == ["DeItaone"]
    assert sink["since"] == 14


def test_event_mode_bursts_with_extra_handles_and_short_lookback(monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "tok")
    sink = {}
    _stub_fetch_tweets(monkeypatch, sink)
    main._collect_apify_entries(_FakeStore(), _x_cfg(), "event")
    assert sink["handles"] == ["DeItaone", "LiveSquawk"]   # widened
    assert sink["since"] == 5                              # shorter lookback


def test_collect_noop_without_token(monkeypatch):
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    assert main._collect_apify_entries(_FakeStore(), _x_cfg(), "cron") == []


def test_disabled_truth_and_recover_do_not_run(monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "tok")
    _stub_fetch_tweets(monkeypatch, {})

    def boom(*a, **k): raise AssertionError("disabled source must not call the actor")
    monkeypatch.setattr(ap, "fetch_truth_social", boom)
    monkeypatch.setattr(ap, "fetch_url_via_proxy", boom)
    cfg = _x_cfg()
    cfg["truth_social"] = {"enabled": False, "actor_id": "a", "handles": ["h"]}
    cfg["apify_recover"] = {"enabled": False, "actor_id": "a",
                            "feeds": [{"id": "b", "url": "u"}]}
    main._collect_apify_entries(_FakeStore(), cfg, "cron")  # no AssertionError = pass


def test_enabled_recover_feeds_pool_via_parse_feed(monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "tok")
    _stub_fetch_tweets(monkeypatch, {})
    monkeypatch.setattr(ap, "fetch_url_via_proxy",
                        lambda *a, **k: RSS_BODY.encode("utf-8"))
    cfg = _x_cfg()
    cfg["apify_recover"] = {
        "enabled": True, "actor_id": "cf/actor", "min_interval_min": 10,
        "feeds": [{"id": "benzinga_apify",
                   "url": "https://www.benzinga.com/markets/feed",
                   "tier": 2, "source_class": "wire",
                   "organization": "benzinga", "role": "trader_macro"}]}
    out = main._collect_apify_entries(_FakeStore(), cfg, "cron")
    assert any(e["organization"] == "benzinga" and "Gold rips" in e["title"]
               for e in out)
