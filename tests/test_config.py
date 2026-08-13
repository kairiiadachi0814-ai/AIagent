from raizuinu.config import Config


class TestConfig:
    def test_defaults_without_file(self, tmp_path):
        config = Config.load(tmp_path / "missing.json")
        assert config.model == "claude-opus-5"
        assert config.allowed_room_ids == []
        assert config.max_tokens >= 16000

    def test_file_overrides_defaults(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text('{"agent_name": "テスト犬", "max_tokens": 20000}', encoding="utf-8")
        config = Config.load(path)
        assert config.agent_name == "テスト犬"
        assert config.max_tokens == 20000
        assert config.model == "claude-opus-5"  # 未指定は既定値

    def test_env_overrides_dirs(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RAIZUINU_STATE_DIR", "/mnt/state")
        monkeypatch.setenv("RAIZUINU_AUDIT_LOG_DIR", "/mnt/logs")
        config = Config.load(tmp_path / "missing.json")
        assert config.state_dir == "/mnt/state"
        assert config.audit_log_dir == "/mnt/logs"

    def test_secrets_come_from_env_only(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CHATWORK_API_TOKEN", "token-x")
        config = Config.load(tmp_path / "missing.json")
        assert config.chatwork_api_token == "token-x"
        assert "CHATWORK_API_TOKEN" not in config.data  # 設定ファイル側に混入しない
