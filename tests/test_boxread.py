"""Box の資料の読み取り（読み取り専用の窓口）のテスト。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from raizuinu.boxread import BoxClient, BoxError, write_permissions
from raizuinu.config import Config

API = "https://api.box.com"
CLIENT_ID = "client-id-abcdef"
SECRET = "client-secret-123456"
ENTERPRISE = "1234567890"
ROOT = "226145855021"
TOKEN_OK = (200, {"access_token": "box-token-xyz", "expires_in": 3600, "token_type": "bearer"})


def make_config(tmp_path, monkeypatch, secrets=True):
    config = Config.load(tmp_path / "no-config.json")
    config.data["box"]["root_folder_id"] = ROOT
    config.base_dir = tmp_path
    values = {"BOX_CLIENT_ID": CLIENT_ID, "BOX_CLIENT_SECRET": SECRET, "BOX_ENTERPRISE_ID": ENTERPRISE}
    for name, value in values.items():
        if secrets:
            monkeypatch.setenv(name, value)
        else:
            monkeypatch.delenv(name, raising=False)
    return config


class FakeHttp:
    """requests.request の差し替え。呼ばれた内容を記録する。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def __call__(self, method, url, params=None, data=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params, "data": data, "headers": headers, "timeout": timeout})
        status, body = self._responses.pop(0)
        if isinstance(body, bytes):
            return SimpleNamespace(status_code=status, text="", content=body, json=lambda: {})
        text = body if isinstance(body, str) else json.dumps(body)
        return SimpleNamespace(status_code=status, text=text, content=text.encode(), json=lambda: json.loads(text))


def client_with(tmp_path, monkeypatch, responses, now=None):
    http = FakeHttp(responses)
    clock = now or (lambda: 1000.0)
    return BoxClient(make_config(tmp_path, monkeypatch), http=http, now=clock), http


class TestToken:
    def test_missing_secrets_are_named_without_values(self, tmp_path, monkeypatch):
        client = BoxClient(make_config(tmp_path, monkeypatch, secrets=False), http=FakeHttp([]))
        assert client.missing_secrets() == ["BOX_CLIENT_ID", "BOX_CLIENT_SECRET", "BOX_ENTERPRISE_ID"]
        with pytest.raises(BoxError, match="BOX_CLIENT_SECRET"):
            client.token()

    def test_the_app_signs_in_as_its_service_account(self, tmp_path, monkeypatch):
        client, http = client_with(tmp_path, monkeypatch, [TOKEN_OK])
        assert client.token() == "box-token-xyz"
        call = http.calls[0]
        assert (call["method"], call["url"]) == ("POST", f"{API}/oauth2/token")
        assert call["data"] == {
            "grant_type": "client_credentials", "client_id": CLIENT_ID, "client_secret": SECRET,
            "box_subject_type": "enterprise", "box_subject_id": ENTERPRISE,
        }
        assert "Authorization" not in call["headers"]

    def test_the_token_is_reused_until_shortly_before_it_expires(self, tmp_path, monkeypatch):
        clock = {"now": 1000.0}
        client, http = client_with(tmp_path, monkeypatch, [TOKEN_OK, TOKEN_OK], now=lambda: clock["now"])
        client.token()
        clock["now"] += 3400  # 寿命 3600 秒の 120 秒手前まで使う
        client.token()
        assert len(http.calls) == 1
        clock["now"] += 100
        client.token()
        assert len(http.calls) == 2

    def test_pasted_values_lose_their_spaces_and_quotes(self, tmp_path, monkeypatch):
        config = make_config(tmp_path, monkeypatch)
        monkeypatch.setenv("BOX_CLIENT_SECRET", f'  "{SECRET}" ')
        monkeypatch.setenv("BOX_ENTERPRISE_ID", "   ")
        assert config.box_client_secret == SECRET
        assert BoxClient(config, http=FakeHttp([])).missing_secrets() == ["BOX_ENTERPRISE_ID"]

    def test_a_refused_sign_in_carries_the_reason_but_no_secret(self, tmp_path, monkeypatch):
        refused = (400, {"error": "invalid_grant", "error_description": f"Grant credentials are invalid [{SECRET}] for {ENTERPRISE}"})
        client, _ = client_with(tmp_path, monkeypatch, [refused])
        with pytest.raises(BoxError) as exc:
            client.token()
        assert (exc.value.status, exc.value.code) == (400, "invalid_grant")
        text = str(exc.value)
        assert "invalid_grant" in text and SECRET not in text and ENTERPRISE not in text and CLIENT_ID not in text

    def test_a_network_failure_is_reported_without_the_request(self, tmp_path, monkeypatch):
        def broken(*args, **kwargs):
            raise ConnectionError(f"could not post client_secret={SECRET}")

        client = BoxClient(make_config(tmp_path, monkeypatch), http=broken)
        with pytest.raises(BoxError) as exc:
            client.token()
        assert "ConnectionError" in str(exc.value) and SECRET not in str(exc.value)


class TestReading:
    def test_a_folder_is_read_with_the_permissions_of_the_service_account(self, tmp_path, monkeypatch):
        folder = {"id": ROOT, "name": "①月次税務資料提出フォルダ（ライズG）", "permissions": {"can_download": True, "can_upload": False}}
        client, http = client_with(tmp_path, monkeypatch, [TOKEN_OK, (200, folder)])
        assert client.folder(ROOT)["name"] == "①月次税務資料提出フォルダ（ライズG）"
        call = http.calls[1]
        assert (call["method"], call["url"]) == ("GET", f"{API}/2.0/folders/{ROOT}")
        assert "permissions" in call["params"]["fields"]
        assert call["headers"]["Authorization"] == "Bearer box-token-xyz"

    def test_all_pages_of_a_folder_are_listed(self, tmp_path, monkeypatch):
        first = {"total_count": 3, "entries": [{"id": "1", "type": "folder", "name": "a"}, {"id": "2", "type": "file", "name": "b.pdf"}]}
        second = {"total_count": 3, "entries": [{"id": "3", "type": "file", "name": "c.xlsx"}]}
        client, http = client_with(tmp_path, monkeypatch, [TOKEN_OK, (200, first), (200, second)])
        assert [i["id"] for i in client.items(ROOT)] == ["1", "2", "3"]
        assert [c["params"]["offset"] for c in http.calls[1:]] == ["0", "2"]

    def test_an_empty_folder_ends_the_listing(self, tmp_path, monkeypatch):
        client, http = client_with(tmp_path, monkeypatch, [TOKEN_OK, (200, {"total_count": 0, "entries": []})])
        assert client.items(ROOT) == [] and len(http.calls) == 2

    def test_a_path_is_followed_by_names(self, tmp_path, monkeypatch):
        month = {"total_count": 2, "entries": [{"id": "10", "type": "folder", "name": "2026年09月報告"}, {"id": "11", "type": "folder", "name": "2026年10月報告"}]}
        # Mac から上げた名前は「ジ」が「シ＋濁点」に分かれていることがある
        company = {"total_count": 1, "entries": [{"id": "20", "type": "folder", "name": "ライズクリエイション"}]}
        client, http = client_with(tmp_path, monkeypatch, [TOKEN_OK, (200, month), (200, company)])
        assert client.find(ROOT, "2026年10月報告/ライズクリエイション")["id"] == "20"
        assert [c["url"] for c in http.calls[1:]] == [f"{API}/2.0/folders/{ROOT}/items", f"{API}/2.0/folders/11/items"]

    def test_a_missing_name_or_a_file_in_the_middle_of_the_path_is_not_found(self, tmp_path, monkeypatch):
        listing = {"total_count": 1, "entries": [{"id": "30", "type": "file", "name": "請求書.pdf"}]}
        client, _ = client_with(tmp_path, monkeypatch, [TOKEN_OK, (200, listing), (200, listing)])
        assert client.find(ROOT, "無いフォルダ") is None
        assert client.find(ROOT, "請求書.pdf/下") is None

    def test_a_file_is_downloaded_as_bytes(self, tmp_path, monkeypatch):
        client, http = client_with(tmp_path, monkeypatch, [TOKEN_OK, (200, b"%PDF-1.7 ...")])
        assert client.download("30", size=12) == b"%PDF-1.7 ..."
        assert (http.calls[1]["method"], http.calls[1]["url"]) == ("GET", f"{API}/2.0/files/30/content")

    def test_a_file_over_the_limit_is_not_fetched(self, tmp_path, monkeypatch):
        client, http = client_with(tmp_path, monkeypatch, [TOKEN_OK, (200, b"x" * 2_000_000)])
        client.settings["max_file_mb"] = 1
        with pytest.raises(BoxError, match="大きすぎます"):
            client.download("30", size=5_000_000)
        assert http.calls == []  # 大きさが分かっていれば取りに行かない
        with pytest.raises(BoxError, match="大きすぎます"):
            client.download("30")  # 大きさを知らずに取った分も返さない

    def test_a_refused_download_tells_the_status(self, tmp_path, monkeypatch):
        refused = (403, {"type": "error", "status": 403, "code": "access_denied_insufficient_permissions", "message": "Access denied - insufficient permission"})
        client, _ = client_with(tmp_path, monkeypatch, [TOKEN_OK, refused])
        with pytest.raises(BoxError) as exc:
            client.download("30")
        assert (exc.value.status, exc.value.code) == (403, "access_denied_insufficient_permissions")
        assert "box-token-xyz" not in str(exc.value)

    def test_a_file_that_is_not_ready_is_an_error_not_empty_bytes(self, tmp_path, monkeypatch):
        client, _ = client_with(tmp_path, monkeypatch, [TOKEN_OK, (202, b"")])
        with pytest.raises(BoxError, match="HTTP 202"):
            client.download("30")


class TestReadOnly:
    def test_nothing_but_the_sign_in_is_sent_with_a_writing_method(self, tmp_path, monkeypatch):
        listing = {"total_count": 1, "entries": [{"id": "30", "type": "file", "name": "請求書.pdf", "size": 3}]}
        responses = [TOKEN_OK, (200, {"id": "1", "login": "AutomationUser_1@boxdevedition.com"}), (200, {"id": ROOT}),
                     (200, listing), (200, listing), (200, b"pdf")]
        client, http = client_with(tmp_path, monkeypatch, responses)
        client.me(), client.folder(ROOT), client.items(ROOT), client.find(ROOT, "請求書.pdf"), client.download("30")
        assert [c["method"] for c in http.calls] == ["POST"] + ["GET"] * 5
        assert http.calls[0]["url"].endswith("/oauth2/token")
        assert all(c["data"] is None for c in http.calls[1:])

    def test_the_client_has_no_way_to_write(self):
        public = {name for name in vars(BoxClient) if not name.startswith("_")}
        assert public == {"settings", "api_url", "root_folder_id", "max_file_bytes", "missing_secrets", "token",
                          "me", "folder", "items", "find", "download"}

    @pytest.mark.parametrize("permissions, expected", [
        ({"can_download": True, "can_upload": False, "can_rename": False, "can_delete": False,
          "can_invite_collaborator": False, "can_share": True}, []),  # ビューアー
        ({"can_download": True, "can_upload": True, "can_rename": True, "can_delete": True,
          "can_invite_collaborator": False}, ["アップロード", "名前の変更", "削除"]),  # 編集者
        ({"can_download": True, "can_upload": True}, ["アップロード"]),
    ])
    def test_a_role_stronger_than_viewer_is_noticed(self, permissions, expected):
        assert write_permissions({"permissions": permissions}) == expected

    def test_missing_permissions_are_unknown_not_safe(self):
        assert write_permissions({"id": ROOT}) is None and write_permissions({"permissions": None}) is None
