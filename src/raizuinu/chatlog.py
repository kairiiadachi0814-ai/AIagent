"""チャットの過去ログを貯めて、そこから答える。

「楽天BillPayのパスワードは？」のように、ハンドブックには無いがチャットの
どこかで共有された値を聞かれることがある。ChatworkのAPIは直近100件しか
返さない（limit・before・offset は効かない）ため、巡回のたびに見えた発言を
手元へ書き写して積み上げ、そこを検索する。

方針:
- 検索するのは**質問が来たルームだけ**。ルームをまたいで探さない
  （そのルームの人しか見られない情報を、外の人へ渡さないため）
- 同じ項目が複数見つかったら**最新を正**とする（パスワードは変わる）
- 過去ログから答えたことを**必ず返信に書く**。古い値の可能性を伝えるため
- 保存するのは発言者・本文・日時のみ。検索に要らないものは持たない
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

JST = timezone(timedelta(hours=9))

# 検索語として拾う語。英数字・カタカナ・漢字のまとまりだけを見る
_TERM_RE = re.compile(
    r"[A-Za-z][A-Za-z0-9._+-]+"  # BillPay, freee, e-Tax
    r"|[0-9]{3,}"  # 口座番号のような数字の並び
    r"|[ァ-ヶ][ァ-ヶー]+"  # カタカナ語
    r"|[一-龥]{2,}"  # 漢字2文字以上
)
# 質問の言い回しそのもの。検索語にすると関係ない発言まで拾う
_STOPWORDS = frozenset(
    """
    教え 確認 何時 場所 方法 内容 状況 対応 質問 返信 連絡 依頼 報告 資料 添付
    過去 履歴 検索 最新 以前 前回 今回 本日 昨日 明日 今日 現在 当社 弊社 社内
    お願 ください 下さい ありがと よろしく すみません
    """.split()
)


def search_terms(question: str) -> list[str]:
    """質問文から検索に使う語を取り出す。

    「楽天BillPayのパスワード教えて」→ ["楽天", "BillPay", "パスワード"]
    """
    text = unicodedata.normalize("NFKC", str(question or ""))
    terms: list[str] = []
    for match in _TERM_RE.finditer(text):
        term = match.group(0)
        if term in _STOPWORDS or any(term.startswith(s) for s in _STOPWORDS):
            continue
        if term not in terms:
            terms.append(term)
    return terms


class ChatArchive:
    """見えた発言を貯めておく置き場（SQLite）。

    メッセージIDを主キーにしているので、巡回が重複して拾っても増えない。
    """

    def __init__(self, path: Path, retention_days: int = 730) -> None:
        self._path = Path(path)
        self._retention_days = int(retention_days)
        self._ready = False

    # --- 公開API ---

    def record(self, room_id: int, messages: list[dict[str, Any]]) -> int:
        """発言を書き写す。→ 新しく入った件数。"""
        rows = []
        for message in messages or []:
            account = message.get("account") or {}
            body = str(message.get("body", ""))
            if not body.strip():
                continue
            rows.append(
                (
                    str(message.get("message_id", "")),
                    int(room_id),
                    int(account.get("account_id", 0) or 0),
                    str(account.get("name", "")),
                    body,
                    int(message.get("send_time", 0) or 0),
                )
            )
        if not rows:
            return 0
        with self._connect() as db:
            before = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            db.executemany(
                "INSERT OR IGNORE INTO messages"
                " (message_id, room_id, account_id, name, body, send_time)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
            after = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        return after - before

    def search(self, room_id: int, terms: list[str], limit: int = 8) -> list[dict[str, Any]]:
        """語を含む発言を、当てはまりの良い順・新しい順で返す。

        同じ項目が何度も投稿されている場合に最新を選べるよう、必ず新しい順で
        並べ替えてから返す。
        """
        if not terms:
            return []
        where = " OR ".join("body LIKE ?" for _ in terms)
        params: list[Any] = [int(room_id)] + [f"%{t}%" for t in terms]
        with self._connect() as db:
            rows = db.execute(
                "SELECT message_id, account_id, name, body, send_time FROM messages"
                f" WHERE room_id = ? AND ({where})"
                " ORDER BY send_time DESC LIMIT 400",
                params,
            ).fetchall()
        hits = []
        for row in rows:
            body = row["body"]
            matched = sum(1 for t in terms if t in body)
            hits.append(
                {
                    "message_id": row["message_id"],
                    "account_id": row["account_id"],
                    "name": row["name"],
                    "body": body,
                    "send_time": int(row["send_time"]),
                    "matched": matched,
                }
            )
        # 当てはまりの良さが同じなら新しいものを先に（最新を正とするため）
        hits.sort(key=lambda h: (h["matched"], h["send_time"]), reverse=True)
        return hits[:limit]

    def prune(self) -> int:
        """保存期間を過ぎた発言を消す。→ 消した件数。"""
        cutoff = int((datetime.now(JST) - timedelta(days=self._retention_days)).timestamp())
        with self._connect() as db:
            cursor = db.execute("DELETE FROM messages WHERE send_time < ?", (cutoff,))
            return cursor.rowcount or 0

    def stats(self) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS n, MIN(send_time) AS oldest, MAX(send_time) AS newest"
                " FROM messages"
            ).fetchone()
        return {"count": row["n"], "oldest": row["oldest"], "newest": row["newest"]}

    # --- 内部 ---

    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self._path)
        db.row_factory = sqlite3.Row
        if not self._ready:
            db.execute(
                "CREATE TABLE IF NOT EXISTS messages ("
                " message_id TEXT PRIMARY KEY, room_id INTEGER, account_id INTEGER,"
                " name TEXT, body TEXT, send_time INTEGER)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_room_time ON messages (room_id, send_time)"
            )
            db.commit()
            self._ready = True
        return db


LOOKUP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "has_answer": {
            "type": "boolean",
            "description": "渡された発言だけで答えが分かるなら true。分からなければ false",
        },
        "answer": {
            "type": "string",
            "description": (
                "質問への答え。発言に書かれている値をそのまま写す。"
                "値を丸めたり言い換えたりしない"
            ),
        },
        "used_index": {
            "type": "integer",
            "description": "答えの根拠にした発言の番号（渡した一覧の番号）。無ければ -1",
        },
        "superseded": {
            "type": "boolean",
            "description": "同じ項目の古い発言も一覧にあり、新しいほうを採ったなら true",
        },
    },
    "required": ["has_answer", "answer", "used_index", "superseded"],
    "additionalProperties": False,
}

LOOKUP_SYSTEM = """あなたは社内チャットの過去ログから答えを探す担当です。

渡されるのは、同じチャットルームの過去の発言です。新しい順に並んでいます。

厳守すること:
- 発言に書かれていることだけで答える。書かれていないことを推測で補わない
- 同じ項目について複数の発言がある場合（パスワードの変更など）は、**必ず
  いちばん新しい発言を正とする**。古い値を答えてはならない
- 値（パスワード・番号・URL・金額・日付）は発言のとおり一字一句正確に写す
- 答えが見つからなければ has_answer を false にする。近そうな別の話題で
  埋め合わせない
- 答えは短く。前置きや言い訳を書かない
"""


class ChatLogAnswerer:
    """過去ログの検索結果から答えを組み立てる。"""

    def __init__(self, config: Any, archive: ChatArchive, client: Any | None = None) -> None:
        self._config = config
        self._archive = archive
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self._client = client

    def lookup(self, room_id: int, question: str) -> tuple[str, dict[str, Any], dict[str, int]]:
        """→ (返信本文, 監査用メタ, usage)。答えが無ければ本文は空文字。"""
        meta: dict[str, Any] = {"terms": [], "hits": 0}
        settings = self._config.chat_archive
        if int(room_id) not in [int(r) for r in settings.get("room_ids") or []]:
            return "", meta, {}  # 貯めていないルームは探しに行かない
        terms = search_terms(question)
        meta["terms"] = terms
        hits = self._archive.search(room_id, terms, int(settings.get("max_hits", 8)))
        meta["hits"] = len(hits)
        if not hits:
            return "", meta, {}

        found, usage = self._ask(question, hits)
        meta["used_message_id"] = ""
        if not found.get("has_answer"):
            return "", meta, usage
        index = int(found.get("used_index", -1))
        used = hits[index] if 0 <= index < len(hits) else hits[0]
        meta["used_message_id"] = used["message_id"]
        meta["used_send_time"] = used["send_time"]
        return self._format(found, used, bool(found.get("superseded"))), meta, usage

    # --- 内部 ---

    def _ask(self, question: str, hits: list[dict]) -> tuple[dict[str, Any], dict[str, int]]:
        from .answer import _call_with_continuation
        from .webhook import strip_chatwork_tags

        lines = []
        for index, hit in enumerate(hits):
            when = datetime.fromtimestamp(hit["send_time"], JST).strftime("%Y年%m月%d日 %H:%M")
            body = strip_chatwork_tags(hit["body"])[:1500]
            lines.append(f"[{index}] {when} {hit['name']}\n{body}")
        prompt = (
            f"質問: {question}\n\n"
            "===過去の発言（新しい順）ここから===\n"
            + "\n\n".join(lines)
            + "\n===過去の発言ここまで==="
        )
        cfg = self._config
        kwargs = {
            "model": cfg.model,
            "max_tokens": int(cfg.max_tokens),
            "output_config": {
                "effort": "low",
                "format": {"type": "json_schema", "schema": LOOKUP_SCHEMA},
            },
            "system": LOOKUP_SYSTEM,
        }
        response, usage, _ = _call_with_continuation(
            self._client.messages.create, kwargs, [{"role": "user", "content": prompt}]
        )
        text = next(
            (b.text for b in getattr(response, "content", []) if getattr(b, "type", "") == "text"),
            "",
        )
        try:
            return json.loads(text), usage
        except (json.JSONDecodeError, TypeError):
            return {"has_answer": False}, usage

    @staticmethod
    def _format(found: dict[str, Any], used: dict[str, Any], superseded: bool) -> str:
        """答えの末尾に、過去ログから拾ったことを必ず書く。"""
        when = datetime.fromtimestamp(used["send_time"], JST).strftime("%Y年%m月%d日")
        note = (
            f"※このルームの過去のやり取りを遡ってお答えしています"
            f"（{when} の{used['name']}さんの発言より）。"
        )
        if superseded:
            note += "同じ内容の古い発言もありましたが、最新のものを採用しています。"
        note += "その後に変更されている可能性もあるため、念のためご確認ください。"
        return f"{str(found.get('answer', '')).strip()}\n\n{note}"
