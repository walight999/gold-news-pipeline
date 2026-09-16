from src import drive_upload as du


def test_view_url_encodes_id():
    assert du.view_url("ABC123") == "https://drive.google.com/uc?export=view&id=ABC123"


def test_upload_png_none_on_empty_data():
    assert du.upload_png(b"", name="x.png") is None


def test_download_none_on_empty_id():
    assert du.download("") is None


def test_access_token_none_without_creds(monkeypatch):
    monkeypatch.delenv("GSHEET_CREDS", raising=False)
    assert du._access_token() is None
