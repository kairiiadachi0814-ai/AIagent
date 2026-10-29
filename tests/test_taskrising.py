import json
from types import SimpleNamespace

import pytest

from raizuinu.config import Config
from raizuinu.taskrising import (
    TaskRisingClient,
    TaskRisingError,
    required_columns,
    table_columns,
)

URL = "https://example.supabase.co"
KEY = "sb_publishable_abcdefghijklmnop"
PASSWORD = "bot-password-1234"


def make_config(tmp_path, monkeypatch, secrets=True):
    config = Config.load(tmp_path / "no-config.json")
    config.data["taskrising"] = {
        "enabled": False, "url": URL, "tasks_table": "tasks", "timeout_seconds": 30,
    }
    config.base_dir = tmp_path
    if secrets:
        monkeypatch.setenv("TASKRISING_API_KEY", KEY)
        monkeypatch.setenv("TASKRISING_BOT_EMAIL", "bot@example.com")
        monkeypatch.setenv("TASKRISING_BOT_PASSWORD", PASSWORD)
    else:
        for name in ("TASKRISING_API_KEY", "TASKRISING_BOT_EMAIL", "TASKRISING_BOT_PASSWORD"):
            monkeypatch.delenv(name, raising=False)
    return config


class FakeHttp:
    """requests.request の差し替え。呼ばれた内容を記録する。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def __call__(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append(
            {"method": method, "url": url, "params": params, "json": json,
             "headers": headers, "timeout": timeout}
        )
        status, body = self._responses.pop(0)
        text = body if isinstance(body, str) else __import__("json").dumps(body)
        return SimpleNamespace(
            status_code=status, text=text,
            json=lambda: __import__("json").loads(text),
        )


TOKEN_OK = (200, {"access_token": "jwt-token-xyz", "expires_in": 3600})


class TestSignIn:
    def test_missing_secrets_are_named_without_values(self, tmp_path, monkeypatch):
        config = make_config(tmp_path, monkeypatch, secrets=False)
        client = TaskRisingClient(config, http=FakeHttp([]))
        assert client.missing_secrets() == ["TASKRISING_API_KEY"]
        with pytest.raises(TaskRisingError) as exc:
            client.sign_in()
        assert "TASKRISING_API_KEY" in str(exc.value)

    def test_the_key_alone_is_not_enough_to_sign_in(self, tmp_path, monkeypatch):
        # キーだけあっても、ボットとして入る手段（パスワードか更新トークン）が要る
        config = make_config(tmp_path, monkeypatch, secrets=False)
        monkeypatch.setenv("TASKRISING_API_KEY", KEY)
        client = TaskRisingClient(config, http=FakeHttp([]))
        assert "TASKRISING_BOT_EMAIL" in client.missing_secrets()[0]

    def test_bot_user_signs_in_with_password_not_google(self, tmp_path, monkeypatch):
        # Googleログインは人の操作が要るため、サーバーからはメール＋パスワードで入る
        http = FakeHttp([TOKEN_OK])
        client = TaskRisingClient(make_config(tmp_path, monkeypatch), http=http)
        assert client.sign_in() == "jwt-token-xyz"
        call = http.calls[0]
        assert call["url"] == f"{URL}/auth/v1/token?grant_type=password"
        assert call["json"]["email"] == "bot@example.com"
        assert call["headers"]["apikey"] == KEY
        assert "Authorization" not in call["headers"]  # ログイン時はトークンを付けない

    def test_the_token_is_reused_until_it_nears_expiry(self, tmp_path, monkeypatch):
        clock = {"t": 1000.0}
        http = FakeHttp([TOKEN_OK, (200, []), (200, []), TOKEN_OK, (200, [])])
        client = TaskRisingClient(
            make_config(tmp_path, monkeypatch), http=http, now=lambda: clock["t"]
        )
        client.select("tasks")
        client.select("tasks")
        assert sum(1 for c in http.calls if "token" in c["url"]) == 1  # 毎回ログインしない
        clock["t"] += 3600  # 期限切れ
        client.select("tasks")
        assert sum(1 for c in http.calls if "token" in c["url"]) == 2

    def test_a_failed_login_says_so(self, tmp_path, monkeypatch):
        http = FakeHttp([(400, {"error": "invalid_grant"})])
        client = TaskRisingClient(make_config(tmp_path, monkeypatch), http=http)
        with pytest.raises(TaskRisingError) as exc:
            client.sign_in()
        assert "HTTP 400" in str(exc.value)


class TestGoogleAccountPath:
    """ボットのGoogleアカウントで一度だけ手動ログインし、更新トークンで続ける方式。

    人のログインはGoogleのまま変わらない。ボットはブラウザを開けないため、
    Googleで発行された更新トークンを預かって使い回す。
    """

    def config(self, tmp_path, monkeypatch, refresh="refresh-abc"):
        config = make_config(tmp_path, monkeypatch, secrets=False)
        monkeypatch.setenv("TASKRISING_API_KEY", KEY)
        monkeypatch.setenv("TASKRISING_BOT_REFRESH_TOKEN", refresh)
        config.data["state_dir"] = str(tmp_path / "state")
        return config

    def test_a_refresh_token_is_enough(self, tmp_path, monkeypatch):
        config = self.config(tmp_path, monkeypatch)
        http = FakeHttp([(200, {"access_token": "jwt-1", "expires_in": 3600})])
        client = TaskRisingClient(config, http=http)
        assert client.missing_secrets() == []
        assert client.sign_in() == "jwt-1"
        call = http.calls[0]
        assert call["url"].endswith("grant_type=refresh_token")
        assert call["json"] == {"refresh_token": "refresh-abc"}

    def test_the_rotated_token_is_kept_for_next_time(self, tmp_path, monkeypatch):
        # 更新トークンは使うたびに入れ替わる。保存しないと次回入れなくなる
        config = self.config(tmp_path, monkeypatch)
        clock = {"t": 1000.0}
        http = FakeHttp([
            (200, {"access_token": "jwt-1", "expires_in": 3600, "refresh_token": "refresh-2"}),
            (200, {"access_token": "jwt-2", "expires_in": 3600, "refresh_token": "refresh-3"}),
        ])
        client = TaskRisingClient(config, http=http, now=lambda: clock["t"])
        client.sign_in()
        clock["t"] += 3600  # 期限切れ
        client.sign_in()
        assert http.calls[1]["json"] == {"refresh_token": "refresh-2"}
        saved = json.loads(
            (tmp_path / "state" / "taskrising_session.json").read_text(encoding="utf-8")
        )
        assert saved["refresh_token"] == "refresh-3"

    def test_an_expired_refresh_token_falls_back_to_the_password(self, tmp_path, monkeypatch):
        config = self.config(tmp_path, monkeypatch)
        monkeypatch.setenv("TASKRISING_BOT_EMAIL", "bot@example.com")
        monkeypatch.setenv("TASKRISING_BOT_PASSWORD", PASSWORD)
        http = FakeHttp([
            (400, {"error": "invalid_grant"}),  # 更新トークンが失効
            (200, {"access_token": "jwt-9", "expires_in": 3600}),
        ])
        client = TaskRisingClient(config, http=http)
        assert client.sign_in() == "jwt-9"
        assert http.calls[-1]["url"].endswith("grant_type=password")

    def test_an_expired_refresh_token_without_a_password_is_reported(self, tmp_path, monkeypatch):
        config = self.config(tmp_path, monkeypatch)
        http = FakeHttp([(400, {"error": "invalid_grant"})])
        client = TaskRisingClient(config, http=http)
        with pytest.raises(TaskRisingError):
            client.sign_in()

    def test_the_saved_token_is_not_world_readable(self, tmp_path, monkeypatch):
        import os
        import stat

        config = self.config(tmp_path, monkeypatch)
        http = FakeHttp([(200, {"access_token": "j", "expires_in": 3600, "refresh_token": "r2"})])
        TaskRisingClient(config, http=http).sign_in()
        path = tmp_path / "state" / "taskrising_session.json"
        if os.name != "nt":  # Windowsはパーミッションの概念が異なる
            assert not stat.S_IMODE(path.stat().st_mode) & 0o077


class TestSecretsAreNotLeaked:
    def test_the_api_key_never_appears_in_an_error(self, tmp_path, monkeypatch):
        # 応答本文にキーが echo されても、そのまま例外へ流さない
        http = FakeHttp([TOKEN_OK, (401, f"bad key: {KEY} for {PASSWORD}")])
        client = TaskRisingClient(make_config(tmp_path, monkeypatch), http=http)
        with pytest.raises(TaskRisingError) as exc:
            client.select("tasks")
        message = str(exc.value)
        assert KEY not in message
        assert PASSWORD not in message
        assert "***" in message

    def test_a_network_failure_does_not_dump_the_request(self, tmp_path, monkeypatch):
        def boom(*args, **kwargs):
            raise ConnectionError(f"failed to connect with {KEY}")

        client = TaskRisingClient(make_config(tmp_path, monkeypatch), http=boom)
        with pytest.raises(TaskRisingError) as exc:
            client.select("tasks")
        assert KEY not in str(exc.value)


class TestRequests:
    def test_select_sends_the_key_and_the_bot_token(self, tmp_path, monkeypatch):
        http = FakeHttp([TOKEN_OK, (200, [{"id": 1}])])
        client = TaskRisingClient(make_config(tmp_path, monkeypatch), http=http)
        rows = client.select("tasks", {"limit": "1"})
        assert rows == [{"id": 1}]
        call = http.calls[-1]
        assert call["url"] == f"{URL}/rest/v1/tasks"
        assert call["params"] == {"limit": "1", "select": "*"}
        assert call["headers"]["apikey"] == KEY
        assert call["headers"]["Authorization"] == "Bearer jwt-token-xyz"

    def test_insert_returns_the_created_row(self, tmp_path, monkeypatch):
        http = FakeHttp([TOKEN_OK, (201, [{"id": 42, "title": "経費支払い"}])])
        client = TaskRisingClient(make_config(tmp_path, monkeypatch), http=http)
        row = client.insert("tasks", {"title": "経費支払い"})
        assert row == {"id": 42, "title": "経費支払い"}
        assert http.calls[-1]["headers"]["Prefer"] == "return=representation"

    def test_an_empty_body_is_not_an_error(self, tmp_path, monkeypatch):
        http = FakeHttp([TOKEN_OK, (204, "")])
        client = TaskRisingClient(make_config(tmp_path, monkeypatch), http=http)
        assert client.insert("tasks", {"title": "x"}) == {}


SCHEMA = {
    "definitions": {
        "tasks": {
            "required": ["title", "created_by"],
            "properties": {
                "id": {"format": "uuid", "type": "string"},
                "title": {"format": "text", "type": "string"},
                "due_date": {"format": "date", "type": "string"},
                "created_by": {"format": "uuid", "type": "string"},
            },
        }
    }
}


class TestSchema:
    def test_columns_and_required_are_read_from_the_definition(self):
        assert set(table_columns(SCHEMA, "tasks")) == {"id", "title", "due_date", "created_by"}
        assert required_columns(SCHEMA, "tasks") == ["title", "created_by"]

    def test_an_unknown_table_is_empty_not_an_error(self):
        assert table_columns(SCHEMA, "invoices") == {}
        assert required_columns(SCHEMA, "invoices") == []


class TestNotWiredInYet:
    def test_the_feature_is_off_by_default(self, tmp_path):
        # 全機能が揃うまで稼働させない
        assert Config.load(tmp_path / "no-config.json").taskrising["enabled"] is False

    def test_the_shipped_config_is_also_off(self):
        import pathlib

        shipped = json.loads(
            (pathlib.Path("config") / "config.json").read_text(encoding="utf-8")
        )
        assert shipped.get("taskrising", {}).get("enabled", False) is False

    def test_no_admin_key_can_be_supplied(self, tmp_path, monkeypatch):
        # 管理者権限のキー（service_role）を読む口を作らない。
        # 入れられてしまうと行レベルの権限制御を素通りする
        config = make_config(tmp_path, monkeypatch)
        monkeypatch.setenv("TASKRISING_SERVICE_ROLE_KEY", "should-be-ignored")
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "should-be-ignored")
        assert not [a for a in dir(config) if "service" in a.lower()]

        http = FakeHttp([TOKEN_OK, (200, [])])
        TaskRisingClient(config, http=http).select("tasks")
        for call in http.calls:
            assert call["headers"]["apikey"] == KEY  # 送るのは公開キーだけ
            assert "should-be-ignored" not in json.dumps(call["headers"])
