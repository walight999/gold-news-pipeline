"""Rotating hashtag line + weak-gold-link backstop for auto-posts (2026-10-11)."""
import random

from src import squawk_mirror, tweet_writer as tw


# Real live posts 2026-10-08 that admitted a weak gold link -> must SKIP.
WEAK = [
    "Wunsch กรรมการ ECB บอกว่ายังไม่มีเหตุผลชัดพอ EUR มีแรงกด ผลต่อทองมีอยู่แต่ไม่ใช่ปัจจัยหลักในตอนนี้",
    "ชีฟอีโคโนมิสต์ BoE ส่งสัญญาณ hawkish ผลต่อทองมีอยู่แต่ยังไม่ใช่แรงขับหลัก",
    "Greene กรรมการ BoE ปฏิเสธแนวคิด แต่ผลต่อทองในภาพรวมยังเล็กน้อย",
    "BoJ ปรับมุมมองเศรษฐกิจขึ้น เยนแข็งค่า แต่ผลต่อทองทางตรงยังเล็กน้อย",
    "ข่าวนี้ผลต่อทองจำกัด",
]
# Real posts with a clear gold link -> must NOT be flagged.
STRONG = [
    "Jobless Claims สหรัฐฯ ลดลงสู่ 197K ต่ำกว่าคาด ดอลลาร์แข็งค่า กดทองในระยะสั้น",
    "Waller กรรมการ Fed ส่งสัญญาณ dovish กดดัน yields และดอลลาร์ ทองได้รับแรงหนุนจากทิศทางนี้",
    "FOMC Minutes ส่งสัญญาณ Fed พร้อมขึ้นดอกเบี้ยต่อ กดทองร่วงใต้ $4,100",
]


def test_weak_link_phrases_flagged():
    for t in WEAK:
        assert tw.is_weak_link(t), t


def test_strong_link_not_flagged():
    for t in STRONG:
        assert not tw.is_weak_link(t), t


def test_pick_tags_keeps_core_and_varies():
    rng = random.Random(1)
    lines = {tw.pick_tags("ECB speaker", rng) for _ in range(30)}
    assert len(lines) > 3
    for line in lines:
        tags = line.split()
        assert tags[0] == "#ทองวันนี้" and tags[-1] == "#ทองคำ"
        assert len(tags) == 4 and len(set(tags)) == 4


def test_pick_tags_adds_topic_tag():
    for _ in range(10):
        assert "#เฟด" in tw.pick_tags("Fed Waller says pause")


def test_sanitize_swaps_model_tag_line():
    tags = "#ทองวันนี้ #เฟด #ราคาทอง #ทองคำ"
    out = tw._sanitize("ข่าวเฟด ทองขึ้น\n" + tw.TAGS, tags)
    assert out == "ข่าวเฟด ทองขึ้น\n" + tags
    assert tw.TAGS not in out


def test_fit_keeps_rotated_tags():
    tags = "#ทองวันนี้ #ดอลลาร์ #ข่าวทอง #ทองคำ"
    out = tw._fit("ก " * 200 + "\n" + tags, tags)
    assert len(out) <= tw.TWEET_LIMIT and out.endswith(tags)


def test_closing_drops_rotated_tags():
    tweet = "Fed ส่งสัญญาณ ทองได้แรงหนุน\n#ทองวันนี้ #เฟด #ราคาทอง #ทองคำ"
    assert "#" not in squawk_mirror._closing(tweet)
