"""管理者の確認を経て、別のルームへお知らせを投稿する。

依頼者以外がいるルームへの書き込みなので、レターパック（FR-09）と同じく
「投稿する文面をそのまま見せて『送信』の返事を得てから投稿する」を守る
（要件定義書 ガードレール4）。

使い方（VPS上）:
    python -m raizuinu.announce --room 446282163 --file 文面.txt [--note 一言]
→ 管理者ルームへ文面と確認の一言を投稿し、控えを持つ。
  管理者がその投稿への返信で「送信」と書けば投稿先へそのまま投稿し、
  「取りやめ」と書けば捨てる。直しは Claude Code 側で文面を作り直して出し直す。
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

JST = timezone(timedelta(hours=9))

_SEND_RE = re.compile(r"(送信|投稿して|投稿お願い|OK|オーケー|お願いします|それで|問題な[いし]|大丈夫)", re.I)
_CANCEL_RE = re.compile(r"(取りやめ|取り止め|やめ|キャンセル|中止|送らな|ボツ|なし)")


class Announcer:
    """お知らせの下書きを管理者ルームで見せ、「送信」で投稿先へ流す。"""

    def __init__(self, config: Any, chatwork: Any, ttl_hours: int = 48) -> None:
        self._config = config
        self._chatwork = chatwork
        self._ttl = timedelta(hours=ttl_hours)
        self._path = config.resolve_path(config.state_dir) / "announce.json"

    # --- 公開API ---

    def propose(self, target_room_id: int, text: str, note: str = "") -> str:
        """管理者ルームへ下書きと確認の一言を投稿し、控えを持つ。→ 投稿したメッセージID"""
        admin_room = int(self._config.admin_room_id or 0)
        if not admin_room:
            raise RuntimeError("admin_room_id が設定されていません")
        lead = (note.strip() + "\n") if note.strip() else ""
        body = (
            f"{lead}次の文面をルーム {int(target_room_id)} へ投稿してよいか、ご確認ください。\n"
            "この投稿への返信で「送信」とお知らせいただければ、このまま投稿します。"
            "直したいところがあれば、その旨をお知らせください（文面を作り直して出し直します）。\n"
            "――――――――――\n"
            f"{text}"
        )
        message_id = str(self._chatwork.send_message(admin_room, body) or "")
        self._save(
            {
                "target_room_id": int(target_room_id),
                "text": text,
                "note": note,
                "proposed_at": datetime.now(JST).isoformat(),
                "message_id": message_id,
            }
        )
        return message_id

    def handle(self, room_id: int, account_id: int, text: str) -> str | None:
        """管理者ルームでの返事を見る。扱ったら返信文、無関係なら None。"""
        pending = self._load()
        if not pending:
            return None
        proposed_at = _parse(pending.get("proposed_at"))
        if proposed_at and datetime.now(JST) - proposed_at > self._ttl:
            self._save({})
            return None
        if int(room_id) != int(self._config.admin_room_id or 0):
            return None
        if int(account_id) not in {int(a) for a in self._config.admin_account_ids}:
            return None
        if _CANCEL_RE.search(text) and not _SEND_RE.search(text):
            self._save({})
            return "承知しました。このお知らせは送らずに取りやめます。"
        if _SEND_RE.search(text):
            target = int(pending["target_room_id"])
            self._chatwork.send_message(target, str(pending["text"]))
            self._save({})
            return f"ルーム {target} へ投稿しました。"
        return None

    def pending(self) -> dict:
        return self._load()

    # --- 内部 ---

    def _load(self) -> dict:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _parse(text: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None


def main(argv: list[str] | None = None) -> int:
    import argparse

    from .chatwork import ChatworkClient
    from .config import Config

    parser = argparse.ArgumentParser(description="管理者の確認を経て、お知らせを投稿する")
    parser.add_argument("--room", type=int, required=True, help="投稿先ルームID")
    parser.add_argument("--file", required=True, help="文面のテキストファイル（UTF-8）")
    parser.add_argument("--note", default="", help="管理者への一言（任意）")
    args = parser.parse_args(argv)

    config = Config.load()
    text = Path(args.file).read_text(encoding="utf-8").strip()
    announcer = Announcer(config, ChatworkClient(config.chatwork_api_token or ""))
    message_id = announcer.propose(args.room, text, note=args.note)
    print(f"管理者ルームへ確認を投稿しました（message_id={message_id}）。返信で「送信」と書かれたら投稿します。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
