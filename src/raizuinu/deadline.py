"""期日の進捗確認（経理財務部業務共有チャットだけ）。

「9/12までに決算資料の修正、篠田さんの進捗を見ておいて」のように頼まれたら期日を
控え、3営業日前・前日・当日の朝に担当者へ進捗を尋ねる。「完了」の返事で控えから
外し、依頼者に知らせる。期日を過ぎても完了の返事が無ければ、翌営業日以降も数日は
朝に尋ねる。

方針:
- 使うのは設定した1ルーム（`deadline.room_id`）だけ。ほかのルームでは何もしない
- 期日・担当・件名の読み取りはモデルに任せるが、日付の計算と投稿文はコードで作る
  （営業日の数え方を作文させない）
- 営業日は平日で祝日を除く（レターパックの催促と同じ数え方。FAXの規則とは別）
- 「完了」「延期」「取り消し」は、進捗確認への返信でも、件名を挙げた一言でもよい。
  どの期日の話かはモデルが控えの一覧から選ぶ
"""

from __future__ import annotations

import json
import re
import traceback
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable

JST = timezone(timedelta(hours=9))
WEEKDAYS = "月火水木金土日"

_CUE_RE = re.compile(r"(期日|締切|締め切り|〆切|デッドライン|期限|までに)")
_DATE_RE = re.compile(
    r"(\d{1,2}\s*[/月]\s*\d{1,2}|今日|本日|明日|明後日|今週|来週|再来週|月末|月初|[月火水木金土日]曜|\d{1,2}日)"
)
_INTENT_RE = re.compile(
    r"(進捗|リマインド|フォロー|追って|見ておいて|見といて|控えて|登録|管理|一覧|確認して|確認を|お願い|伺って|聞いて)"
)
_LIST_RE = re.compile(r"(一覧|どれ|何が|ある[？?]|あります|教えて|残って)")


def looks_like_deadline_message(question: str) -> bool:
    """期日の登録・照会の頼み方か（進捗確認への返信は別に見る）。

    「有効期限は？」「締め日は？」のような知識の質問を巻き込まないよう、
    期日の語だけでなく、日付か意図の語も要る。
    """
    text = str(question or "")
    if not _CUE_RE.search(text):
        return bool(re.search(r"進捗", text) and _DATE_RE.search(text))
    if _DATE_RE.search(text) and _INTENT_RE.search(text):
        return True
    return bool(_INTENT_RE.search(text) and _LIST_RE.search(text)) or bool(re.search(r"進捗", text))


def jp_date(day: date) -> str:
    return f"{day.month}月{day.day}日（{WEEKDAYS[day.weekday()]}）"


def is_office_day(day: date, holidays: set[str]) -> bool:
    return day.weekday() < 5 and day.isoformat() not in holidays


def office_days_between(start: date, end: date, holidays: set[str]) -> int:
    """start の翌日から end までの営業日数（end が start 以前なら負）。"""
    if end == start:
        return 0
    sign = 1 if end > start else -1
    lo, hi = (start, end) if end > start else (end, start)
    day, count = lo + timedelta(days=1), 0
    while day <= hi:
        if is_office_day(day, holidays):
            count += 1
        day += timedelta(days=1)
    return sign * count


def parse_clock(text: Any, default: tuple[int, int]) -> tuple[int, int]:
    try:
        hour, minute = str(text).split(":")
        return int(hour), int(minute)
    except (ValueError, AttributeError):
        return default


# --- 控え ---


class DeadlineStore:
    def __init__(self, path: Any) -> None:
        self._path = path

    def load(self) -> dict:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data.setdefault("items", [])
        data.setdefault("next_id", 1)
        return data

    def save(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def open_items(data: dict) -> list[dict]:
    return sorted(
        (i for i in data.get("items", []) if i.get("status") == "open"),
        key=lambda i: (str(i.get("due", "")), int(i.get("id", 0))),
    )


def item_line(item: dict, today: date | None = None, holidays: set[str] | None = None) -> str:
    """控え1件の表示。「9月12日（金）まで　決算資料の修正（担当: 篠田さん・あと3営業日）」"""
    due = _parse_date(item.get("due"))
    when = jp_date(due) if due else str(item.get("due", ""))
    if item.get("due_time"):
        when += f" {item['due_time']}"
    who = "・".join(a.get("name", "") for a in item.get("assignees") or []) or "担当未定"
    tail = ""
    if today is not None and due is not None:
        left = office_days_between(today, due, holidays or set())
        if left > 0:
            tail = f"・あと{left}営業日"
        elif left == 0:
            tail = "・今日が期日"
        else:
            tail = f"・期日を{-left}営業日過ぎています"
    return f"{when}まで　{item.get('title', '')}（担当: {who}さん{tail}）"


# --- 読み取り ---

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["register", "done", "progress", "postpone", "cancel", "list", "other"],
            "description": (
                "register=期日を新しく控える / done=済んだ報告 / progress=途中経過の報告 / "
                "postpone=期日の変更 / cancel=取り消し / list=控えの一覧を尋ねている / other=どれでもない"
            ),
        },
        "title": {"type": "string", "description": "register のときの件名。依頼文の言葉をそのまま短く。例: 決算資料の修正"},
        "due_date": {"type": "string", "description": "register/postpone のときの期日 YYYY-MM-DD。分からなければ空文字"},
        "due_time": {"type": "string", "description": "期日に時刻があれば HH:MM。無ければ空文字"},
        "assignees": {
            "type": "array",
            "items": {"type": "string"},
            "description": "register のときの担当者の苗字（一覧にある人だけ）。書かれていなければ空配列（依頼者本人が担当）",
        },
        "target_id": {"type": "integer", "description": "done/progress/postpone/cancel が指す控えのID。特定できなければ 0"},
        "note": {"type": "string", "description": "progress のときの進捗の要点（相手の言葉で1文）。無ければ空文字"},
        "reply": {"type": "string", "description": "相手への一言（1〜2文、です・ます）。事実（日付・件数）は書かない。プログラム側が添える"},
    },
    "required": ["action", "title", "due_date", "due_time", "assignees", "target_id", "note", "reply"],
    "additionalProperties": False,
}

SYSTEM = """あなたは株式会社ライズクリエイション経理財務部のアシスタント「{agent_name}」です。
部のルームで、期日（資料の修正期限やアポの期日など）の控えと進捗確認を任されています。
相手のメッセージを読み、どの操作かと必要な項目を取り出してください。

厳守すること:
- 依頼文に書かれていることだけを使う。期日・担当を推測で作らない
- 「来週金曜」「月末」は today と曜日を基準に YYYY-MM-DD へ直す。「9/12」は today 以降で最も近いその日
- 担当者は次の一覧の苗字で書く: {members}。書かれていなければ空配列
- done/progress/postpone/cancel は、下の控えの一覧から該当する ID を target_id に入れる。
  相手が返信している控え（reply_to）があればそれを優先する。特定できなければ 0
- reply には日付・営業日数・件数を書かない（プログラム側が正確に添える）。
  同じ言い回しを続けない。「お待たせしました」のような、待たせていないのに詫びる言い方はしない
- 「完了」「終わりました」「対応済み」は done。「半分まで」「明日には」「進めています」は progress

いま控えている期日:
{items}
"""


class DeadlineRunner:
    """チャットでの登録・報告・照会。"""

    def __init__(
        self,
        config: Any,
        chatwork: Any,
        client: Any | None = None,
        now: Callable[[], datetime] | None = None,
        holidays: set[str] | None = None,
        store: DeadlineStore | None = None,
    ) -> None:
        self._config = config
        self._chatwork = chatwork
        self._client = client
        self._now = now or (lambda: datetime.now(JST))
        if holidays is None:
            from .letterpack import load_holidays

            holidays, _ = load_holidays(config)
        self._holidays = set(holidays)
        self._store = store or DeadlineStore(config.resolve_path(config.state_dir) / "deadlines.json")

    # --- 公開API ---

    def owns(self, room_id: int) -> bool:
        settings = self._config.deadline
        return bool(settings.get("enabled")) and int(room_id) == int(settings.get("room_id", 0) or 0)

    def is_reply_to_ours(self, reply_target: str) -> dict | None:
        """相手が返信しているのが、こちらの期日の投稿なら、その控え。"""
        if not reply_target:
            return None
        for item in self._store.load().get("items", []):
            if str(reply_target) in [str(m) for m in item.get("message_ids") or []]:
                return item
        return None

    def looks_related(self, question: str, reply_target: str = "") -> bool:
        return self.is_reply_to_ours(reply_target) is not None or looks_like_deadline_message(question)

    def remember_message(self, meta: dict, message_id: Any) -> None:
        """こちらの投稿（登録の確認・返事）のIDを控えに結びつける（返信の紐付け用）。"""
        item_id = meta.get("item_id")
        if not item_id or not message_id:
            return
        data = self._store.load()
        for item in data.get("items", []):
            if int(item.get("id", 0)) == int(item_id):
                item.setdefault("message_ids", []).append(str(message_id))
                break
        self._store.save(data)

    def handle(
        self, room_id: int, account_id: int, question: str, reply_target: str = "", display_name: str = ""
    ) -> tuple[str, dict, dict] | None:
        """→ (返信, meta, usage)。この機能の話でなければ None。"""
        data = self._store.load()
        items = open_items(data)
        replied = self.is_reply_to_ours(reply_target)
        fields, usage = self._extract(question, items, replied)
        action = str(fields.get("action") or "other")
        today = self._now().date()

        if action == "list" or (action == "other" and _LIST_RE.search(question) and _CUE_RE.search(question)):
            return self._list_reply(items, today), {"action": "list", "count": len(items)}, usage

        if action == "register":
            return self._register(data, fields, room_id, account_id, display_name, question, usage)

        target = self._target(items, fields, replied)
        if action in ("done", "progress", "postpone", "cancel") and target is None:
            if not items:
                return "いま控えている期日はありません。", {"action": action, "error": "nothing"}, usage
            lines = ["どの期日の話か分からなかったので、番号か件名でお知らせください。"]
            lines += [f"{i.get('id')}. {item_line(i, today, self._holidays)}" for i in items]
            return "\n".join(lines), {"action": action, "error": "no_target"}, usage

        if action == "done":
            return self._done(data, target, account_id, fields, usage)
        if action == "progress":
            return self._progress(data, target, account_id, fields, usage)
        if action == "postpone":
            return self._postpone(data, target, fields, usage)
        if action == "cancel":
            return self._cancel(data, target, fields, usage)
        if replied is not None:
            reply = str(fields.get("reply") or "").strip() or "承知しました。"
            return reply, {"action": "other", "target_id": replied.get("id")}, usage
        return None

    # --- 操作 ---

    def _register(self, data, fields, room_id, account_id, display_name, question, usage):
        title = str(fields.get("title") or "").strip()
        due = _parse_date(fields.get("due_date"))
        missing = [m for m, ok in (("期日", due is not None), ("件名", bool(title))) if not ok]
        if missing:
            return (
                "期日の控えですね。これだけ教えていただけますか。\n" + "\n".join(f"・{m}" for m in missing),
                {"action": "register", "error": "missing", "missing": missing},
                usage,
            )
        from .scheduleplan import find_members, load_members, member_by_account

        members = load_members(self._config)
        named = find_members(question + " " + " ".join(str(a) for a in fields.get("assignees") or []), members)
        assignees = [{"account_id": m.account_id, "name": m.name} for m in named if m.account_id]
        if not assignees:
            me = member_by_account(account_id, members)
            assignees = [{"account_id": int(account_id), "name": (me.name if me else (display_name or "依頼者"))}]

        today = self._now().date()
        item = {
            "id": int(data.get("next_id", 1)),
            "title": title,
            "due": due.isoformat(),
            "due_time": str(fields.get("due_time") or "").strip(),
            "assignees": assignees,
            "requester_id": int(account_id),
            "room_id": int(room_id),
            "created_at": self._now().isoformat(),
            "status": "open",
            "message_ids": [],
            "checks": {},
            "progress": [],
        }
        left = office_days_between(today, due, self._holidays)
        if left <= 0:
            item["checks"]["0"] = self._now().isoformat()  # 今日以降が期日なら、今から尋ね直さない
        data["next_id"] = item["id"] + 1
        data["items"].append(item)
        self._store.save(data)

        plan = self._plan_label(left)
        who = "・".join(a["name"] for a in assignees) + "さん"
        lines = ["承知しました。次の期日を控えました。", f"・{item_line(item, today, self._holidays)}"]
        if plan:
            lines.append(f"{plan}に、{who}へ進捗を伺います。済んだら「完了」と返信してください。")
        else:
            lines.append(f"期日を過ぎるまで完了の返事が無ければ、翌営業日の朝に{who}へ伺います。済んだら「完了」と返信してください。")
        return "\n".join(lines), {"action": "register", "item_id": item["id"], "due": item["due"]}, usage

    def _done(self, data, target, account_id, fields, usage):
        target["status"] = "done"
        target["done_at"] = self._now().isoformat()
        target["done_by"] = int(account_id)
        self._store.save(data)
        reply = str(fields.get("reply") or "").strip() or "お疲れさまでした。"
        lines = [reply, f"「{target.get('title')}」を控えから外しました。"]
        requester = int(target.get("requester_id", 0) or 0)
        if requester and requester != int(account_id):
            who = next((a.get("name") for a in target.get("assignees") or [] if int(a.get("account_id", 0)) == int(account_id)), "")
            lines.insert(0, f"[To:{requester}]")
            lines.append(f"{who + 'さん' if who else '担当の方'}から完了の報告がありました。")
        return "\n".join(lines), {"action": "done", "item_id": target.get("id")}, usage

    def _progress(self, data, target, account_id, fields, usage):
        note = str(fields.get("note") or "").strip()
        target.setdefault("progress", []).append({"ts": self._now().isoformat(), "by": int(account_id), "note": note})
        self._store.save(data)
        reply = str(fields.get("reply") or "").strip() or "承知しました。"
        today = self._now().date()
        due = _parse_date(target.get("due"))
        nxt = self._next_check_label(target, today, due)
        lines = [reply]
        if nxt:
            lines.append(nxt)
        return "\n".join(lines), {"action": "progress", "item_id": target.get("id"), "note": note}, usage

    def _postpone(self, data, target, fields, usage):
        due = _parse_date(fields.get("due_date"))
        if due is None:
            return (
                f"「{target.get('title')}」の新しい期日を教えていただけますか。",
                {"action": "postpone", "item_id": target.get("id"), "error": "missing"},
                usage,
            )
        old = target.get("due")
        target["due"] = due.isoformat()
        if fields.get("due_time"):
            target["due_time"] = str(fields["due_time"]).strip()
        target["checks"] = {}
        today = self._now().date()
        left = office_days_between(today, due, self._holidays)
        if left <= 0:
            target["checks"]["0"] = self._now().isoformat()
        self._store.save(data)
        reply = str(fields.get("reply") or "").strip() or "承知しました。"
        old_label = jp_date(_parse_date(old)) if _parse_date(old) else str(old)
        lines = [reply, f"「{target.get('title')}」の期日を{old_label}から{jp_date(due)}に変えました。"]
        plan = self._plan_label(left)
        if plan:
            lines.append(f"{plan}に、改めて進捗を伺います。")
        return "\n".join(lines), {"action": "postpone", "item_id": target.get("id"), "due": target["due"]}, usage

    def _cancel(self, data, target, fields, usage):
        target["status"] = "cancelled"
        target["cancelled_at"] = self._now().isoformat()
        self._store.save(data)
        reply = str(fields.get("reply") or "").strip() or "承知しました。"
        return (
            f"{reply}\n「{target.get('title')}」の控えを取り消しました。",
            {"action": "cancel", "item_id": target.get("id")},
            usage,
        )

    def _list_reply(self, items, today):
        if not items:
            return "いま控えている期日はありません。"
        lines = [f"控えている期日は{len(items)}件です。"]
        lines += [f"{i.get('id')}. {item_line(i, today, self._holidays)}" for i in items]
        return "\n".join(lines)

    # --- 部品 ---

    def _target(self, items, fields, replied):
        wanted = int(fields.get("target_id") or 0)
        if wanted:
            for item in items:
                if int(item.get("id", 0)) == wanted:
                    return item
        if replied is not None and replied.get("status") == "open":
            for item in items:
                if int(item.get("id", 0)) == int(replied.get("id", 0)):
                    return item
        if len(items) == 1:
            return items[0]
        return None

    def _plan_label(self, left: int) -> str:
        """「3営業日前・前日・当日の朝」のうち、これから来るもの。"""
        offsets = sorted({int(n) for n in (self._config.deadline.get("check_days_before") or [3, 1, 0])}, reverse=True)
        coming = [n for n in offsets if n < left] if left > 0 else []
        if not coming:
            return ""
        words = {1: "前日", 0: "当日"}
        return "・".join(words.get(n, f"{n}営業日前") for n in coming) + "の朝"

    def _next_check_label(self, target: dict, today: date, due: date | None) -> str:
        if due is None:
            return ""
        left = office_days_between(today, due, self._holidays)
        offsets = sorted({int(n) for n in (self._config.deadline.get("check_days_before") or [3, 1, 0])}, reverse=True)
        sent = set(str(k) for k in (target.get("checks") or {}))
        coming = [n for n in offsets if n < left and str(n) not in sent] if left > 0 else []
        if coming:
            words = {1: "前日", 0: "当日"}
            return f"次は{words.get(coming[0], f'{coming[0]}営業日前')}の朝に伺います。引き続きよろしくお願いします。"
        return "引き続きよろしくお願いします。"

    def _extract(self, question: str, items: list[dict], replied: dict | None) -> tuple[dict, dict]:
        from .answer import _call_with_continuation
        from .scheduleplan import load_members

        try:
            if self._client is None:
                import anthropic

                self._client = anthropic.Anthropic()
            cfg = self._config
            now = self._now()
            listing = "\n".join(
                f"- ID {i.get('id')}: {i.get('title')} / 期日 {i.get('due')} / 担当 "
                + "・".join(a.get("name", "") for a in i.get("assignees") or [])
                for i in items
            ) or "（なし）"
            kwargs = {
                "model": cfg.model,
                "max_tokens": int(cfg.max_tokens),
                "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": _SCHEMA}},
                "system": SYSTEM.format(
                    agent_name=cfg.agent_name,
                    members="、".join(m.name for m in load_members(cfg)) or "（未設定）",
                    items=listing,
                ),
            }
            prompt = f"today: {now:%Y-%m-%d}（{WEEKDAYS[now.weekday()]}曜日）\n"
            if replied is not None:
                prompt += f"reply_to: ID {replied.get('id')}（{replied.get('title')}）への返信\n"
            prompt += f"\n===相手のメッセージ===\n{question}\n==="
            response, usage, _ = _call_with_continuation(
                self._client.messages.create, kwargs, [{"role": "user", "content": prompt}]
            )
            text = next(
                (b.text for b in getattr(response, "content", []) if getattr(b, "type", "") == "text"), ""
            )
            return json.loads(text), usage
        except Exception:
            print("[warn] 期日の読み取りに失敗: " + traceback.format_exc(), flush=True)
            return {"action": "other", "reply": ""}, {}


class DeadlineFollower:
    """朝の進捗確認（巡回のタイマーに相乗り）。"""

    def __init__(
        self,
        config: Any,
        chatwork: Any,
        now: Callable[[], datetime] | None = None,
        holidays: set[str] | None = None,
        store: DeadlineStore | None = None,
    ) -> None:
        self._config = config
        self._chatwork = chatwork
        self._now = now or (lambda: datetime.now(JST))
        if holidays is None:
            from .letterpack import load_holidays

            holidays, _ = load_holidays(config)
        self._holidays = set(holidays)
        self._store = store or DeadlineStore(config.resolve_path(config.state_dir) / "deadlines.json")

    def run_once(self) -> int:
        """→ 投稿した確認の数。"""
        settings = self._config.deadline
        if not settings.get("enabled") or not settings.get("room_id"):
            return 0
        now = self._now()
        today = now.date()
        if not is_office_day(today, self._holidays):
            return 0
        if now < datetime.combine(today, time(*parse_clock(settings.get("check_time"), (9, 0))), tzinfo=JST):
            return 0
        offsets = {int(n) for n in (settings.get("check_days_before") or [3, 1, 0])}
        overdue_days = int(settings.get("overdue_days", 3))
        data = self._store.load()
        posted = 0
        for item in open_items(data):
            due = _parse_date(item.get("due"))
            if due is None:
                continue
            left = office_days_between(today, due, self._holidays)
            checks = item.setdefault("checks", {})
            key = None
            if left > 0 and left in offsets:
                key = str(left)
            elif left == 0 or (left < 0 and "0" not in checks):
                key = "0"  # 当日（期日が休日なら、その後の最初の営業日）
            elif left < 0 and -left <= overdue_days:
                key = f"overdue:{today.isoformat()}"
            if key is None or key in checks:
                continue
            text = self._check_text(item, left, due, today)
            try:
                mid = self._chatwork.send_message(int(settings["room_id"]), text)
            except Exception:
                print("[warn] 期日の進捗確認の投稿に失敗: " + traceback.format_exc(), flush=True)
                continue
            checks[key] = now.isoformat()
            item.setdefault("message_ids", []).append(str(mid or ""))
            posted += 1
            self._audit({"type": "deadline_check", "item_id": item.get("id"), "title": item.get("title"), "key": key})
        # 済んだものは remember_days 経ったら控えから消す（際限なく育たないように）
        keep = int(settings.get("remember_days", 60))
        data["items"] = [
            i for i in data.get("items", [])
            if i.get("status") == "open" or (now - (_parse_iso(i.get("done_at") or i.get("cancelled_at")) or now)).days < keep
        ]
        self._store.save(data)
        return posted

    def _check_text(self, item: dict, left: int, due: date, today: date) -> str:
        heads = " ".join(f"[To:{int(a.get('account_id', 0))}]" for a in item.get("assignees") or [] if a.get("account_id"))
        title = item.get("title", "")
        when = jp_date(due) + (f" {item['due_time']}" if item.get("due_time") else "")
        if left > 0:
            body = f"おはようございます。「{title}」の期日が{when}で、あと{left}営業日です。進捗はいかがでしょうか。"
        elif left == 0 and today == due:
            body = f"おはようございます。「{title}」は今日（{when}）が期日です。状況はいかがでしょうか。"
        elif left == 0:
            # 期日が土日祝のとき、その前の最後の営業日
            body = f"おはようございます。「{title}」の期日は{when}で、営業日は今日が最後です。状況はいかがでしょうか。"
        else:
            body = f"おはようございます。「{title}」の期日（{when}）を過ぎています。状況をお知らせください。"
        tail = "済んでいましたら、このメッセージへの返信で「完了」とお知らせください。途中でしたら、いまの状況を一言いただければ控えておきます。"
        return f"{heads}\n{body}\n{tail}"

    def _audit(self, record: dict) -> None:
        try:
            from .audit import AuditLogger

            cfg = self._config
            AuditLogger(log_dir=cfg.resolve_path(cfg.audit_log_dir), retention_days=cfg.audit_log_retention_days).log(record)
        except Exception:
            print("[warn] 監査ログの記録に失敗: " + traceback.format_exc(), flush=True)


def _parse_date(text: Any) -> date | None:
    try:
        return date.fromisoformat(str(text).strip())
    except (TypeError, ValueError):
        return None


def _parse_iso(text: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=JST)
