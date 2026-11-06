"""朝の予定通知（systemd: raizuinu-morning.timer で毎朝1回）。

設定した時刻に、その日1日の予定をまとめて部署ルームへ投稿する。
宛先は [To:] で管理者を指すだけで、内容は同じルームの全員に見える
（メンバーが予定を把握できるようにするのが目的のため）。

- 同じ日に二重投稿しないよう、投稿済みの日付を状態ファイルに残す
- カレンダーを1つ読めなくても、取れたぶんは投稿する（無言で欠けさせない）
- 土日は既定で投稿しない（設定で変えられる）

実行: python -m raizuinu.morning
"""

from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from .chatwork import ChatworkClient
from .config import Config
from .schedule import JST, build_sources, collect, day_range, format_day


class MorningNotifier:
    def __init__(
        self,
        config: Config | None = None,
        chatwork: Any | None = None,
        sources: list[Any] | None = None,
    ) -> None:
        self._config = config or Config.load()
        self._chatwork = chatwork or ChatworkClient(self._config.chatwork_api_token or "")
        self._sources = sources

    def run_once(self, today: Any = None) -> bool:
        """投稿したら True。対象外・投稿済みなら False。"""
        cfg = self._config.schedule
        if not cfg.get("enabled"):
            return False
        room_id = cfg.get("notify_room_id")
        if not room_id:
            return False

        target = today or datetime.now(JST).date()
        if cfg.get("notify_weekdays_only", True) and target.weekday() >= 5:
            return False

        state = self._load_state()
        if state.get("last_posted") == target.isoformat():
            return False  # 同じ日に二度出さない

        sources = self._sources if self._sources is not None else build_sources(self._config)
        if not sources:
            print("[warn] 予定の取得先が設定されていません", flush=True)
            return False

        start, end = day_range(target)
        schedule = collect(sources, start, end)
        owner = str(cfg.get("owner_name", "")) or "担当者"
        body = format_day(
            schedule,
            target,
            owner,
            mark_source=str(cfg.get("unsynced_source", "")),
            mark_note=str(cfg.get("unsynced_note", "")),
            greeting=str(cfg.get("greeting", "")),
        )
        account_id = cfg.get("notify_account_id")
        if account_id:
            body = f"[To:{int(account_id)}]\n" + body

        self._chatwork.send_message(int(room_id), body)
        state["last_posted"] = target.isoformat()
        self._save_state(state)
        return True

    # --- 状態（同じ日の二重投稿を防ぐだけ） ---

    def _state_path(self) -> Path:
        return self._config.resolve_path(self._config.state_dir) / "morning.json"

    def _load_state(self) -> dict[str, Any]:
        path = self._state_path()
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_state(self, state: dict[str, Any]) -> None:
        path = self._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    try:
        posted = MorningNotifier().run_once()
    except Exception:
        print("[error] 朝の予定通知に失敗: " + traceback.format_exc(), flush=True)
        return 1
    print("[morning] 投稿しました" if posted else "[morning] 対象外（投稿なし）", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
