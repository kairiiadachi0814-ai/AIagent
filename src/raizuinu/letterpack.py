"""レターパックの手配依頼 — 送付状を作ったら総務へ取り次ぐ。

送付状を作るということは郵送する見込みが高い。作った直後に手配の要否を
確認し、必要なら総務あての依頼を代わりに出して、返信を依頼者へ返す。

流れ:
1. 送付状を作った返信の末尾で「手配も依頼しますか」と確認する
2. 枚数・種類を答えてもらったら、総務あての依頼文案を見せて送信確認をとる
3. 「送信」で備品・消耗品購入依頼チャットへ投稿し、やり取りを追跡する
4. 総務からの返信は5分ごとの巡回（watcher）で拾い、依頼者へ伝える。
   質問なら依頼者の答えを備品ルームへ返し、完了ならお礼を返して締める

方針:
- 備品ルームは投稿と巡回にだけ使い、許可ルーム（Q&Aの対象）には入れない。
  他部署のルームへ社内ナレッジが流れる経路を作らないため
- 依頼文は必ず依頼者に見せてから送る。他部署のルームへの投稿は取り消しが
  きかないため、読み取りを誤ったまま届くことを避ける
- 誰の依頼かを依頼文に明記する（総務側が誰に確認すればよいか分かるように）
- 枚数・種類の読み取りは正規表現で行う（モデルに数えさせない）
"""

from __future__ import annotations

import json
import re
import traceback
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

JST = timezone(timedelta(hours=9))

# レターパックの種類。金額はJP（日本郵便）の区分で、依頼文にはこの表記を使う
KINDS = {
    "プラス": "レターパックプラス",
    "ライト": "レターパックライト",
}
_PLUS_RE = re.compile(r"(プラス|ぷらす|plus|赤|520)", re.I)
_LIGHT_RE = re.compile(r"(ライト|らいと|light|青|370|430)", re.I)
_COUNT_RE = re.compile(r"(\d+)\s*(?:枚|通|部)")
_DECLINE_RE = re.compile(r"(不要|いらな|要らな|結構です|なし$|無し$|いいえ|大丈夫です|やめ|見送)")
_SEND_RE = re.compile(r"(送信|送って|送付して|依頼して|お願いします|これでお願い|ok|OK|オーケー|了解|はい)")
_CANCEL_RE = re.compile(r"(取消|取り消|やめ|中止|キャンセル|やっぱり)")
# 備品ルームは総務が部署全体の依頼をさばく場。こちらの依頼への返事だけを拾うため、
# 返信タグ・宛先タグでこちらのメッセージを指しているものに限る
_RP_RE = re.compile(r"\[rp\s+aid=\d+\s+to=(\d+)-(\d+)\]")
_TO_RE = re.compile(r"\[To:(\d+)\]")

# 送付状の返信の末尾に足す確認文
OFFER = (
    "郵送でしたら、レターパックの手配を総務へ依頼できます。"
    "必要でしたら枚数と種類（プラス／ライト）をお知らせください。"
    "不要なら「不要」とだけお返事いただければ、この件は閉じます。"
)


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", str(text or "")).strip()


def read_request(text: str) -> dict[str, Any]:
    """依頼者の返事から枚数と種類を読む。

    → {"declined": bool, "count": int|None, "kind": str}
    「不要」と読めれば declined。枚数・種類は読めた分だけ返す
    （足りないぶんはこちらから尋ねる）。
    """
    folded = _fold(text)
    if _DECLINE_RE.search(folded):
        return {"declined": True, "count": None, "kind": ""}
    count_match = _COUNT_RE.search(folded)
    kind = ""
    if _PLUS_RE.search(folded):
        kind = KINDS["プラス"]
    elif _LIGHT_RE.search(folded):
        kind = KINDS["ライト"]
    return {
        "declined": False,
        "count": int(count_match.group(1)) if count_match else None,
        "kind": kind,
    }


def summarize_use(detail: dict[str, Any]) -> str:
    """「宛先と使用内容」の一行。送付状の項目から組み立てる。"""
    to_lines = [str(line).strip() for line in detail.get("to_lines") or [] if str(line).strip()]
    destination = to_lines[0] if to_lines else "（宛先未確認）"
    items = []
    for item in detail.get("items") or []:
        name = str(item.get("name", "")).strip()
        qty = str(item.get("qty", "") or "").strip()
        if name:
            items.append(f"{name} {qty}".strip())
    contents = "・".join(items) if items else "書類"
    return f"{destination}／{contents}の送付"


def mention(account_id: int, name: str) -> str:
    """Chatworkの宛先タグ。

    本文とは分けて持つ。本文は sanitize_for_chatwork を通すため、タグを
    本文へ混ぜると全角に置き換わって相手に通知が飛ばなくなる。文面を見せる
    段階ではあえて本文ごと通し、確認の時点で総務へ通知が飛ばないようにする。
    """
    return f"[To:{account_id}] {name}さん"


def build_request_text(detail: dict[str, Any], count: int, kind: str) -> str:
    """総務あての依頼文（宛先タグを除く本文）。

    誰の依頼かを明記する。名乗りは入れない（投稿元のアカウントで分かるため）。
    """
    requester = str(detail.get("staff") or "").strip()
    on_behalf = f"経理財務部の{requester}さんの依頼です。" if requester else ""
    return (
        f"お疲れさまです。\n"
        f"{on_behalf}レターパックの手配をお願いできますでしょうか。\n"
        f"\n"
        f"・使用会社名: {detail.get('company') or ''}\n"
        f"・宛先と使用内容: {summarize_use(detail)}\n"
        f"・必要枚数: {count}枚\n"
        f"・種類: {kind}\n"
        f"\n"
        f"お手数をおかけしますが、よろしくお願いいたします。\n"
        f"（ご返信は、このメッセージへの返信でお願いできますと助かります）"
    )


def room_link(room_id: int, message_id: str | int = "") -> str:
    """Chatworkの該当メッセージへのリンク。"""
    return f"https://www.chatwork.com/#!rid{room_id}" + (f"-{message_id}" if message_id else "")


REPLY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["完了", "質問", "その他"],
            "description": (
                "手配が済んだ・用意した旨なら完了。こちらへの問い合わせなら質問。"
                "判断できなければその他"
            ),
        },
        "summary": {
            "type": "string",
            "description": "依頼者へ伝える内容を1〜2文で。総務の言葉づかいは変えてよい",
        },
        "question": {
            "type": "string",
            "description": "質問のときだけ、聞かれている内容を1文で。それ以外は空文字",
        },
    },
    "required": ["kind", "summary", "question"],
    "additionalProperties": False,
}

REPLY_SYSTEM = (
    "あなたは社内チャットの取次ぎ担当です。備品の手配を依頼した相手（総務）からの"
    "返信を読み、依頼者へ伝えるために内容を整理します。"
    "推測で情報を足さないこと。書かれていないこと（受け取り場所・期日など）を"
    "補わないこと。"
)


class LetterpackError(Exception):
    """レターパック手配の取次ぎに失敗した（利用者向けの文面を持つ）。"""


class LetterpackStore:
    """手配の進行状況（依頼者ごとの待ち状態と、総務とのやり取り）。"""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"offers": {}, "drafts": {}, "threads": {}}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"offers": {}, "drafts": {}, "threads": {}}
        for key in ("offers", "drafts", "threads"):
            data.setdefault(key, {})
        return data

    def save(self, data: dict[str, Any]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            print("[warn] レターパック状態の保存に失敗: " + traceback.format_exc(), flush=True)


class LetterpackRunner:
    """依頼者とのやり取り（要否の確認 → 文面の確認 → 投稿）を受け持つ。"""

    def __init__(
        self,
        config: Any,
        chatwork: Any,
        store: LetterpackStore | None = None,
        phrasebook: Any | None = None,
    ) -> None:
        self._config = config
        self._chatwork = chatwork
        self._store = store or LetterpackStore(
            config.resolve_path(config.state_dir) / "letterpack.json"
        )
        if phrasebook is None:
            from .phrasing import build

            phrasebook = build(config)
        self._phrasebook = phrasebook

    # --- 公開API ---

    @property
    def offer_text(self) -> str:
        return OFFER

    def offer(
        self, room_id: int, account_id: int, send_time: int, detail: dict[str, Any]
    ) -> None:
        """送付状を作った直後に「手配しますか」の待ち状態を作る。"""
        data = self._store.load()
        data["offers"][f"{room_id}:{account_id}"] = {
            "ts": int(send_time),
            "detail": detail,
        }
        self._store.save(data)

    def handle(
        self, room_id: int, account_id: int, send_time: int, text: str, display_name: str = ""
    ) -> str | None:
        """レターパックのやり取りの続きなら返信文を返す。無関係なら None。"""
        data = self._store.load()
        key = f"{room_id}:{account_id}"
        window = int(self._settings.get("reply_window_minutes", 120)) * 60

        draft = data["drafts"].get(key)
        if draft and int(send_time) - int(draft.get("ts", 0)) <= window:
            return self._on_draft_answer(data, key, draft, text, display_name)

        offer = data["offers"].get(key)
        if offer and int(send_time) - int(offer.get("ts", 0)) <= window:
            return self._on_offer_answer(data, key, offer, send_time, text)

        thread = self._find_asked_thread(data, key, send_time, window)
        if thread is not None and not _looks_like_new_request(text):
            return self._on_question_answer(data, thread, text)
        return None

    # --- 内部 ---

    @property
    def _settings(self) -> dict[str, Any]:
        return self._config.letterpack

    def _on_offer_answer(
        self, data: dict, key: str, offer: dict, send_time: int, text: str
    ) -> str:
        """枚数・種類の返事を受けて、総務あての文面を見せる。"""
        answer = read_request(text)
        if answer["declined"]:
            data["offers"].pop(key, None)
            self._store.save(data)
            return self._phrasebook.pick("letterpack_declined")

        detail = offer.get("detail") or {}
        missing = []
        if not answer["count"]:
            missing.append("必要枚数（例: 2枚）")
        if not answer["kind"]:
            missing.append("種類（プラス／ライト）")
        if missing:
            # 待ち状態は残す。読めた分は覚えておき、二度手間にしない
            offer["ts"] = int(send_time)
            offer["count"] = answer["count"] or offer.get("count")
            offer["kind"] = answer["kind"] or offer.get("kind", "")
            data["offers"][key] = offer
            self._store.save(data)
            still = [m for m in missing if not (m.startswith("必要枚数") and offer.get("count"))]
            still = [m for m in still if not (m.startswith("種類") and offer.get("kind"))]
            if not still:
                return self._make_draft(
                    data, key, offer, detail, int(offer["count"]), str(offer["kind"])
                )
            return "手配しますね。これも教えていただけますか。\n" + "\n".join(
                f"・{m}" for m in still
            )

        return self._make_draft(data, key, offer, detail, answer["count"], answer["kind"])

    def _make_draft(
        self, data: dict, key: str, offer: dict, detail: dict, count: int, kind: str
    ) -> str:
        settings = self._settings
        text = build_request_text(detail, count, kind)
        data["offers"].pop(key, None)
        data["drafts"][key] = {
            "ts": int(offer.get("ts", 0)),
            "text": text,
            "detail": detail,
            "count": count,
            "kind": kind,
        }
        self._store.save(data)
        # 送るかどうかを決める前に相手を巻き込まないよう、文面案の宛先タグは
        # ここで無効にしておく（見た目は残る）。送信時に生のタグを付け直す
        from .answer import sanitize_for_chatwork

        head = sanitize_for_chatwork(
            mention(int(settings.get("staff_account_id", 0)), str(settings.get("staff_name", "")))
        )
        return (
            "総務の"
            f"{settings.get('staff_name', '')}さんへ、下記の内容で依頼します。"
            "よろしければ「送信」とお返事ください。直すところがあれば教えてください。\n"
            "\n"
            "――――――――――\n"
            f"{head}\n{text}\n"
            "――――――――――"
        )

    def _on_draft_answer(
        self, data: dict, key: str, draft: dict, text: str, display_name: str
    ) -> str:
        folded = _fold(text)
        if _CANCEL_RE.search(folded):
            data["drafts"].pop(key, None)
            self._store.save(data)
            return self._phrasebook.pick("letterpack_cancelled")
        if not _SEND_RE.search(folded):
            # 文面の直しは読み取らず、作り直しを促す（誤った内容で送らないため）
            return (
                "送ってよければ「送信」とお返事ください。"
                "直すところがあれば、枚数・種類・宛先のどれをどう直すか書いていただければ作り直します。"
            )
        return self._send(data, key, draft, display_name)

    def _send(self, data: dict, key: str, draft: dict, display_name: str) -> str:
        settings = self._settings
        room_id = int(settings.get("supplies_room_id", 0))
        from .answer import sanitize_for_chatwork

        head = mention(
            int(settings.get("staff_account_id", 0)), str(settings.get("staff_name", ""))
        )
        try:
            message_id = self._chatwork.send_message(
                room_id, head + "\n" + sanitize_for_chatwork(draft["text"])
            )
        except Exception as exc:
            raise LetterpackError(
                "すみません、備品・消耗品購入依頼チャットへの投稿に失敗しました。"
                "お手数ですが、直接ご依頼いただけますか。"
            ) from exc

        requester_room, requester_account = key.split(":")
        data["drafts"].pop(key, None)
        data["threads"][str(message_id)] = {
            "requester_room_id": int(requester_room),
            "requester_account_id": int(requester_account),
            "requester_name": display_name,
            "posted_message_id": str(message_id),
            # こちらが備品ルームへ出したメッセージ。総務の返信がどれを指しているかを
            # 突き合わせて、他の依頼への返事を拾わないようにする
            "message_ids": [str(message_id)],
            "last_seen": int(message_id),
            "status": "open",
            "detail": draft.get("detail") or {},
            "count": draft.get("count"),
            "kind": draft.get("kind"),
            "ts": int(datetime.now(JST).timestamp()),
        }
        self._store.save(data)
        return (
            "備品・消耗品購入依頼チャットへ依頼しました。\n"
            f"{room_link(room_id, message_id)}\n"
            "返信があればこちらでお伝えします。"
        )

    def _find_asked_thread(
        self, data: dict, key: str, send_time: int, window: int
    ) -> dict | None:
        requester_room, requester_account = key.split(":")
        for thread in data["threads"].values():
            if thread.get("status") != "asked":
                continue
            if int(thread.get("requester_room_id", 0)) != int(requester_room):
                continue
            if int(thread.get("requester_account_id", 0)) != int(requester_account):
                continue
            if int(send_time) - int(thread.get("asked_ts", 0)) <= window:
                return thread
        return None

    def _on_question_answer(self, data: dict, thread: dict, text: str) -> str:
        """総務からの質問に依頼者が答えた → そのまま備品ルームへ返す。"""
        settings = self._settings
        room_id = int(settings.get("supplies_room_id", 0))
        from .answer import sanitize_for_chatwork

        head = mention(
            int(settings.get("staff_account_id", 0)), str(settings.get("staff_name", ""))
        )
        body = (
            f"お待たせしました。依頼者に確認しました。\n"
            f"\n"
            f"{_fold(text)}\n"
            f"\n"
            f"よろしくお願いいたします。"
        )
        try:
            message_id = self._chatwork.send_message(
                room_id, head + "\n" + sanitize_for_chatwork(body)
            )
        except Exception as exc:
            raise LetterpackError(
                "すみません、備品・消耗品購入依頼チャットへの返信に失敗しました。"
                "お手数ですが、直接お伝えいただけますか。"
            ) from exc
        # この投稿への返信も、同じやり取りの続きとして拾えるようにする
        thread.setdefault("message_ids", []).append(str(message_id))
        thread["status"] = "open"
        thread.pop("asked_ts", None)
        self._store.save(data)
        return self._phrasebook.pick("letterpack_forwarded")


def _looks_like_new_request(text: str) -> bool:
    """別件の依頼（書類作成・予定）なら、質問への答えとして扱わない。"""
    from .docbuild import looks_like_document_build_request
    from .scheduletask import looks_like_schedule_request

    return looks_like_document_build_request(text) or looks_like_schedule_request(text)


class LetterpackFollower:
    """総務からの返信を拾って依頼者へ返す（5分ごとの巡回から呼ぶ）。

    備品ルームはメンションの許可ルームに入れていないため、webhookでは
    受け取れない。総務側にメンションを求めずに済むよう、巡回で拾う。
    """

    def __init__(
        self,
        config: Any,
        chatwork: Any | None = None,
        client: Any | None = None,
        cost: Any | None = None,
        store: LetterpackStore | None = None,
    ) -> None:
        self._config = config
        if chatwork is None:
            from .chatwork import ChatworkClient

            chatwork = ChatworkClient(config.chatwork_api_token or "")
        self._chatwork = chatwork
        self._client = client
        self._cost = cost
        self._store = store or LetterpackStore(
            config.resolve_path(config.state_dir) / "letterpack.json"
        )

    # --- 公開API ---

    def run_once(self) -> None:
        settings = self._config.letterpack
        if not settings.get("enabled") or not settings.get("follow_up", True):
            return
        data = self._store.load()
        threads = {
            key: t for key, t in data["threads"].items() if t.get("status") in ("open", "asked")
        }
        if not threads:
            return  # 追いかけるものが無ければAPIも呼ばない

        room_id = int(settings.get("supplies_room_id", 0))
        staff_id = int(settings.get("staff_account_id", 0))
        try:
            messages = self._chatwork.get_recent_messages(room_id, limit=50)
        except Exception:
            print("[warn] 備品ルームの取得に失敗: " + traceback.format_exc(), flush=True)
            return

        changed = self._expire(data, threads, int(settings.get("max_open_days", 7)))
        newest = max((int(m.get("message_id", 0)) for m in messages), default=0)
        agent_id = self._agent_account_id()

        for message in messages:
            if int((message.get("account") or {}).get("account_id", 0) or 0) != staff_id:
                continue
            key = self._attribute(message, threads, room_id, agent_id)
            if key is None:
                continue  # 他の依頼への返事。こちらの件ではない
            thread = threads[key]
            if int(message.get("message_id", 0)) <= int(thread.get("last_seen", 0)):
                continue  # 取次ぎ済み
            thread["last_seen"] = int(message["message_id"])
            changed = True
            try:
                self._relay(room_id, thread, message)
            except Exception:
                print("[warn] 総務返信の取次ぎに失敗: " + traceback.format_exc(), flush=True)
            data["threads"][key] = thread

        # 拾わなかった発言を毎回見直さないよう、既読位置だけは進めておく
        for key, thread in threads.items():
            if newest > int(thread.get("last_seen", 0)):
                thread["last_seen"] = newest
                data["threads"][key] = thread
                changed = True
        if changed:
            self._store.save(data)

    # --- 内部 ---

    def _agent_account_id(self) -> int:
        """こちら（アシスタント）のアカウントID。宛先タグの突き合わせに使う。"""
        configured = int(self._config.data.get("agent_account_id") or 0)
        if configured:
            return configured
        try:
            return int(self._chatwork.get_me())
        except Exception:
            print("[warn] 自アカウントIDの取得に失敗: " + traceback.format_exc(), flush=True)
            return 0

    @staticmethod
    def _attribute(
        message: dict, threads: dict, room_id: int, agent_id: int
    ) -> str | None:
        """総務の発言が、どの依頼への返事かを決める。決められなければ None。

        備品ルームは部署全体の依頼が流れる場なので、総務の発言というだけで
        自分あての返事とみなすと、他の人あての連絡を横取りしてしまう。
        こちらのメッセージを名指ししているものだけを拾う。
        """
        body = str(message.get("body", ""))
        for target_room, target_id in _RP_RE.findall(body):
            if int(target_room) != int(room_id):
                continue
            for key, thread in threads.items():
                if target_id in [str(m) for m in thread.get("message_ids") or []]:
                    return key
        if agent_id and str(agent_id) in _TO_RE.findall(body):
            keys = list(threads)
            if len(keys) == 1:
                return keys[0]  # 進行中が1件なら、宛先タグだけでも判別できる
            print(
                "[warn] 総務からの返信を特定できませんでした"
                f"（進行中のやり取りが{len(keys)}件）",
                flush=True,
            )
        return None

    def _expire(self, data: dict, threads: dict, max_days: int) -> bool:
        """放置されたやり取りを閉じる。

        黙って消すと依頼者が待ち続けるため、閉じたことは伝える。
        """
        limit = datetime.now(JST).timestamp() - max_days * 86400
        changed = False
        for key, thread in list(threads.items()):
            if int(thread.get("ts", 0)) >= limit:
                continue
            data["threads"][key]["status"] = "expired"
            threads.pop(key)
            changed = True
            try:
                self._notify_expired(thread, max_days)
            except Exception:
                print("[warn] 期限切れの通知に失敗: " + traceback.format_exc(), flush=True)
        return changed

    def _notify_expired(self, thread: dict, max_days: int) -> None:
        from .answer import sanitize_for_chatwork

        settings = self._config.letterpack
        room_id = int(settings.get("supplies_room_id", 0))
        self._chatwork.send_message(
            int(thread["requester_room_id"]),
            mention(
                int(thread.get("requester_account_id", 0)),
                str(thread.get("requester_name") or ""),
            )
            + "\n"
            + sanitize_for_chatwork(
                f"レターパックの件、{max_days}日たっても総務からの返信を確認できませんでした。"
                "こちらでの追跡は終了します。お手数ですが、備品・消耗品購入依頼チャットを"
                "直接ご確認ください。\n"
                + room_link(room_id, thread.get("posted_message_id", ""))
            ),
        )

    def _relay(self, room_id: int, thread: dict, message: dict) -> None:
        from .answer import sanitize_for_chatwork
        from .webhook import strip_chatwork_tags

        settings = self._config.letterpack
        body = strip_chatwork_tags(str(message.get("body", "")))
        verdict = self._classify(body)
        link = room_link(room_id, message.get("message_id", ""))
        name = str(settings.get("staff_name", "総務"))

        lead = f"レターパックの件、総務の{name}さんから返信がありました。"
        if verdict["kind"] == "質問":
            note = "お手数ですが、この返信にそのままお答えください。総務へお伝えします。"
            thread["status"] = "asked"
            thread["asked_ts"] = int(datetime.now(JST).timestamp())
        elif verdict["kind"] == "完了":
            note = "手配は完了です。お礼はこちらでお伝えしました。"
            thread["status"] = "done"
        else:
            note = "続きがあればお答えください。総務へお伝えします。"
            thread["status"] = "asked"
            thread["asked_ts"] = int(datetime.now(JST).timestamp())

        to = mention(
            int(thread.get("requester_account_id", 0)), str(thread.get("requester_name") or "")
        )
        self._chatwork.send_message(
            int(thread["requester_room_id"]),
            to
            + "\n"
            + sanitize_for_chatwork(f"{lead}\n\n{verdict['summary']}\n\n{note}\n{link}"),
        )
        if verdict["kind"] == "完了":
            self._chatwork.send_message(
                room_id,
                mention(int(settings.get("staff_account_id", 0)), name)
                + "\n"
                + sanitize_for_chatwork(
                    "ご対応ありがとうございます。依頼者へ申し送りました。"
                    "引き続きよろしくお願いいたします。"
                ),
            )

    def _classify(self, body: str) -> dict[str, str]:
        """返信が「完了」か「質問」かを見る。判断できなければそのまま伝える。"""
        fallback = {"kind": "その他", "summary": body[:400], "question": ""}
        if not body.strip():
            return fallback
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        try:
            response = self._client.messages.create(
                model=self._config.model,
                max_tokens=1000,
                system=REPLY_SYSTEM,
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": REPLY_SCHEMA},
                },
                messages=[{"role": "user", "content": body}],
            )
        except Exception:
            print("[warn] 総務返信の判定に失敗: " + traceback.format_exc(), flush=True)
            return fallback
        if self._cost is not None:
            usage = {}
            for key in (
                "input_tokens",
                "output_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
            ):
                value = getattr(getattr(response, "usage", None), key, None)
                if value:
                    usage[key] = int(value)
            self._cost.add_usage(usage)
        text = next(
            (b.text for b in getattr(response, "content", []) if getattr(b, "type", "") == "text"),
            "",
        )
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return fallback
        return {
            "kind": str(parsed.get("kind") or "その他"),
            "summary": str(parsed.get("summary") or body[:400]),
            "question": str(parsed.get("question") or ""),
        }
