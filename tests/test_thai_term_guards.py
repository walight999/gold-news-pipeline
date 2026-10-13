"""2026-10-13 read of 1,499 sent cards: deterministic guards.

- ภูมิศาสตร์ / ธรณีศาสตร์ used for "geopolitical" (178 cards) → ภูมิรัฐศาสตร์
- "minutes" rendered as นาที (FOMC/BoJ minutes) → รายงานการประชุม
- Arabic / Cyrillic / Greek / Indic letters inside Thai copy → flagged for
  repair, and rejected if the repair can't remove them."""
from __future__ import annotations

from src import news_alert
from src.news_alert import MarketAlert
from src.thai_grammar import grammar_warnings, has_foreign_script
from src.translator import _patch_places


def test_geopolitics_word_patched():
    assert _patch_places("ความเสี่ยงทางภูมิศาสตร์หนุนทอง") == "ความเสี่ยงทางภูมิรัฐศาสตร์หนุนทอง"
    assert _patch_places("ความตึงเครียดทางธรณีศาสตร์") == "ความตึงเครียดทางภูมิรัฐศาสตร์"


def test_correct_geopolitics_word_untouched():
    s = "ความเสี่ยงทางภูมิรัฐศาสตร์ยังสูง"
    assert _patch_places(s) == s
    assert _patch_places(_patch_places(s)) == s          # idempotent


def test_minutes_patched():
    assert _patch_places("นาทีประชุม FOMC เผยกรรมการหลายคน") == "รายงานการประชุม FOMC เผยกรรมการหลายคน"
    assert _patch_places("เอกสารนาทีการประชุม BoJ") == "รายงานการประชุม BoJ"
    # an ordinary minute count is left alone
    assert _patch_places("ทองขึ้น $12 ใน 15 นาที") == "ทองขึ้น $12 ใน 15 นาที"


def test_foreign_script_flagged():
    for bad in ("มุมมองแบบดовish", "นেตันยาฮู กล่าว", "ลิختเชนสไตน์", "ดอกเบี้য"):
        assert has_foreign_script(bad), bad
        assert "foreign_script" in grammar_warnings(bad), bad
    for ok in ("Fed ส่งสัญญาณ hawkish", "ทองแตะ $4,190", "สายเหยี่ยว (hawkish)"):
        assert not has_foreign_script(ok), ok
        assert "foreign_script" not in grammar_warnings(ok), ok


def _alert(headline):
    return MarketAlert(action="keep", headline_th=headline, body_th=["เนื้อหา"],
                       impact_th="ผลกระทบต่อทองคำยังไม่ชัดเจน")


def test_foreign_script_rejected_when_repair_fails(monkeypatch):
    monkeypatch.setattr(news_alert, "_grammar_repair_enabled", lambda: True)
    monkeypatch.setattr(news_alert, "_repair_grammar_llm", lambda a, w: None)
    out = news_alert._apply_grammar_qc(_alert("ECB ส่งสัญญาณ ดовish"))
    assert out.action == "reject" and out.reason == "foreign-script-in-rewrite"


def test_foreign_script_kept_when_repair_fixes_it(monkeypatch):
    fixed = _alert("ECB ส่งสัญญาณ dovish")
    monkeypatch.setattr(news_alert, "_grammar_repair_enabled", lambda: True)
    monkeypatch.setattr(news_alert, "_repair_grammar_llm", lambda a, w: fixed)
    out = news_alert._apply_grammar_qc(_alert("ECB ส่งสัญญาณ ดовish"))
    assert out.action == "keep" and out.headline_th == "ECB ส่งสัญญาณ dovish"


def test_foreign_script_rejected_even_with_repair_disabled(monkeypatch):
    monkeypatch.setattr(news_alert, "_grammar_repair_enabled", lambda: False)
    out = news_alert._apply_grammar_qc(_alert("นেตันยาฮู กล่าว"))
    assert out.action == "reject"
