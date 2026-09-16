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
