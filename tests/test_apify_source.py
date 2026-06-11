from datetime import datetime, timezone

from src import apify_source as ap


def test_pick_and_parse_dt():
    assert ap._pick({"a": "", "b": "x"}, ["a", "b"]) == "x"
    assert ap._pick({}, ["a"]) is None
    assert ap._parse_dt("2026-06-11T05:30:00.000Z").year == 2026
    assert ap._parse_dt(1749619800).tzinfo is timezone.utc
    assert ap._parse_dt("") is None


def test_tweet_to_entry_basic():
    e = ap._tweet_to_entry({
        "text": "Fed signals a pause   in hikes",
        "url": "https://x.com/DeItaone/status/123",
        "createdAt": "2026-06-11T05:30:00.000Z",
        "author": {"userName": "DeItaone"},
    })
    assert e["source_id"] == "x_deitaone"
    assert e["organization"] == "x_deitaone"
    assert e["source_class"] == "wire"
    assert e["title"] == "Fed signals a pause in hikes"   # whitespace collapsed
    assert e["url"].endswith("/123")
    assert e["published_ts"].year == 2026


def test_tweet_to_entry_builds_url_from_id():
    e = ap._tweet_to_entry({"text": "hi", "id": "999", "author": {"userName": "FirstSquawk"}})
    assert e["url"] == "https://x.com/FirstSquawk/status/999"


def test_tweet_to_entry_skips_retweets_and_empty():
    assert ap._tweet_to_entry({"text": "x", "url": "u", "isRetweet": True}) is None
    assert ap._tweet_to_entry({"text": "   ", "url": "u"}) is None
    assert ap._tweet_to_entry({"text": "no url no id"}) is None


def test_fetch_tweets_noop_without_token_or_handles():
    assert ap.fetch_tweets("", ["DeItaone"]) == []
    assert ap.fetch_tweets("tok", []) == []


def test_fetch_tweets_parses_response(monkeypatch):
    captured = {}

    class _Resp:
        def raise_for_status(self): pass
        def json(self):
            return [
                {"text": "Gold breaks 4400", "url": "https://x.com/KitcoNewsNOW/status/1",
                 "createdAt": "2026-06-11T05:00:00.000Z", "author": {"userName": "KitcoNewsNOW"}},
                {"text": "RT something", "url": "u", "isRetweet": True},   # dropped
            ]

    class _Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, url, params=None, json=None):
            captured["url"] = url
            captured["params"] = params
            captured["json"] = json
            return _Resp()

    monkeypatch.setattr(ap.httpx, "Client", _Client)
    out = ap.fetch_tweets("tok", ["KitcoNewsNOW"], since_minutes=20, max_per_handle=8)
    assert len(out) == 1                       # retweet dropped
    assert out[0]["source_id"] == "x_kitconewsnow"
    assert captured["params"]["token"] == "tok"
    assert captured["json"]["searchTerms"][0].startswith("from:KitcoNewsNOW since:")
    assert captured["json"]["maxItems"] == 8


def test_fetch_tweets_swallows_errors(monkeypatch):
    class _Boom:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, *a, **k): raise RuntimeError("network down")
    monkeypatch.setattr(ap.httpx, "Client", _Boom)
    assert ap.fetch_tweets("tok", ["DeItaone"]) == []
