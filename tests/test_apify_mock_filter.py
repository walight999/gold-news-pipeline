"""The Apify actor's billing-notice record must never become a news event.

kaitoeasyapi charges a minimum per call even when the search matches nothing,
and returns a NOTICE record instead of an empty list. It has no author and a
tweet id of -1, so it used to normalize into source_id `x_x` with the url
https://x.com/x/status/-1 — 119 junk rows in event_state over 8 days, each one
paying for a Claude classification call and inflating the `other` topic bucket.
"""
from __future__ import annotations

from src import apify_source as ap

# Verbatim shape of the record observed in event_state on 2026-08-05..13.
NOTICE_TEXT = (
    "From KaitoEasyAPI, a reminder:Our API pricing is based on the volume of "
    "data returned. However, to ensure we can cover our costs on the Apify "
    "platform, we have a minimum charge of $X per API call,even if the response "
    "contains no results.Thus, we returned N pieces of mock data. We"
)
NOTICE_ITEM = {"text": NOTICE_TEXT, "id": -1}


def test_billing_notice_record_is_dropped():
    assert ap._tweet_to_entry(NOTICE_ITEM) is None


def test_billing_notice_is_dropped_even_with_a_url():
    """Belt and braces: the actor may start stamping a url on the notice."""
    item = dict(NOTICE_ITEM, url="https://x.com/x/status/-1")
    assert ap._tweet_to_entry(item) is None


def test_record_without_an_author_is_dropped():
    """The filter keys on STRUCTURE, not on the notice wording — the actor is
    free to reword it. No author means we cannot attribute it, so it is not
    news regardless of what the text says."""
    assert ap._tweet_to_entry({"text": "Gold breaks 4400", "id": 12345}) is None


def test_non_snowflake_id_is_dropped():
    for bad in (-1, 0, "-1", "not-an-id"):
        assert ap._tweet_to_entry(
            {"text": "x", "id": bad, "author": {"userName": "FirstSquawk"}}
        ) is None, bad


def test_real_tweets_still_pass():
    e = ap._tweet_to_entry({
        "text": "FED'S WALLER: RATE CUT APPROPRIATE AT NEXT MEETING",
        "id": "1954321987654321098",
        "author": {"userName": "FirstSquawk"},
        "createdAt": "2026-08-13T12:00:00.000Z",
    })
    assert e is not None
    assert e["source_id"] == "x_firstsquawk"
    assert e["url"] == "https://x.com/FirstSquawk/status/1954321987654321098"


def test_real_tweet_with_url_and_no_id_still_passes():
    """Some actor responses carry `url` but no separate id field — the id guard
    must not reject those."""
    e = ap._tweet_to_entry({
        "text": "Gold hits record",
        "url": "https://x.com/KitcoNewsNOW/status/1954000000000000000",
        "author": {"userName": "KitcoNewsNOW"},
    })
    assert e is not None
    assert e["source_id"] == "x_kitconewsnow"


def test_fetch_tweets_drops_the_notice_and_keeps_the_tweet(monkeypatch):
    """End-to-end through fetch_tweets: a mixed batch keeps only real tweets."""
    class _Resp:
        def raise_for_status(self): pass
        def json(self):
            return [
                NOTICE_ITEM,
                {"text": "ECB'S LAGARDE SPEAKS", "id": "1954321987654321099",
                 "author": {"userName": "DeItaone"}},
            ]

    class _Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, *a, **k): return _Resp()

    monkeypatch.setattr(ap.httpx, "Client", _Client)
    out = ap.fetch_tweets("tok", ["DeItaone"])

    assert len(out) == 1
    assert out[0]["source_id"] == "x_deitaone"
    assert not any(o["source_id"] == "x_x" for o in out)
