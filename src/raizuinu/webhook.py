"""Chatwork Webhookの署名検証とペイロード解析。

署名仕様: リクエストボディに対し、Webhookトークン（BASE64文字列）を
BASE64デコードした鍵でHMAC-SHA256ダイジェストを取り、BASE64エンコードした値が
`X-ChatWorkWebhookSignature` ヘッダーで送られてくる。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass

SIGNATURE_HEADER = "X-ChatWorkWebhookSignature"

# 本文中のChatworkタグ（[To:123] [rp aid=...] [piconname:...] 等）を除去する
_TAG_PATTERN = re.compile(r"\[/?[a-zA-Z]+[^\]]*\]")


class SignatureError(Exception):
    """署名が不正、または検証に必要な情報が欠けている。"""


def verify_signature(raw_body: bytes, signature: str | None, webhook_token: str | None) -> None:
    """署名を検証する。不正なら SignatureError を送出。"""
    if not webhook_token:
        raise SignatureError("CHATWORK_WEBHOOK_TOKEN が設定されていません")
    if not signature:
        raise SignatureError("署名ヘッダーがありません")
    try:
        key = base64.b64decode(webhook_token)
    except Exception as exc:
        raise SignatureError("Webhookトークンの形式が不正です") from exc
    digest = hmac.new(key, raw_body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("ascii")
    if not hmac.compare_digest(expected, signature):
        raise SignatureError("署名が一致しません")


def strip_chatwork_tags(text: str) -> str:
    """Chatworkタグ（[To:...] [rp ...] 等）を除去した本文を返す。"""
    return _TAG_PATTERN.sub("", str(text)).strip()


@dataclass
class MentionEvent:
    """自分宛メンション（mention_to_me）イベント。"""

    room_id: int
    message_id: str
    account_id: int
    body: str
    send_time: int

    @property
    def question(self) -> str:
        """Chatworkタグを除去した質問本文。"""
        return _TAG_PATTERN.sub("", self.body).strip()


def parse_mention(raw_body: bytes) -> MentionEvent | None:
    """Webhookペイロードを解析する。mention_to_me 以外は None を返す。"""
    payload = json.loads(raw_body.decode("utf-8"))
    if payload.get("webhook_event_type") != "mention_to_me":
        return None
    event = payload.get("webhook_event") or {}
    return MentionEvent(
        room_id=int(event["room_id"]),
        message_id=str(event["message_id"]),
        account_id=int(event["from_account_id"]),
        body=str(event.get("body", "")),
        send_time=int(event.get("send_time", 0)),
    )
