"""Thai AI-tell list (docs/THAI-AI-TELLS.md) in the composer (2026-10-13)."""
from types import SimpleNamespace

from src import tweet_writer as tw

# Live posts from the review.
TEMPLATE = ("Sleijpen กรรมการ ECB ระบุว่าความคาดการณ์เงินเฟ้อในยุโรปยึดเหนี่ยวได้ดี สัญญาณ dovish แบบนี้"
            "เปิดทางให้ ECB ลดดอกเบี้ยต่อ กดดัน EUR และหนุนดอลลาร์ทางอ้อม ทองเผชิญแรงกดในระยะสั้น\n"
            "#ทองวันนี้ #ทองคำ")
CLEAN = ("ตลาดหุ้นสหรัฐฯ แบ่งทิศ หุ้นชิปฉุด Nasdaq ลง 0.7% ขณะ Dow ขยับขึ้น 0.4% "
         "ทุกสายตาจับ CPI สหรัฐฯ วันพุธ\n#ทองวันนี้ #ราคาทอง #เงินเฟ้อ #ทองคำ")


def test_template_post_hits_many_tells():
    hits = tw.thai_tells(TEMPLATE)
    for name in ("แบบนี้", "เปิดทาง", "ทางอ้อม", "แรงกด", "ระยะสั้น"):
        assert name in hits
    assert len(hits) >= 5


def test_clean_post_has_none_and_hashtags_ignored():
    assert tw.thai_tells(CLEAN) == []
    assert tw.thai_tells("ข่าว\n#ระยะสั้น #ทองคำ") == []


def test_prompt_carries_the_list_with_limit(monkeypatch):
    calls = []

    class C:
        messages = None

        def create(self, **kw):
            calls.append(kw["messages"][0]["content"])
            return SimpleNamespace(content=[SimpleNamespace(text='{"tweet": "ข่าว"}')])
    c = C()
    c.messages = c
    monkeypatch.setattr(tw, "_get_anthropic_client", lambda: c)
    tw.compose_tweet(headline_th="h", body_th=["b"], impact_th=None, category="",
                     en_title="Fed's Waller: x", en_summary=None, autopost=True)
    p = calls[0]
    assert "ไม่เกิน 2 วลีต่อโพสต์" in p and "ในระยะสั้น" in p and "เปิดทาง" in p
