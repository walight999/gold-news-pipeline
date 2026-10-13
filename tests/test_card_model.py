"""2026-10-13: every card records which model wrote its Thai, so the next
quality review can split results by model (Haiku / Sonnet / Gemini / repair)."""
from __future__ import annotations

from types import SimpleNamespace

from src import main as m
from src import news_alert
from src.news_alert import MarketAlert


def test_model_round_trips_through_cache_json():
    a = MarketAlert(action="keep", headline_th="x", model="claude-haiku-4-5-20251001")
    assert MarketAlert.from_json(a.to_json()).model == "claude-haiku-4-5-20251001"
    # cache rows written before the field existed read back as ""
    old = '{"action": "keep", "headline_th": "x"}'
    assert MarketAlert.from_json(old).model == ""


def test_claude_path_stamps_model(monkeypatch):
    class _Resp:
        content = [SimpleNamespace(text='{"action": "keep", "headline_th": "h", "body_th": ["b"], "impact_th": "i"}')]
        usage = SimpleNamespace(input_tokens=1, output_tokens=1)

    class _Client:
        messages = SimpleNamespace(create=lambda **kw: _Resp())

    import src.translator as tr
    monkeypatch.setattr(tr, "_get_anthropic_client", lambda: _Client())
    monkeypatch.setenv("CLAUDE_CLASSIFY_MODEL", "claude-haiku-test")
    monkeypatch.setattr(news_alert, "_MIN_CLAUDE_GAP_S", 0)
    alert, _, _ = news_alert._classify_claude_with_usage("t", "", "src", "1.0")
    assert alert.model == "claude-haiku-test"
    alert, _, _ = news_alert._classify_claude_with_usage("t", "", "src", "1.0", high_quality=True)
    assert alert.model.startswith("claude-sonnet")


def test_repair_appends_repair_model(monkeypatch):
    class _Resp:
        content = [SimpleNamespace(text='{"headline_th": "fixed", "body_th": ["b"], "impact_th": "i"}')]

    class _Client:
        messages = SimpleNamespace(create=lambda **kw: _Resp())

    import src.translator as tr
    monkeypatch.setattr(tr, "_get_anthropic_client", lambda: _Client())
    monkeypatch.setenv("GRAMMAR_REPAIR_MODEL", "claude-sonnet-test")
    monkeypatch.setattr(news_alert, "_MIN_CLAUDE_GAP_S", 0)
    src_alert = MarketAlert(action="keep", headline_th="h", body_th=["b"], impact_th="i",
                            model="claude-haiku-test")
    out = news_alert._repair_grammar_llm(src_alert, ["english_leak"])
    assert out.model == "claude-haiku-test+repair:claude-sonnet-test"


def test_fallback_is_labelled(monkeypatch):
    import src.translator as tr
    monkeypatch.setattr(tr, "to_thai", lambda text, **kw: "ทองขึ้น")   # no network
    a = news_alert._fallback_alert("Gold rises", "", store=None)
    assert a.model == "google-translate-fallback"


def test_card_flags():
    a = MarketAlert(action="keep", model="gemini-2.0-flash")
    assert m._card_flags(a, "degraded") == "degraded model=gemini-2.0-flash"
    assert m._card_flags(a, "") == "model=gemini-2.0-flash"
    assert m._card_flags(MarketAlert(action="keep"), "") == ""
