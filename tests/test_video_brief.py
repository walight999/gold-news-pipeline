from src import video_brief as vb

# A realistic script in the exact shape daily_brief now emits (colon markers,
# Thai-transliterated jargon, spelled numbers).
REAL_SCRIPT = """[ฉาก 1: ภาพกราฟทองร่วง พร้อมตัวเลข 4,300 ดอลลาร์]
วันนี้ทองปรับตัวลงใต้สี่พันสามร้อยดอลลาร์ หลังตลาดเผชิญแรงกดดันพร้อมกันสองด้าน

[ฉาก 2: ภาพกราฟ Yield พันธบัตร 10 ปี แตะเส้น 5%]
ผลตอบแทนพันธบัตรสหรัฐฯ อายุสิบปี แตะห้าเปอร์เซ็นต์ ระดับสูงสุดตั้งแต่ปีสองพันเจ็ด
Yield ที่สูงระดับนี้เพิ่มต้นทุนโอกาสการถือทอง

[ฉาก 3: ภาพโลโก้ Tradetongkam]
สรุปวันนี้ แรงกดดันขาลงยังหนักกว่า ติดตามได้ที่ Tradetongkam"""


def test_parse_scenes_real_format():
    scenes = vb.parse_scenes(REAL_SCRIPT)
    assert [s["n"] for s in scenes] == [1, 2, 3]
    assert scenes[0]["cue"].startswith("ภาพกราฟทองร่วง")
    # scene 2 has two narration lines merged into one spoken block
    assert "ห้าเปอร์เซ็นต์" in scenes[1]["narration"]
    assert "เพิ่มต้นทุนโอกาส" in scenes[1]["narration"]
    assert scenes[2]["narration"].endswith("Tradetongkam")


def test_parse_scenes_tolerates_dash_and_space_markers():
    script = "[ฉาก 1 - คิว]\nพูดหนึ่ง\n[ฉาก 2  คิวสอง]\nพูดสอง"
    scenes = vb.parse_scenes(script)
    assert len(scenes) == 2
    assert scenes[1]["cue"] == "คิวสอง"


def test_parse_scenes_drops_empty_and_preamble():
    script = "อารัมภบทก่อนฉากแรก ถูกทิ้ง\n[ฉาก 1: คิว]\n\nพูด\n[ฉาก 2: ว่าง]"
    scenes = vb.parse_scenes(script)
    # scene 2 has no narration → dropped; preamble ignored
    assert len(scenes) == 1
    assert scenes[0]["narration"] == "พูด"


def test_parse_scenes_empty():
    assert vb.parse_scenes("") == []
    assert vb.parse_scenes("no markers at all") == []


def test_build_payload_shape():
    scenes = vb.parse_scenes(REAL_SCRIPT)
    p = vb.build_payload(scenes, title="Gold Daily Brief — 16 Sep 2026")
    assert p["width"] == vb.WIDTH and p["height"] == vb.HEIGHT   # vertical
    assert len(p["scenes"]) == 3
    # every scene carries a TTS voice element with its narration
    for src_scene, out_scene in zip(scenes, p["scenes"]):
        voices = [e for e in out_scene["elements"] if e["type"] == "voice"]
        assert len(voices) == 1
        assert voices[0]["text"] == src_scene["narration"]
    # one global karaoke subtitles element, center screen
    subs = [e for e in p["elements"] if e["type"] == "subtitles"]
    assert len(subs) == 1
    assert subs[0]["settings"]["position"] == "center-center"


def test_build_payload_voice_override(monkeypatch):
    monkeypatch.setenv("BRIEF_VOICE", "th-TH-NiwatNeural")
    p = vb.build_payload(vb.parse_scenes(REAL_SCRIPT), title="t")
    voice_el = next(e for e in p["scenes"][0]["elements"] if e["type"] == "voice")
    assert voice_el["voice"] == "th-TH-NiwatNeural"
