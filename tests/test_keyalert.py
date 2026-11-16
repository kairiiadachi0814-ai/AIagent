"""AnthropicのAPIキーが使えなくなったら、管理者ルームへ一度だけ知らせる。

実例（2026-11-16）: 有効期限つきのキーが切れ、FAXの読み取りも質問への回答も止まったが、
止まっていることをどこにも知らせていなかった。
"""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import anthropic
from anthropic import _base_client

from raizuinu.config import Config
from raizuinu.keyalert import INVALID, JST, OK, SKIPPED, UNKNOWN, KeyAlert

httpx = _base_client.httpx  # SDKが使っているHTTPライブラリ（例外の組み立てに要る）

ADMIN_ROOM, ADACHI = 444945031, 6945415
SECRET = "sk-ant-test-0000-not-a-real-key"
START = datetime(2026, 11, 14, 9, 0, tzinfo=JST)


def api_error(status):
    response = httpx.Response(status, request=httpx.Request("GET", "https://api.anthropic.com/v1/models"))
    kind = {401: anthropic.AuthenticationError, 403: anthropic.PermissionDeniedError,
            429: anthropic.RateLimitError, 500: anthropic.InternalServerError}[status]
    return kind(f"error {status}", response=response, body=None)


class FakeClient:
    """models.list の差し替え。outcomes を順に返す（例外なら投げる）。最後を繰り返す。"""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes) or [None]
        self.calls = 0
        self.models = SimpleNamespace(list=self._list)

    def _list(self, limit=None):
        outcome = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(data=[])


class FakeChatwork:
    def __init__(self, fail=0):
        self.sent, self.fail = [], fail

    def send_message(self, room_id, body):
        if self.fail:
            self.fail -= 1
            raise RuntimeError("chatwork down")
        self.sent.append((room_id, body))
        return str(100 + len(self.sent))


def make(tmp_path, monkeypatch, *outcomes, chatwork=None, key=SECRET, **settings):
    config = Config.load(tmp_path / "no-config.json")
    config.base_dir = tmp_path
    config.data["state_dir"] = str(tmp_path / "state")
    config.data["admin_room_id"] = ADMIN_ROOM
    config.data["admin_account_ids"] = [ADACHI]
    config.data["key_alert"] = {"enabled": True, "check_minutes": 15, **settings}
    if key is None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    else:
        monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    clock = SimpleNamespace(now=START)
    cw = chatwork or FakeChatwork()
    client = FakeClient(*outcomes)
    alert = KeyAlert(config, cw, client=client, now=lambda: clock.now)
    return alert, cw, client, clock


def state_of(tmp_path):
    return json.loads((tmp_path / "state" / "key_alert.json").read_text(encoding="utf-8"))


class TestKeyAlert:
    def test_a_working_key_says_nothing(self, tmp_path, monkeypatch):
        alert, cw, client, _ = make(tmp_path, monkeypatch, None)
        assert alert.run_once() == OK
        assert cw.sent == [] and client.calls == 1

    def test_a_rejected_key_is_reported_to_the_admin_room_once(self, tmp_path, monkeypatch):
        alert, cw, client, clock = make(tmp_path, monkeypatch, api_error(401))
        assert alert.run_once() == INVALID
        room, body = cw.sent[0]
        assert room == ADMIN_ROOM
        assert body.startswith(f"[To:{ADACHI}]\n【要対応】AnthropicのAPIキーが使えなくなっています（認証エラー 401。")
        assert "有効期限切れ" in body and "ANTHROPIC_API_KEY" in body and "1回だけ" in body
        assert SECRET not in body and SECRET not in json.dumps(state_of(tmp_path))
        # 止まっている間は毎回確かめるが、知らせは重ねない
        for minutes in (5, 10, 600):
            clock.now = START + timedelta(minutes=minutes)
            assert alert.run_once() == INVALID
        assert len(cw.sent) == 1 and client.calls == 4

    def test_a_key_without_permission_is_reported_too(self, tmp_path, monkeypatch):
        alert, cw, _, _ = make(tmp_path, monkeypatch, api_error(403))
        assert alert.run_once() == INVALID
        assert "権限エラー 403" in cw.sent[0][1]

    def test_a_missing_key_is_reported_without_calling_the_api(self, tmp_path, monkeypatch):
        alert, cw, client, _ = make(tmp_path, monkeypatch, None, key=None)
        assert alert.run_once() == INVALID
        assert "キーが設定されていません" in cw.sent[0][1] and client.calls == 0

    def test_congestion_and_outages_are_not_a_key_problem(self, tmp_path, monkeypatch):
        connection = anthropic.APIConnectionError(request=httpx.Request("GET", "https://api.anthropic.com/v1/models"))
        alert, cw, client, clock = make(tmp_path, monkeypatch, api_error(429), api_error(500), connection, None)
        for step in range(3):
            clock.now = START + timedelta(minutes=5 * step)
            assert alert.run_once() == UNKNOWN  # 確かめられなかった回は数えず、次の巡回でまた見る
        assert cw.sent == [] and client.calls == 3
        clock.now = START + timedelta(minutes=15)
        assert alert.run_once() == OK

    def test_recovery_is_reported_once_and_a_later_failure_is_reported_again(self, tmp_path, monkeypatch):
        alert, cw, _, clock = make(tmp_path, monkeypatch, api_error(401), api_error(401), None, None, api_error(401))
        alert.run_once()
        clock.now = START + timedelta(minutes=5)
        alert.run_once()
        clock.now = datetime(2026, 11, 16, 10, 25, tzinfo=JST)
        assert alert.run_once() == OK
        assert len(cw.sent) == 2
        assert cw.sent[1][1].startswith(
            f"[To:{ADACHI}]\nAnthropicのAPIキーが通るようになりました（11/14 09:00 から止まっていました）。"
        )
        assert "自動では読み直しません" in cw.sent[1][1]
        assert "alerted_at" not in state_of(tmp_path) and "invalid_since" not in state_of(tmp_path)
        clock.now += timedelta(minutes=15)
        assert alert.run_once() == OK and len(cw.sent) == 2  # 戻った知らせも1回だけ
        clock.now += timedelta(minutes=15)
        assert alert.run_once() == INVALID and len(cw.sent) == 3  # 次に止まったら、また1回知らせる

    def test_a_working_key_is_checked_only_every_check_minutes(self, tmp_path, monkeypatch):
        alert, _, client, clock = make(tmp_path, monkeypatch, None)
        assert alert.run_once() == OK
        for minutes in (5, 10):
            clock.now = START + timedelta(minutes=minutes)
            assert alert.run_once() == SKIPPED
        clock.now = START + timedelta(minutes=15)
        assert alert.run_once() == OK
        assert client.calls == 2

    def test_a_notice_that_could_not_be_posted_is_tried_again(self, tmp_path, monkeypatch):
        alert, cw, _, clock = make(tmp_path, monkeypatch, api_error(401), api_error(401), None, None,
                                   chatwork=FakeChatwork(fail=1))
        assert alert.run_once() == INVALID and cw.sent == []
        clock.now = START + timedelta(minutes=5)
        assert alert.run_once() == INVALID and len(cw.sent) == 1
        cw.fail = 1  # 戻った知らせの投稿に失敗しても、次の巡回で出す
        clock.now = START + timedelta(minutes=10)
        assert alert.run_once() == OK and len(cw.sent) == 1
        clock.now = START + timedelta(minutes=15)
        assert alert.run_once() == OK and len(cw.sent) == 2

    def test_it_can_be_switched_off(self, tmp_path, monkeypatch):
        alert, cw, client, _ = make(tmp_path, monkeypatch, api_error(401), enabled=False)
        assert alert.run_once() == SKIPPED
        assert cw.sent == [] and client.calls == 0

    def test_without_an_admin_room_nothing_is_marked_as_told(self, tmp_path, monkeypatch):
        alert, cw, _, _ = make(tmp_path, monkeypatch, api_error(401))
        alert._config.data["admin_room_id"] = None
        assert alert.run_once() == INVALID
        assert cw.sent == [] and "alerted_at" not in state_of(tmp_path)
