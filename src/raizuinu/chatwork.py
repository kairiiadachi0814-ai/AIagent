"""Chatwork REST APIクライアント。

Phase 1で使う操作は「メッセージ送信」と「直近メッセージ取得」の2つのみ
（要件定義書4章の判断どおりMCPは使わずREST直接）。送信ロジックを本クラスに
隔離しているため、将来公式MCPへ差し替える場合も本ファイルの変更で完結する。
"""

from __future__ import annotations

from typing import Any

import requests

API_BASE = "https://api.chatwork.com/v2"


class ChatworkError(Exception):
    """Chatwork API呼び出しの失敗。"""


class ChatworkClient:
    def __init__(self, api_token: str, timeout_seconds: int = 30) -> None:
        if not api_token:
            raise ValueError("CHATWORK_API_TOKEN が設定されていません")
        self._headers = {"X-ChatWorkToken": api_token}
        self._timeout = timeout_seconds

    def send_message(self, room_id: int, body: str) -> str:
        """メッセージを送信し、message_idを返す。"""
        resp = requests.post(
            f"{API_BASE}/rooms/{room_id}/messages",
            headers=self._headers,
            data={"body": body},
            timeout=self._timeout,
        )
        self._raise_for_status(resp)
        return str(resp.json().get("message_id", ""))

    def get_recent_messages(self, room_id: int, limit: int = 20) -> list[dict[str, Any]]:
        """直近のメッセージを古い順で最大limit件返す。

        Chatwork APIは force=1 で最新100件を返す。未読管理に影響させないよう
        force=1 固定で取得し、末尾limit件に絞る。
        """
        resp = requests.get(
            f"{API_BASE}/rooms/{room_id}/messages",
            headers=self._headers,
            params={"force": 1},
            timeout=self._timeout,
        )
        if resp.status_code == 204:
            return []
        self._raise_for_status(resp)
        messages = resp.json() or []
        return messages[-limit:]

    @staticmethod
    def _raise_for_status(resp: requests.Response) -> None:
        if resp.status_code >= 400:
            raise ChatworkError(
                f"Chatwork APIエラー: HTTP {resp.status_code} {resp.text[:200]}"
            )


def format_context(messages: list[dict[str, Any]], exclude_message_id: str | None = None) -> str:
    """直近メッセージ一覧を会話コンテキスト文字列にする。"""
    lines = []
    for msg in messages:
        if exclude_message_id and str(msg.get("message_id")) == str(exclude_message_id):
            continue
        account = msg.get("account") or {}
        name = account.get("name", "不明")
        body = str(msg.get("body", "")).strip()
        if body:
            lines.append(f"{name}: {body}")
    return "\n".join(lines)
