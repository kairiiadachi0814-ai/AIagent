"""AnthropicのAPIキーが使えなくなったら、管理者ルームへ一度だけ知らせる。

実例（2026-11-16）: 有効期限つきのキーが週末に切れ、月曜 8:30 から FAX の読み取りが全件
「読み取れませんでした」になった。質問への回答なども止まっていたが、止まっていることを
どこにも知らせておらず、管理者がFAXルームを見るまで気づけなかった。

5分ごとの巡回（watcher）に相乗りし、check_minutes おきにモデル一覧のAPI（料金はかからない）
でキーを確かめる。キーの問題と言えるのは認証・権限のエラー（401／403）だけで、通信の失敗や
混雑では知らせない。知らせるのは、使えなくなったときの1回と、通るようになったときの1回。
キーの値は扱わない（SDKが環境変数から読む。投稿にも状態ファイルにも書かない）。
"""

from __future__ import annotations

import json
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

JST = timezone(timedelta(hours=9))

OK, INVALID, UNKNOWN, SKIPPED = "ok", "invalid", "unknown", "skipped"

REASON_MISSING = "キーが設定されていません"
REASON_REJECTED = "認証エラー 401。キーの削除・無効化か、有効期限切れです"
REASON_FORBIDDEN = "権限エラー 403。キーに必要な権限がありません"


class KeyAlert:
    """APIキーを確かめ、使えなくなったとき・戻ったときに管理者ルームへ知らせる。"""

    def __init__(
        self,
        config: Any,
        chatwork: Any,
        client: Any | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._chatwork = chatwork
        self._client = client
        self._now = now or (lambda: datetime.now(JST))
        self._path = config.resolve_path(config.state_dir) / "key_alert.json"

    def run_once(self) -> str:
        """キーを確かめる。→ ok／invalid／unknown（確かめられなかった）／skipped（今回は見ていない）"""
        settings = self._config.data.get("key_alert") or {}
        if not settings.get("enabled", True):
            return SKIPPED
        state = self._load()
        now = self._now()
        if not self._due(state, now, int(settings.get("check_minutes", 15))):
            return SKIPPED
        status, reason = self._check()
        if status == UNKNOWN:
            return UNKNOWN  # 通信の失敗など。状態は変えず、次の巡回でまた見る
        state["checked_at"] = now.isoformat()
        if status == INVALID:
            state.setdefault("invalid_since", now.isoformat())
            if not state.get("alerted_at") and self._post(self._invalid_text(reason)):
                state["alerted_at"] = now.isoformat()  # 投稿できなかったら立てない（次の巡回でもう一度）
        else:
            if state.get("alerted_at") and not self._post(self._recovered_text(state)):
                self._save(state)
                return OK  # 戻った知らせを出せなかった。控えは残して次の巡回でもう一度
            state.pop("invalid_since", None)
            state.pop("alerted_at", None)
        self._save(state)
        return status

    # --- 確かめる ---

    @staticmethod
    def _due(state: dict, now: datetime, check_minutes: int) -> bool:
        """止まっている間は毎回見る（戻ったらすぐ分かるように）。普段は check_minutes おき。"""
        if state.get("invalid_since"):
            return True
        checked = _parse(state.get("checked_at"))
        return checked is None or now - checked >= timedelta(minutes=check_minutes)

    def _check(self) -> tuple[str, str]:
        if not (self._config.anthropic_api_key or "").strip():
            return INVALID, REASON_MISSING
        import anthropic

        try:
            if self._client is None:
                self._client = anthropic.Anthropic()
            self._client.models.list(limit=1)
        except anthropic.AuthenticationError:
            return INVALID, REASON_REJECTED
        except anthropic.PermissionDeniedError:
            return INVALID, REASON_FORBIDDEN
        except anthropic.APIError as exc:
            # 混雑・サーバー側の障害・通信の失敗はキーの問題ではない
            print(f"[warn] APIキーの確認ができませんでした: {type(exc).__name__}", flush=True)
            return UNKNOWN, ""
        return OK, ""

    # --- 知らせる ---

    def _heads(self) -> str:
        return " ".join(f"[To:{int(a)}]" for a in self._config.data.get("admin_account_ids") or [])

    def _invalid_text(self, reason: str) -> str:
        return "\n".join(
            line for line in (
                self._heads(),
                f"【要対応】AnthropicのAPIキーが使えなくなっています（{reason}）。",
                "この間、質問への回答・FAXの読み取りなど、AIを使う機能は止まります"
                "（FAXは「読み取れませんでした」という通知だけが出ます）。",
                "Consoleで新しいキーを発行し、VPSの設定（ANTHROPIC_API_KEY）を書き換えてください。"
                "キーの値はチャットに貼らないでください。",
                "この知らせは1回だけです。キーが通るようになったら、もう一度だけお知らせします。",
            ) if line
        )

    def _recovered_text(self, state: dict) -> str:
        since = _parse(state.get("invalid_since"))
        span = f"（{since.month}/{since.day} {since:%H:%M} から止まっていました）" if since else ""
        return "\n".join(
            line for line in (
                self._heads(),
                f"AnthropicのAPIキーが通るようになりました{span}。",
                "止まっていた間に「読み取れませんでした」となったFAXは、自動では読み直しません。",
            ) if line
        )

    def _post(self, text: str) -> bool:
        admin_room = int(self._config.admin_room_id or 0)
        if not admin_room:
            print("[warn] admin_room_id未設定のためAPIキーの知らせを送信できません", flush=True)
            return False
        try:
            self._chatwork.send_message(admin_room, text)
            return True
        except Exception:
            print("[warn] APIキーの知らせの投稿に失敗: " + traceback.format_exc(), flush=True)
            return False

    # --- 状態 ---

    def _load(self) -> dict:
        try:
            state = json.loads(self._path.read_text(encoding="utf-8"))
            return state if isinstance(state, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, state: dict) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        except OSError:
            print("[warn] APIキーの確認の控えを保存できません: " + traceback.format_exc(), flush=True)


def _parse(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
