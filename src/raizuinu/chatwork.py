"""Chatwork REST APIクライアント。

使う操作は「メッセージ送信」「直近メッセージ取得」「添付ファイルの取得」
「ファイルのアップロード」の4つ（要件定義書4章の判断どおりMCPは使わずREST直接）。
送信ロジックを本クラスに隔離しているため、将来公式MCPへ差し替える場合も
本ファイルの変更で完結する。
"""

from __future__ import annotations

import re
import uuid
from typing import Any
from urllib.parse import quote

import requests

API_BASE = "https://api.chatwork.com/v2"
# ファイルアップロードのAPI上限（プランによらず1ファイル5MB）
MAX_UPLOAD_BYTES = 5 * 1024 * 1024


class ChatworkError(Exception):
    """Chatwork API呼び出しの失敗。"""


# 本文の先頭に並ぶ宛先・返信のタグ（と、Chatworkが付ける「氏名さん」の表示名。
# 「坂田 美穂さん」のように姓名の間に空白が入る。名前の直後に本文が続く「田中さんへ」は名前と見ない）
_MENTION_HEAD_RE = re.compile(
    r"^(?:\[(?:To|rp)[^\]]*\][ \t　]*"
    r"(?:[^\s\[\]]{1,12}(?:[ 　][^\s\[\]]{1,12}){0,2}(?:さん|様)(?=\s|$))?[ \t　]*)+"
)


def break_after_mentions(body: str) -> str:
    """宛先（[To:]）・返信（[rp]）のタグの後で必ず改行する。

    指示（2026-09-24）: 部のメンバーへ送るとき、メンションの後ろに本文を続けず改行してから書く。
    どこから送っても同じになるよう、送信の入口でそろえる。タグに続く「氏名さん」は
    Chatworkの表示の一部なので同じ行に残し、本文はその次の行から始める。
    """
    text = str(body or "")
    match = _MENTION_HEAD_RE.match(text)
    if not match:
        return text
    head, rest = text[: match.end()], text[match.end():]
    if not rest or rest.startswith("\n"):
        return text
    return head.rstrip(" \t　") + "\n" + rest


class ChatworkClient:
    def __init__(self, api_token: str, timeout_seconds: int = 30) -> None:
        if not api_token:
            raise ValueError("CHATWORK_API_TOKEN が設定されていません")
        self._headers = {"X-ChatWorkToken": api_token}
        self._timeout = timeout_seconds

    def send_message(self, room_id: int, body: str) -> str:
        """メッセージを送信し、message_idを返す。宛先タグの後は必ず改行してから本文。"""
        resp = requests.post(
            f"{API_BASE}/rooms/{room_id}/messages",
            headers=self._headers,
            data={"body": break_after_mentions(body)},
            timeout=self._timeout,
        )
        self._raise_for_status(resp)
        return str(resp.json().get("message_id", ""))

    def get_me(self) -> int:
        """自分（エージェントアカウント）のaccount_idを返す。"""
        resp = requests.get(
            f"{API_BASE}/me", headers=self._headers, timeout=self._timeout
        )
        self._raise_for_status(resp)
        return int(resp.json().get("account_id"))

    def get_file_info(self, room_id: int, file_id: int) -> dict[str, Any]:
        """添付ファイルの情報（filename・filesize・30秒有効のdownload_url）を返す。"""
        resp = requests.get(
            f"{API_BASE}/rooms/{room_id}/files/{file_id}",
            headers=self._headers,
            params={"create_download_url": 1},
            timeout=self._timeout,
        )
        self._raise_for_status(resp)
        return resp.json()

    def upload_file(
        self, room_id: int, filename: str, data: bytes, message: str = ""
    ) -> str:
        """ファイルをルームへアップロードし、file_idを返す。

        本文（message）を添えると、ファイルの前にその本文が付いた1件の
        メッセージとして投稿される。
        """
        if len(data) > MAX_UPLOAD_BYTES:
            raise ChatworkError(
                f"ファイルが大きすぎます（{len(data):,}バイト / 上限 {MAX_UPLOAD_BYTES:,}バイト）"
            )
        body, content_type = _multipart(filename, data, message)
        resp = requests.post(
            f"{API_BASE}/rooms/{room_id}/files",
            headers={**self._headers, "Content-Type": content_type},
            data=body,
            timeout=self._timeout,
        )
        self._raise_for_status(resp)
        return str(resp.json().get("file_id", ""))

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


class OneShotCache:
    """1回の巡回のあいだ、同じルームの取得を1度で済ませる包み。

    巡回では過去ログの保存・レターパックの追跡・議論の見回りが、同じルームを
    それぞれ見に行くことがある。取得はAPIの回数を使うだけなので1回にまとめる。

    寿命は1回の実行だけにすること。常駐プロセスで使い回すと、古い内容を
    掴んだまま動き続けることになる。
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self._messages: dict[int, list[dict[str, Any]]] = {}
        self._me: int | None = None

    def get_recent_messages(self, room_id: int, limit: int = 20) -> list[dict[str, Any]]:
        room_id = int(room_id)
        if room_id not in self._messages:
            # 本体は常に最新100件を取り、呼び出し側の件数で切って返す。
            # まとめて持っておけば、より多い件数を求められても取り直さずに済む
            self._messages[room_id] = self._client.get_recent_messages(room_id, limit=100)
        return self._messages[room_id][-limit:]

    def get_me(self) -> int:
        if self._me is None:
            self._me = self._client.get_me()
        return self._me

    def __getattr__(self, name: str) -> Any:
        # 送信など、まとめる意味のない操作はそのまま本体へ渡す
        return getattr(self._client, name)


def _multipart(filename: str, data: bytes, message: str) -> tuple[bytes, str]:
    """multipart/form-dataの本文を自前で組む。

    filename には生のUTF-8をそのまま書き（HTML5の流儀。requestsと同じ）、
    加えて filename*=UTF-8'' も併記する（RFC 5987の流儀）。どちらの解釈でも
    同じ名前が読めるようにして、日本語ファイル名の文字化けを避ける。
    """
    boundary = uuid.uuid4().hex
    # ダブルクォートと改行はヘッダを壊すので落とす
    safe_name = re.sub(r'[",\r\n]', "_", filename) or "document"
    parts: list[bytes] = []
    if message:
        parts += [
            f'--{boundary}\r\nContent-Disposition: form-data; name="message"\r\n\r\n'.encode(),
            message.encode("utf-8"),
            b"\r\n",
        ]
    parts += [
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{safe_name}"; '
            f"filename*=UTF-8''{quote(filename)}\r\n"
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode(),
        data,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


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
