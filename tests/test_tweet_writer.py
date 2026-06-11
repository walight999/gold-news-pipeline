import json

from src import tweet_writer as tw


def test_extract_json_plain_and_embedded():
    assert tw._extract_json('{"tweet":"hi"}') == {"tweet": "hi"}
    assert tw._extract_json('noise {"tweet":"x"} tail')["tweet"] == "x"
    assert tw._extract_json("not json") is None
    assert tw._extract_json("") is None


def test_sanitize_strips_dash_and_ensures_tags():
    out = tw._sanitize("Trump pressures — Iran")
    assert "—" not in out
    assert out.endswith(tw.TAGS)                      # tags appended when missing
    withtags = "news\n" + tw.TAGS
    assert tw._sanitize(withtags).count("ข่าวทอง") == 1  # #ข่าวทอง once


class _FakeResp:
    def __init__(self, text):
        self.content = [type("C", (), {"text": text})()]


class _FakeClient:
    def __init__(self, text):
        self.messages = type("M", (), {"create": lambda self, **kw: _FakeResp(text)})()


def _client_returning(tweet_text):
    return _FakeClient(json.dumps({"tweet": tweet_text}))


def test_compose_tweet_none_without_client(monkeypatch):
    monkeypatch.setattr(tw, "_get_anthropic_client", lambda: None)
    assert tw.compose_tweet(headline_th="x", body_th=[], impact_th=None,
                            category="", en_title="", en_summary="") is None


def test_compose_tweet_success(monkeypatch):
    tweet = "Trump pressures Netanyahu. Key point is the Iran deal is close.\n" + tw.TAGS
    monkeypatch.setattr(tw, "_get_anthropic_client", lambda: _client_returning(tweet))
    t = tw.compose_tweet(headline_th="h", body_th=["x"], impact_th="y",
                         category="Geopolitics", en_title="Trump pressures", en_summary="...")
    assert t is not None
    assert "—" not in t
    assert t.endswith(tw.TAGS)
    assert not any(c in t for c in ("\U0001F7E2", "\U0001F534", "\U0001F7E1"))


def test_compose_tweet_appends_tags_if_model_drops_them(monkeypatch):
    monkeypatch.setattr(tw, "_get_anthropic_client", lambda: _client_returning("a bare tweet"))
    t = tw.compose_tweet(headline_th="h", body_th=[], impact_th=None,
                         category="", en_title="", en_summary="")
    assert t.endswith(tw.TAGS)


def test_compose_tweet_trims_over_limit(monkeypatch):
    monkeypatch.setattr(tw, "_get_anthropic_client", lambda: _client_returning("word " * 200))
    t = tw.compose_tweet(headline_th="h", body_th=[], impact_th=None,
                         category="", en_title="", en_summary="")
    assert len(t) <= tw.TWEET_LIMIT
    assert t.endswith(tw.TAGS)


def test_compose_tweet_none_on_garbage(monkeypatch):
    monkeypatch.setattr(tw, "_get_anthropic_client", lambda: _FakeClient("totally not json"))
    assert tw.compose_tweet(headline_th="h", body_th=[], impact_th=None,
                            category="", en_title="", en_summary="") is None
