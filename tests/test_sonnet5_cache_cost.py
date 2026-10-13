"""2026-10-14: Sonnet 5 on both classifier routes, prompt caching of the static
rules block, dollar-based monthly brake, retired Gemini default replaced."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src import news_alert as na
from src.news_alert import MarketAlert, classify_and_rewrite


def _usage(i=0, cw=0, cr=0, o=0):
    return SimpleNamespace(input_tokens=i, cache_creation_input_tokens=cw,
                           cache_read_input_tokens=cr, output_tokens=o)


def test_cost_math_prices_cache_reads_and_writes():
    # Sonnet 5 $2/$10: 100 fresh + 1000 written (1.25x) + 7000 read (0.1x) in, 400 out
    c = na.claude_cost_usd("claude-sonnet-5", _usage(100, 1000, 7000, 400))
    assert c == pytest.approx((100 * 2 + 1000 * 2.5 + 7000 * 0.2 + 400 * 10) / 1e6)
    # dated id resolves by prefix; unknown model costs 0 (and warns)
    assert na.claude_cost_usd("claude-haiku-4-5-20251001", _usage(1_000_000)) == pytest.approx(1.0)
    assert na.claude_cost_usd("mystery-model", _usage(1_000_000)) == 0.0


def test_thinking_kwargs(monkeypatch):
    monkeypatch.delenv("CLAUDE_CLASSIFY_THINKING", raising=False)
    assert na._claude_thinking_kwargs("claude-sonnet-5") == {
        "thinking": {"type": "adaptive"}, "output_config": {"effort": "low"}}
    assert na._claude_thinking_kwargs("claude-haiku-4-5") == {}
    assert na._claude_thinking_kwargs("claude-sonnet-4-6") == {}
    monkeypatch.setenv("CLAUDE_CLASSIFY_THINKING", "off")
    assert na._claude_thinking_kwargs("claude-sonnet-5") == {"thinking": {"type": "disabled"}}


def test_claude_request_caches_static_rules(monkeypatch):
    seen = {}

    class _Msgs:
        def create(self, **kw):
            seen.update(kw)
            return SimpleNamespace(
                content=[SimpleNamespace(type="thinking", thinking=""),
                         SimpleNamespace(type="text", text='{"action": "keep", "headline_th": "h"}')],
                usage=_usage(50, 0, 7500, 300))

    monkeypatch.setattr("src.translator._get_anthropic_client",
                        lambda: SimpleNamespace(messages=_Msgs()))
    monkeypatch.setattr(na, "_MIN_CLAUDE_GAP_S", 0)
    monkeypatch.delenv("CLAUDE_CLASSIFY_MODEL", raising=False)
    monkeypatch.delenv("CLAUDE_CLASSIFY_THINKING", raising=False)
    alert, tin, tout = na._classify_claude_with_usage("Fed's Waller speaks", "", "src", "1.0")
    assert seen["model"] == "claude-sonnet-5"                      # new default
    assert seen["thinking"] == {"type": "adaptive"}
    assert seen["output_config"] == {"effort": "low"} and seen["max_tokens"] == 2000
    sysblock = seen["system"][0]
    assert sysblock["cache_control"] == {"type": "ephemeral"}
    assert "GOLD DIRECTION RULES" in sysblock["text"]
    assert "Fed's Waller speaks" not in sysblock["text"]           # per-item input stays out
    assert "Fed's Waller speaks" in seen["messages"][0]["content"]
    assert alert.headline_th == "h"                                 # text block after thinking
    assert (tin, tout) == (7550, 300)
    assert alert.cost_usd == pytest.approx(na.claude_cost_usd("claude-sonnet-5", _usage(50, 0, 7500, 300)))


@pytest.fixture
def store():
    from src import store as store_mod
    s = store_mod.Store(sheet_id="x", creds_json="{}")
    for tab in store_mod.SCHEMAS:
        s.data[tab] = {}
    return s


def _health(store, **blob):
    from src.utils_time import now_ict
    store.upsert("source_state", {"source_id": "_classifier_health", "items_last_hour": json.dumps(
        {"buckets": [], "month": now_ict().strftime("%Y-%m"), **blob})})


def test_dollar_cap_skips_claude(store, monkeypatch):
    monkeypatch.setenv("CLASSIFIER_MONTHLY_USD_CAP", "10")
    monkeypatch.delenv("CLASSIFIER_MONTHLY_TOKEN_CAP", raising=False)
    _health(store, month_cost_usd=10.5)
    kept = MarketAlert(action="keep", headline_th="จาก Gemini")
    with patch("src.news_alert._classify_claude_with_usage") as m_claude, \
         patch("src.news_alert._classify_gemini_with_usage", return_value=(kept, 1, 1)):
        out = classify_and_rewrite("CPI hotter", "x", store=store)
    m_claude.assert_not_called()
    assert out.headline_th == "จาก Gemini"


def test_token_count_alone_no_longer_trips_by_default(store, monkeypatch):
    # 9.4M tokens was where the old 8M default would have tripped mid-month
    monkeypatch.delenv("CLASSIFIER_MONTHLY_TOKEN_CAP", raising=False)
    monkeypatch.delenv("CLASSIFIER_MONTHLY_USD_CAP", raising=False)
    _health(store, month_tokens_in=9_000_000, month_tokens_out=400_000, month_cost_usd=12.0)
    assert na._month_spend_over_cap(store) is False


def test_cost_accumulates_monthly(store):
    a = MarketAlert(action="keep", headline_th="x")
    na._record_classifier_outcome(store, "s", a, used_fallback=False, cache_hit=False,
                                  tokens_in=10, tokens_out=5, cost_usd=0.006)
    na._record_classifier_outcome(store, "s", a, used_fallback=False, cache_hit=False,
                                  tokens_in=10, tokens_out=5, high_quality=True, cost_usd=0.004)
    c = na.get_classifier_counters(store, source_id=None)
    assert c["month_cost_usd"] == pytest.approx(0.010)
    blob = json.loads(store.get("source_state", ("_classifier_health",))["items_last_hour"])
    assert blob["month_hq_cost_usd"] == pytest.approx(0.004)


def test_gemini_default_is_not_the_retired_model():
    assert na.GEMINI_DEFAULT_MODEL != "gemini-2.0-flash"
    import inspect
    src = inspect.getsource(na)
    assert '"gemini-2.0-flash").strip()' not in src


def test_gemini_error_reason_masks_ids():
    class _R:
        status_code = 402
        text = ""

        def json(self):
            return {"error": {"code": 402, "status": "FAILED_PRECONDITION",
                              "message": "Billing is disabled for project 123456789012."}}
    assert na._gemini_error_reason(_R()) == "FAILED_PRECONDITION: Billing is disabled for project <n>."

    class _Bad:
        status_code = 402
        text = "not json"

        def json(self):
            raise ValueError
    assert na._gemini_error_reason(_Bad()) == "not json"
