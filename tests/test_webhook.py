import base64
import hashlib
import hmac
import json

import pytest

from raizuinu.webhook import (
    MentionEvent,
    SignatureError,
    parse_mention,
    verify_signature,
)

TOKEN = base64.b64encode(b"secret-key-for-test").decode()


def sign(body: bytes, token: str = TOKEN) -> str:
    digest = hmac.new(base64.b64decode(token), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def mention_payload(**overrides) -> bytes:
    event = {
        "from_account_id": 111,
        "to_account_id": 999,
        "room_id": 12345,
        "message_id": "100001",
        "body": "[To:999]らいずいぬさん\n銀行明細の取得手順を教えて",
        "send_time": 1700000000,
        "update_time": 0,
    }
    event.update(overrides)
    payload = {
        "webhook_setting_id": "1",
        "webhook_event_type": "mention_to_me",
        "webhook_event_time": 1700000001,
        "webhook_event": event,
    }
    return json.dumps(payload).encode()


class TestVerifySignature:
    def test_valid_signature_passes(self):
        body = mention_payload()
        verify_signature(body, sign(body), TOKEN)  # 例外が出ないこと

    def test_wrong_signature_rejected(self):
        body = mention_payload()
        with pytest.raises(SignatureError):
            verify_signature(body, sign(b"other-body"), TOKEN)

    def test_missing_signature_rejected(self):
        with pytest.raises(SignatureError):
            verify_signature(mention_payload(), None, TOKEN)

    def test_missing_token_rejected(self):
        body = mention_payload()
        with pytest.raises(SignatureError):
            verify_signature(body, sign(body), None)

    def test_tampered_body_rejected(self):
        body = mention_payload()
        signature = sign(body)
        with pytest.raises(SignatureError):
            verify_signature(body + b" ", signature, TOKEN)


class TestParseMention:
    def test_parses_mention_event(self):
        event = parse_mention(mention_payload())
        assert isinstance(event, MentionEvent)
        assert event.room_id == 12345
        assert event.message_id == "100001"
        assert event.account_id == 111

    def test_strips_chatwork_tags_from_question(self):
        event = parse_mention(mention_payload())
        assert event.question == "らいずいぬさん\n銀行明細の取得手順を教えて"
        assert "[To:" not in event.question

    def test_strips_reply_tags(self):
        event = parse_mention(
            mention_payload(body="[rp aid=999 to=12345-1][pname:999]さん 質問です")
        )
        assert event.question == "さん 質問です"

    def test_non_mention_event_returns_none(self):
        payload = json.loads(mention_payload())
        payload["webhook_event_type"] = "message_created"
        assert parse_mention(json.dumps(payload).encode()) is None
