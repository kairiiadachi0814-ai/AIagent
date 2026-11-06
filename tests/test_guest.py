"""経理財務部メンバー以外への応対（社内ナレッジを返さない）。"""

import json

from raizuinu.answer import Answer
from raizuinu.config import Config
from raizuinu.guest import FALLBACK, SYSTEM_PROMPT, GuestResponder
from raizuinu.handbook import HandbookLoader
from raizuinu.handler import RaizuinuHandler
from tests.test_handler import TOKEN, FakeAudit, FakeGenerator, sign

MEMBER = 8681926      # 坂田さん（経理財務部）
GUEST = 3115861       # 山田さん（部外）
ROOM = 384793683


class FakeChatwork:
    def __init__(self):
        self.sent = []

    def send_message(self, room_id, body):
        self.sent.append((room_id, body))
        return "1"

    def get_recent_messages(self, room_id, limit=20):
        return []


class FakeGuest:
    def __init__(self):
        self.calls = []

    def reply(self, question):
        self.calls.append(question)
        return "お声がけありがとうございます。", {"input_tokens": 50, "output_tokens": 20}


def make_handler(tmp_path, monkeypatch, members=(MEMBER,), guest=None, **overrides):
    monkeypatch.setenv("CHATWORK_WEBHOOK_TOKEN", TOKEN)
    (tmp_path / "銀行明細取得.md").write_text("# 銀行明細取得\n手順は毎月5日\n", encoding="utf-8")
    config = Config.load(tmp_path / "no-config.json")
    config.data["allowed_room_ids"] = [ROOM]
    config.data["webhook_async"] = False
    config.data["state_dir"] = str(tmp_path / "state")
    config.data["audit_log_dir"] = str(tmp_path / "logs")
    config.data["member_account_ids"] = list(members)
    config.base_dir = tmp_path

    chatwork = FakeChatwork()
    generator = FakeGenerator(
        Answer(has_answer=True, text="毎月5日です", sources=[], usage={"input_tokens": 10})
    )
    audit = FakeAudit()
    handler = RaizuinuHandler(
        config,
        chatwork=chatwork,
        generator=generator,
        audit=audit,
        handbook_loader=HandbookLoader([tmp_path], ["*.md"], [], 300),
        guest=guest if guest is not None else FakeGuest(),
        **overrides,
    )
    return handler, chatwork, generator, audit


def payload(account_id, body="[To:999] 銀行明細の取得手順は？", message_id="1", send_time=1700000000):
    return json.dumps(
        {
            "webhook_event_type": "mention_to_me",
            "webhook_event": {
                "from_account_id": account_id,
                "to_account_id": 999,
                "room_id": ROOM,
                "message_id": message_id,
                "body": body,
                "send_time": send_time,
            },
        }
    ).encode()


class TestMemberGate:
    def test_member_gets_the_normal_answer(self, tmp_path, monkeypatch):
        handler, chatwork, generator, audit = make_handler(tmp_path, monkeypatch)
        raw = payload(MEMBER)
        handler.handle_webhook(raw, sign(raw))
        assert generator.calls  # ハンドブックを使う通常フローに入る
        assert "毎月5日です" in chatwork.sent[0][1]

    def test_guest_never_reaches_the_handbook(self, tmp_path, monkeypatch):
        guest = FakeGuest()
        handler, chatwork, generator, audit = make_handler(tmp_path, monkeypatch, guest=guest)
        raw = payload(GUEST)
        handler.handle_webhook(raw, sign(raw))
        assert not generator.calls  # 社内ナレッジの生成経路へ入れない
        assert guest.calls == ["銀行明細の取得手順は？"]
        assert "お声がけありがとうございます。" in chatwork.sent[0][1]
        assert audit.records[-1]["type"] == "guest"

    def test_guest_gate_is_off_when_no_members_configured(self, tmp_path, monkeypatch):
        # 設定が空なら従来どおり（うっかり全員遮断しない）
        handler, chatwork, generator, _ = make_handler(tmp_path, monkeypatch, members=())
        raw = payload(GUEST)
        handler.handle_webhook(raw, sign(raw))
        assert generator.calls

    def test_guest_cannot_reach_document_or_schedule_flows(self, tmp_path, monkeypatch):
        class Exploding:
            def run(self, *args, **kwargs):
                raise AssertionError("部外の方をこのフローへ通してはいけない")

        guest = FakeGuest()
        handler, chatwork, _, _ = make_handler(
            tmp_path, monkeypatch, guest=guest,
            doc_build=Exploding(), schedule=Exploding(), doc_task=Exploding(),
        )
        for body in (
            "[To:999] 南都銀行あての送付状を作って",
            "[To:999] 足立さんの今日の予定教えて",
            "[To:999] この会議の文字起こしを議事録にして",
        ):
            raw = payload(GUEST, body=body, message_id=str(hash(body) % 10**6))
            handler.handle_webhook(raw, sign(raw))
        assert len(guest.calls) == 3


class TestFollowup:
    def test_reply_after_an_ask_back_is_handled_normally(self, tmp_path, monkeypatch):
        guest = FakeGuest()
        handler, chatwork, generator, _ = make_handler(tmp_path, monkeypatch, guest=guest)
        # アシスタントが聞き返した状態を作る
        from raizuinu.webhook import MentionEvent

        handler._save_pending(
            MentionEvent(room_id=ROOM, message_id="9", account_id=GUEST, body="", send_time=1700000000),
            "doc_task",
            "書類送付状を作って",
        )
        raw = payload(GUEST, body="[To:999] 株式会社Aです", message_id="10", send_time=1700000600)
        handler.handle_webhook(raw, sign(raw))
        assert generator.calls  # 続きなので通常フローで受ける
        assert not guest.calls

    def test_followup_is_good_for_one_message_only(self, tmp_path, monkeypatch):
        guest = FakeGuest()
        handler, chatwork, generator, _ = make_handler(tmp_path, monkeypatch, guest=guest)
        from raizuinu.webhook import MentionEvent

        handler._save_pending(
            MentionEvent(room_id=ROOM, message_id="9", account_id=GUEST, body="", send_time=1700000000),
            "doc_task",
            "書類送付状を作って",
        )
        for mid, t in (("10", 1700000600), ("11", 1700000700)):
            raw = payload(GUEST, body="[To:999] 手順を教えて", message_id=mid, send_time=t)
            handler.handle_webhook(raw, sign(raw))
        assert len(generator.calls) == 1  # 2通目は部外扱いに戻る
        assert len(guest.calls) == 1

    def test_followup_expires(self, tmp_path, monkeypatch):
        guest = FakeGuest()
        handler, chatwork, generator, _ = make_handler(tmp_path, monkeypatch, guest=guest)
        handler._config.data["guest_followup_minutes"] = 30
        from raizuinu.webhook import MentionEvent

        handler._save_pending(
            MentionEvent(room_id=ROOM, message_id="9", account_id=GUEST, body="", send_time=1700000000),
            "doc_task",
            "書類送付状を作って",
        )
        # 31分後の返信は続きとみなさない
        raw = payload(GUEST, body="[To:999] 手順を教えて", message_id="10", send_time=1700000000 + 31 * 60)
        handler.handle_webhook(raw, sign(raw))
        assert not generator.calls
        assert guest.calls


class TestGuestResponder:
    def test_prompt_carries_no_handbook(self, tmp_path):
        # 材料を渡さないことが担保。指示だけに頼らない
        assert "ハンドブック" not in SYSTEM_PROMPT
        assert "社内の手順・ルール・マニュアルの内容には答えない" in SYSTEM_PROMPT

    def test_api_failure_falls_back_to_a_fixed_message(self, tmp_path):
        class Broken:
            class messages:
                @staticmethod
                def create(**kwargs):
                    raise RuntimeError("API障害")

        config = Config.load(tmp_path / "no-config.json")
        reply, usage = GuestResponder(config, client=Broken()).reply("こんにちは")
        assert reply == FALLBACK  # 無言で終わらせない
        assert usage == {}
