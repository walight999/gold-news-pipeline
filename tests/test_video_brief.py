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


def test_build_payload_empty_voice_env_falls_back(monkeypatch):
    # Empty BRIEF_VOICE secret must fall back to DEFAULT_VOICE, not send "".
    monkeypatch.setenv("BRIEF_VOICE", "")
    p = vb.build_payload(vb.parse_scenes(REAL_SCRIPT), title="t")
    voice_el = next(e for e in p["scenes"][0]["elements"] if e["type"] == "voice")
    assert voice_el["voice"] == vb.DEFAULT_VOICE


def test_extract_card_text():
    assert vb.extract_card_text("การ์ดข้อความ 'US 10Y Yield: 5%'") == "US 10Y Yield: 5%"
    assert vb.extract_card_text('ภาพการ์ด "Fed 92.5%"') == "Fed 92.5%"
    assert vb.extract_card_text("ภาพตึก Federal Reserve") is None


def test_broll_query_pulls_english_nouns():
    assert "Federal Reserve" in vb.broll_query("ภาพตึก Federal Reserve วอชิงตัน")
    assert "Trading Floor" in vb.broll_query("ภาพ Trading Floor ตลาดหุ้น")
    # no Latin → Thai macro map / default, never empty, never gold-chart
    q = vb.broll_query("ภาพธนาคารกลางญี่ปุ่น")
    assert q and "chart" not in q.lower() and "gold" not in q.lower()


def test_resolve_visuals_without_pexels_key_uses_card_or_text():
    scenes = [{"n": 1, "cue": "การ์ด 'US 10Y 5%'", "narration": "x"},
              {"n": 2, "cue": "ภาพตึก Federal Reserve", "narration": "y"}]
    vis = vb.resolve_visuals(scenes, pexels_key=None)   # no key → no b-roll
    assert vis[0] == {"type": "card", "value": "US 10Y 5%"}
    assert vis[1] == {"type": "text", "value": "ภาพตึก Federal Reserve"}


def test_build_payload_visual_branches():
    scenes = [{"n": 1, "cue": "c1", "narration": "n1"},
              {"n": 2, "cue": "c2", "narration": "n2"},
              {"n": 3, "cue": "c3", "narration": "n3"}]
    visuals = [{"type": "broll", "value": "https://x/clip.mp4"},
               {"type": "card", "value": "US 10Y 5%"},
               {"type": "text", "value": "c3"}]
    p = vb.build_payload(scenes, title="t", visuals=visuals)
    s0, s1, s2 = p["scenes"]
    assert any(e["type"] == "video" and e["src"].endswith("clip.mp4")
               for e in s0["elements"])                 # b-roll → video bg
    assert any(e["type"] == "text" and e["text"] == "US 10Y 5%"
               for e in s1["elements"])                 # card → text
    assert any(e["type"] == "text" and e["text"] == "c3" for e in s2["elements"])
    # Text settings must be REAL CSS keys — `color` (not `font-color`, which
    # JSON2Video silently ignores) and a unit-bearing `font-size`.
    card_txt = next(e for e in s1["elements"] if e["type"] == "text")
    assert "font-color" not in card_txt["settings"]
    assert card_txt["settings"]["color"] == "#FFD34D"
    assert card_txt["settings"]["font-size"].endswith("px")
    # every scene still carries its TTS voice
    for sc in p["scenes"]:
        assert any(e["type"] == "voice" for e in sc["elements"])
