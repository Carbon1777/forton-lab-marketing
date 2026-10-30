from unittest.mock import MagicMock, patch

from src import meta_post


def _resp(payload, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    r.text = str(payload)
    return r


def test_discover_returns_page_token_and_ig():
    with patch("src.meta_post.requests.get",
               return_value=_resp({"access_token": "PT", "instagram_business_account": {"id": "1784"}})):
        assert meta_post.discover_page_assets("P", "SU") == ("PT", "1784")


def test_discover_falls_back_on_error():
    with patch("src.meta_post.requests.get", return_value=_resp({"error": {"message": "x"}}, 400)):
        assert meta_post.discover_page_assets("P", "SU") == ("SU", None)
