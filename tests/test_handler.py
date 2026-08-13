import base64
import hashlib
import hmac
import json

from raizuinu.answer import Answer
from raizuinu.config import Config
from raizuinu.handbook import HandbookLoader
from raizuinu.handler import RaizuinuHandler

TOKEN = base64.b64encode(b"handler-test-key").decode()


def sign(body: bytes) -> str:
    digest = hmac.new(base64.b64decode(TOKEN), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def payload(room_id=12345, message_id="200001") -> bytes:
    return json.dumps(
        {
            "webhook_event_type": "mention_to_me",
            "webhook_event": {
                "from_account_id": 111,
                "to_account_id": 999,
                "room_id": room_id,
                "message_id": message_id,
                "body": "[To:999] 銀行明細の取得手順は？",
                "send_time": 1700000000,
            },
        }
    ).encode()


class FakeChatwork:
    def __init__(self):
        self.sent = []

    def send_message(self, room_id, body):
        self.sent.append((room_id, body))
        return "1"

    def get_recent_messages(self, room_id, limit=20):
        return [
            {"message_id": "1", "account": {"name": "坂田"}, "body": "前の会話"},
            {"message_id": "200001", "account": {"name": "質問者"}, "body": "質問本文"},
        ]


class FakeGenerator:
    def __init__(self, answer):
        self._answer = answer
        self.calls = []

    def generate(self, question, handbook, context=""):
        self.calls.append({"question": question, "context": context})
        return self._answer


class FakeAudit:
    def __init__(self):
        self.records = []

    def log(self, record):
        self.records.append(record)


def make_handler(tmp_path, monkeypatch, allowed_rooms=(12345,), answer=None):
    monkeypatch.setenv("CHATWORK_WEBHOOK_TOKEN", TOKEN)
    (tmp_path / "銀行明細取得.md").write_text("# 銀行明細取得\n手順\n", encoding="utf-8")
    config = Config.load(tmp_path / "no-config.json")
    config.data["allowed_room_ids"] = list(allowed_rooms)
    config.data["webhook_async"] = False  # テストは同期で
    config.data["state_dir"] = str(tmp_path / "state")
    config.data["audit_log_dir"] = str(tmp_path / "logs")
    config.base_dir = tmp_path

    answer = answer or Answer(
        has_answer=True,
        text="回答",
        sources=[{"file": "銀行明細取得.md", "heading": ""}],
        usage={"input_tokens": 10, "output_tokens": 10},
    )
    chatwork = FakeChatwork()
    generator = FakeGenerator(answer)
    audit = FakeAudit()
    handler = RaizuinuHandler(
        config,
        chatwork=chatwork,
        generator=generator,
        audit=audit,
        handbook_loader=HandbookLoader([tmp_path], ["*.md"], [], 300),
    )
    return handler, chatwork, generator, audit


class TestHandleWebhook:
    def test_end_to_end_reply_and_audit(self, tmp_path, monkeypatch):
        handler, chatwork, generator, audit = make_handler(tmp_path, monkeypatch)
        body = payload()
        result = handler.handle_webhook(body, sign(body))
        assert result.status == 200
        assert len(chatwork.sent) == 1
        room_id, reply = chatwork.sent[0]
        assert room_id == 12345
        assert reply.startswith("[rp aid=111 to=12345-200001]")
        assert "【出典】" in reply
        # 会話コンテキストから質問自身は除外される
        assert "前の会話" in generator.calls[0]["context"]
        assert "質問本文" not in generator.calls[0]["context"]
        assert audit.records[0]["room_id"] == 12345

    def test_invalid_signature_401(self, tmp_path, monkeypatch):
        handler, chatwork, _, _ = make_handler(tmp_path, monkeypatch)
        result = handler.handle_webhook(payload(), "invalid")
        assert result.status == 401
        assert chatwork.sent == []

    def test_room_not_allowed_no_response(self, tmp_path, monkeypatch):
        handler, chatwork, _, _ = make_handler(
            tmp_path, monkeypatch, allowed_rooms=(99999,)
        )
        body = payload()
        result = handler.handle_webhook(body, sign(body))
        assert result.status == 200
        assert chatwork.sent == []  # FR-05: リスト外ルームには応答しない

    def test_duplicate_message_processed_once(self, tmp_path, monkeypatch):
        handler, chatwork, _, _ = make_handler(tmp_path, monkeypatch)
        body = payload()
        handler.handle_webhook(body, sign(body))
        handler.handle_webhook(body, sign(body))  # Webhook再送を想定
        assert len(chatwork.sent) == 1

    def test_generator_error_sends_failure_message(self, tmp_path, monkeypatch):
        handler, chatwork, generator, _ = make_handler(tmp_path, monkeypatch)

        def boom(question, handbook, context=""):
            raise RuntimeError("API down")

        generator.generate = boom
        body = payload()
        result = handler.handle_webhook(body, sign(body))
        assert result.status == 200
        assert len(chatwork.sent) == 1  # FR-06: 無言で終わらせない
        assert "失敗" in chatwork.sent[0][1]

    def test_cost_limit_stops_answering(self, tmp_path, monkeypatch):
        handler, chatwork, generator, _ = make_handler(tmp_path, monkeypatch)
        # 上限超過状態を作る
        handler._cost.add_usage({"output_tokens": 10_000_000_000})
        body = payload()
        handler.handle_webhook(body, sign(body))
        assert generator.calls == []  # 生成は呼ばれない
        assert any("上限" in sent[1] for sent in chatwork.sent)

    def test_post_send_audit_failure_does_not_double_send(self, tmp_path, monkeypatch):
        handler, chatwork, _, audit = make_handler(tmp_path, monkeypatch)

        def boom(record):
            raise OSError("disk full")

        audit.log = boom
        body = payload()
        handler.handle_webhook(body, sign(body))
        # 回答は1通だけ届き、「失敗しました」の追送はない
        assert len(chatwork.sent) == 1
        assert "失敗" not in chatwork.sent[0][1]

    def test_cost_counted_even_if_send_fails(self, tmp_path, monkeypatch):
        handler, chatwork, _, _ = make_handler(tmp_path, monkeypatch)

        original_send = chatwork.send_message
        calls = {"n": 0}

        def failing_send(room_id, body):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("Chatwork down")  # 回答送信は失敗
            return original_send(room_id, body)      # 失敗通知は成功

        chatwork.send_message = failing_send
        body = payload()
        handler.handle_webhook(body, sign(body))
        assert handler._cost.status().total_jpy > 0  # 送信失敗でもコストは計上済み

    def test_empty_handbook_fails_loudly(self, tmp_path, monkeypatch):
        from raizuinu.handbook import HandbookLoader

        handler, chatwork, generator, _ = make_handler(tmp_path, monkeypatch)
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        handler._handbook_loader = HandbookLoader([empty_dir], ["*.md"], [], 300)
        body = payload()
        handler.handle_webhook(body, sign(body))
        assert generator.calls == []  # 空のまま回答しない
        assert any("失敗" in sent[1] for sent in chatwork.sent)

    def test_update_report_is_queued_and_acknowledged(self, tmp_path, monkeypatch):
        import json as _json

        report_answer = Answer(
            has_answer=False,
            text="「為替レート取得・SCM入力」の更新報告を受け付けました。",
            usage={"input_tokens": 10, "output_tokens": 10},
            intent="manual_update_report",
            reported_manuals=["為替レート取得・SCM入力"],
        )
        handler, chatwork, _, audit = make_handler(
            tmp_path, monkeypatch, answer=report_answer
        )
        handler._config.data["admin_room_id"] = 55555  # 報告ルームと別の管理者ルーム
        body = payload()
        handler.handle_webhook(body, sign(body))

        # 報告者への受領返信＋管理者通知の2通
        assert len(chatwork.sent) == 2
        assert "受け付けました" in chatwork.sent[0][1]
        assert "反映" in chatwork.sent[0][1]
        assert chatwork.sent[1][0] == 55555
        assert "為替レート取得・SCM入力" in chatwork.sent[1][1]

        # 受付リストに記録される
        queue = tmp_path / "state" / "pending_updates.jsonl"
        assert queue.exists()
        entry = _json.loads(queue.read_text(encoding="utf-8").strip())
        assert entry["manuals"] == ["為替レート取得・SCM入力"]
        assert entry["status"] == "pending"

        # 監査ログにも記録
        assert audit.records[0]["type"] == "update_report"

    def test_feedback_is_queued_and_acknowledged(self, tmp_path, monkeypatch):
        import json as _json

        feedback_answer = Answer(
            has_answer=False,
            text="ご指摘ありがとうございます。改善リストに追加し、管理者が確認します。",
            usage={"input_tokens": 10, "output_tokens": 10},
            intent="answer_feedback",
        )
        handler, chatwork, _, audit = make_handler(
            tmp_path, monkeypatch, answer=feedback_answer
        )
        handler._config.data["admin_room_id"] = 55555
        body = payload(message_id="500001")
        handler.handle_webhook(body, sign(body))

        assert len(chatwork.sent) == 2  # 受領返信＋管理者通知
        assert "ご指摘ありがとうございます" in chatwork.sent[0][1]
        assert chatwork.sent[1][0] == 55555
        queue = tmp_path / "state" / "feedback.jsonl"
        assert queue.exists()
        entry = _json.loads(queue.read_text(encoding="utf-8").strip())
        assert entry["status"] == "pending"
        assert audit.records[0]["type"] == "feedback"

    def test_update_report_same_room_admin_no_duplicate_notify(self, tmp_path, monkeypatch):
        report_answer = Answer(
            has_answer=False,
            text="受け付けました。",
            usage={},
            intent="manual_update_report",
            reported_manuals=["銀行明細取得"],
        )
        handler, chatwork, _, _ = make_handler(tmp_path, monkeypatch, answer=report_answer)
        handler._config.data["admin_room_id"] = 12345  # 報告ルーム＝管理者ルーム
        body = payload()
        handler.handle_webhook(body, sign(body))
        assert len(chatwork.sent) == 1  # 受領返信のみ（同室への二重通知はしない）

    def test_queue_overflow_sends_busy_message(self, tmp_path, monkeypatch):
        import threading

        handler, chatwork, generator, _ = make_handler(tmp_path, monkeypatch)
        handler._config.data["webhook_async"] = True
        handler._queue_slots = threading.Semaphore(0)  # 滞留上限に達した状態を再現
        body = payload(message_id="400001")
        result = handler.handle_webhook(body, sign(body))
        assert result.status == 200
        assert generator.calls == []  # 処理はしない
        assert any("混み合って" in sent[1] for sent in chatwork.sent)  # 無言で捨てない

    def test_stopped_notification_flag_only_on_success(self, tmp_path, monkeypatch):
        handler, chatwork, _, _ = make_handler(tmp_path, monkeypatch)
        handler._cost.add_usage({"output_tokens": 10_000_000_000})

        # admin_room_id未設定 → フラグは立たない（設定後に通知できる）
        handler.handle_webhook(payload(message_id="300001"), sign(payload(message_id="300001")))
        assert not handler._cost.status().stopped_notified

        # admin_room_id設定後は送信されフラグが立つ
        handler._config.data["admin_room_id"] = 55555
        body = payload(message_id="300002")
        handler.handle_webhook(body, sign(body))
        assert handler._cost.status().stopped_notified
        assert any(room == 55555 for room, _ in chatwork.sent)
