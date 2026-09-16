from src import fb_publish as fb


def _todo(label, checked):
    return {"type": "to_do", "to_do": {
        "rich_text": [{"plain_text": label}], "checked": checked}}


def test_todo_checked_matches_label():
    blocks = [
        {"type": "heading_2", "heading_2": {"rich_text": []}},
        _todo("อนุมัติบทความ Facebook", True),
        _todo("อนุมัติบทพูด video", False),
    ]
    assert fb._todo_checked(blocks, fb.FB_APPROVE_LABEL) is True
    assert fb._todo_checked(blocks, "อนุมัติบทพูด video") is False


def test_todo_checked_unchecked_and_missing():
    blocks = [_todo("อนุมัติบทความ Facebook", False)]
    assert fb._todo_checked(blocks, fb.FB_APPROVE_LABEL) is False
    assert fb._todo_checked([], fb.FB_APPROVE_LABEL) is False   # label absent


def test_todo_checked_handles_split_rich_text():
    # Notion often splits a label across multiple rich_text runs.
    block = {"type": "to_do", "to_do": {
        "rich_text": [{"plain_text": "อนุมัติบทความ "}, {"plain_text": "Facebook"}],
        "checked": True}}
    assert fb._todo_checked([block], fb.FB_APPROVE_LABEL) is True


def test_is_fb_approved_fail_closed_without_token():
    assert fb.is_fb_approved("page123", "") is False
    assert fb.is_fb_approved("", "tok") is False


def test_is_fb_approved_reads_children(monkeypatch):
    monkeypatch.setattr(fb, "_notion_children",
                        lambda pid, tok: [_todo(fb.FB_APPROVE_LABEL, True)])
    assert fb.is_fb_approved("page123", "tok") is True


def test_is_fb_approved_fail_closed_on_error(monkeypatch):
    def boom(pid, tok):
        raise RuntimeError("notion down")
    monkeypatch.setattr(fb, "_notion_children", boom)
    assert fb.is_fb_approved("page123", "tok") is False


def test_post_to_page_none_on_empty_args():
    assert fb.post_to_page("", page_id="p", token="t") is None
    assert fb.post_to_page("msg", page_id="", token="t") is None
    assert fb.post_to_page("msg", page_id="p", token="") is None


def test_is_approved_generic_reads_video_label(monkeypatch):
    monkeypatch.setattr(fb, "_notion_children",
                        lambda pid, tok: [_todo(fb.VIDEO_APPROVE_LABEL, True)])
    assert fb.is_approved("p", "t", fb.VIDEO_APPROVE_LABEL) is True
    # the FB wrapper still works and is independent of the video label
    monkeypatch.setattr(fb, "_notion_children",
                        lambda pid, tok: [_todo(fb.FB_APPROVE_LABEL, False)])
    assert fb.is_fb_approved("p", "t") is False


def test_post_reel_none_on_empty_args():
    assert fb.post_reel("", page_id="p", token="t") is None
    assert fb.post_reel("http://v/x.mp4", page_id="", token="t") is None
    assert fb.post_reel("http://v/x.mp4", page_id="p", token="") is None


def test_attach_video_to_notion_none_on_empty_args():
    assert fb.attach_video_to_notion("", "tok", "http://v/x.mp4") is False
    assert fb.attach_video_to_notion("page", "", "http://v/x.mp4") is False
    assert fb.attach_video_to_notion("page", "tok", "") is False


def test_post_photo_none_on_empty_args():
    assert fb.post_photo(b"", page_id="p", token="t") is None
    assert fb.post_photo(b"bytes", page_id="", token="t") is None
    assert fb.post_photo(b"bytes", page_id="p", token="") is None


def test_attach_image_to_notion_none_on_empty_args():
    assert fb.attach_image_to_notion("", "tok", "http://i/x.png") is False
    assert fb.attach_image_to_notion("page", "", "http://i/x.png") is False
    assert fb.attach_image_to_notion("page", "tok", "") is False


def test_checked_tweet_indices(monkeypatch):
    blocks = [
        _todo("Tweet 1", True),
        _todo("Tweet 2", False),
        _todo("Tweet 3", True),
        _todo("อนุมัติบทความ Facebook", True),   # not a tweet → ignored
    ]
    monkeypatch.setattr(fb, "_notion_children", lambda pid, tok: blocks)
    assert fb.checked_tweet_indices("p", "t") == {1, 3}


def test_checked_tweet_indices_fail_closed():
    assert fb.checked_tweet_indices("", "t") == set()
    assert fb.checked_tweet_indices("p", "") == set()
