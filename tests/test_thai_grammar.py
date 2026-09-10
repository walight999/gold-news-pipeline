"""Thai grammar QC — rule detection (free, high-precision) + the hybrid repair
gate in news_alert. The rules must fire on real MT artifacts and stay SILENT on
clean desk-Thai (incl. a gold PRICE in the 25xx range, which must not read as a
Buddhist-Era year)."""
from __future__ import annotations

from src import news_alert as na
from src import thai_grammar as tg
from src.news_alert import MarketAlert


# ---------------- rule detection ----------------

def test_flags_known_mt_artifacts():
    assert "mt:ดึงเอียง" in tg.grammar_warnings("ดอลลาร์ดึงเอียงตลาดทอง")
    assert "mt:ในซื้อขาย" in tg.grammar_warnings("ทองปรับขึ้นในซื้อขายวันศุกร์")
    assert "mt:polymarket-mistranslated" in tg.grammar_warnings("ตลาดเทพเจ้าคาดโอกาส 90%")


def test_flags_leaked_english_verbs():
    # Distinctive MT verbs/preps only — short ambiguous words ("as"/"to") are
    # deliberately NOT flagged to avoid false positives.
    assert "english_leak" in tg.grammar_warnings("ทองอ่อนตัว ahead of CPI")
    assert "english_leak" in tg.grammar_warnings("ทองร่วง amid ความเสี่ยง")
    assert "english_leak" in tg.grammar_warnings("ทอง soaring หลังตัวเลข")
    # "as" is intentionally NOT flagged (too ambiguous / short).
    assert "english_leak" not in tg.grammar_warnings("ทองอ่อนตัว as ดอลลาร์แข็ง")


def test_flags_dangling_connector():
    assert "dangling" in tg.grammar_warnings("ราคาทองปรับขึ้นหลังตัวเลข CPI และ")
    assert "dangling" not in tg.grammar_warnings("ราคาทองปรับขึ้นหลังตัวเลข CPI")


def test_buddhist_era_flagged_but_gold_price_is_not():
    assert "buddhist_era" in tg.grammar_warnings("ประชุมปี 2568 สำคัญ")
    assert "buddhist_era" in tg.grammar_warnings("รายงานเมื่อ พ.ศ. 2568")
    # A gold PRICE in the 25xx-4400 range is NOT a year — must stay clean.
    assert tg.grammar_warnings("ทองแตะ 2563 ดอลลาร์ต่อออนซ์") == []
    assert tg.grammar_warnings("ทองทะลุ 4400 ดอลลาร์") == []


def test_clean_desk_thai_has_no_warnings():
    for s in [
        "Fed คงดอกเบี้ย 5.5% กดดันทองคำระยะสั้น",
        "ทองอ่อนตัว หลังดอลลาร์แข็งค่าก่อนตัวเลข CPI",
        "Core CPI ญี่ปุ่นชะลอเหลือ 1.4% y/y ต่ำกว่าคาด",
        "ราคาทองปรับขึ้นระหว่างวันหลังตัวเลขจ้างงานอ่อนแอ",
    ]:
        assert tg.grammar_warnings(s) == [], s


def test_alert_grammar_warnings_aggregates_across_fields():
    w = tg.alert_grammar_warnings(
        headline="ทองดึงเอียง",
        body=["ปกติดี", "ทองร่วง amid ความเสี่ยง"],
        impact="ผลกระทบยังไม่ชัด")
    assert "mt:ดึงเอียง" in w and "english_leak" in w


# ---------------- hybrid QC gate ----------------

def _flagged_alert():
    return MarketAlert(action="keep", category="Inflation",
                       headline_th="ทองดึงเอียงตลาด",
                       body_th=["ดอลลาร์แข็ง amid ความเสี่ยง"],
                       impact_th="กดดันทอง")


def test_qc_gate_flags_and_skips_llm_when_disabled(monkeypatch):
    monkeypatch.setenv("GRAMMAR_LLM_REPAIR", "0")
    # LLM repair must NOT be called when disabled.
    monkeypatch.setattr(na, "_repair_grammar_llm",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not repair")))
    out = na._apply_grammar_qc(_flagged_alert())
    assert out.grammar_flags  # warnings recorded for visibility
    assert "mt:ดึงเอียง" in out.grammar_flags


def test_qc_gate_keeps_repair_when_it_reduces_warnings(monkeypatch):
    monkeypatch.setenv("GRAMMAR_LLM_REPAIR", "1")
    clean = MarketAlert(action="keep", category="Inflation",
                        headline_th="ทองอ่อนตัวกดดันตลาด",
                        body_th=["ดอลลาร์แข็งค่าเพิ่มความเสี่ยง"],
                        impact_th="กดดันทอง")
    monkeypatch.setattr(na, "_repair_grammar_llm", lambda alert, issues: clean)
    out = na._apply_grammar_qc(_flagged_alert())
    assert out is clean
    assert out.grammar_flags == []          # repaired copy is clean


def test_qc_gate_keeps_original_when_repair_fails_or_worsens(monkeypatch):
    monkeypatch.setenv("GRAMMAR_LLM_REPAIR", "1")
    original = _flagged_alert()
    # repair returns None (failure) → keep original, flags recorded
    monkeypatch.setattr(na, "_repair_grammar_llm", lambda alert, issues: None)
    out = na._apply_grammar_qc(original)
    assert out is original
    assert out.grammar_flags  # still flagged so content_log / logs see it


def test_qc_gate_noop_on_clean_card(monkeypatch):
    monkeypatch.setattr(na, "_repair_grammar_llm",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("clean card must not repair")))
    clean = MarketAlert(action="keep", headline_th="ทองปรับขึ้นหลัง CPI ต่ำกว่าคาด",
                        body_th=["ดอลลาร์อ่อนค่าหนุนทอง"], impact_th="บวกต่อทอง")
    out = na._apply_grammar_qc(clean)
    assert out is clean and out.grammar_flags == []
