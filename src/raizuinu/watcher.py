"""議論ウォッチャー — ルーム内の議論を巡回し、明らかな誤りに助言を出す。

設計方針:
- 5分ごとのタイマー実行（systemd: raizuinu-watcher.timer）。発言単位ではなく
  バッチで評価することでコストを抑える
- 2段階判定: 1段目は軽量（ハンドブックなし）で「誤りの疑い」だけを拾い、
  疑いがある場合のみ2段目でハンドブック・法令に照らして厳密に裏取りする
- 沈黙がデフォルト: 裏取りできた「明らかな誤り」以外には一切口を挟まない。
  意見の相違・雑談・判断が分かれる話題は対象外
- 見習いモード（mode=shadow）: ルームには投稿せず管理者ルームへ内報のみ。
  精度確認後に mode=live へ切替（config変更のみ）
- プライバシー: 発言の恒久保存はしない。監査ログに残すのは介入時のみ
- 初回実行はその時点までの発言を「既読」にするだけで評価しない（過去分を裁かない）

実行: python -m raizuinu.watcher
"""

from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .answer import AnswerGenerator, sanitize_for_chatwork
from .chatwork import ChatworkClient
from .config import Config
from .cost import CostTracker
from .handbook import HandbookLoader
from .webhook import strip_chatwork_tags

JST = timezone(timedelta(hours=9))

SCREEN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "suspicious": {
            "type": "boolean",
            "description": "誤りの疑いがある事実主張が含まれるか",
        },
        "reason": {"type": "string", "description": "疑いの内容（1文）。無ければ空文字"},
    },
    "required": ["suspicious", "reason"],
    "additionalProperties": False,
}

SCREEN_SYSTEM = (
    "あなたは社内チャットの発言群から、業務手順・会計・税務・法令に関する"
    "「事実の主張」で誤りの疑いがあるものを検出する担当です。"
    "意見の相違・雑談・予定調整・判断が分かれる話題・冗談は対象外。"
    "誤りの疑いのある事実主張（例: 期限・金額・手順・法令の内容についての断定）が"
    "1つでもあれば suspicious=true（次の工程で厳密に裏取りされるため、確信は不要）。"
    "対象となる主張が無ければ suspicious=false とすること。"
)

VERIFY_PROMPT = """以下は社内チャットルームでのメンバー同士の議論です（あなたへの質問ではありません）。
この中に、ハンドブック・マニュアルリンク集・法令に照らして「明らかな誤り」と裏取りできる事実の主張が含まれるかを検証してください。

- 明らかな誤りが裏取りできた場合のみ: has_answer=true とし、answerには「横から失礼します。」から始まる丁寧で控えめな助言文を書くこと（1〜4文。何の話題についてかが分かるように書き、根拠を添える。断定的な訂正口調にしない）。sourcesに根拠を挙げること。
- 助言文では、通常回答用の「受け止めの一言」やくだけた言い回し（「ちなみに」「ざっくり言うと」等）を使わないこと。議論中のメンバーがすでに使っている用語に説明を付け足さず、終始控えめで落ち着いた敬体にすること。
- 意見の相違・判断が分かれる話題・雑談・裏取りできない内容・些細な言い間違いの場合: has_answer=false とすること（沈黙が正しい対応です）。
- 迷ったら必ず has_answer=false。

議論（発言順）:
{batch}"""


class DiscussionWatcher:
    def __init__(
        self,
        config: Config | None = None,
        chatwork: Any | None = None,
        generator: Any | None = None,
        screen_client: Any | None = None,
        cost: Any | None = None,
        handbook_loader: Any | None = None,
    ) -> None:
        self._config = config or Config.load()
        cfg = self._config
        self._chatwork = chatwork or ChatworkClient(cfg.chatwork_api_token or "")
        self._generator = generator or AnswerGenerator(cfg)
        if screen_client is None:
            import anthropic

            screen_client = anthropic.Anthropic()
        self._screen_client = screen_client
        self._cost = cost or CostTracker(
            state_dir=cfg.resolve_path(cfg.state_dir),
            limit_jpy=cfg.monthly_cost_limit_jpy,
            alert_threshold=cfg.cost_alert_threshold,
            usd_jpy_rate=cfg.usd_jpy_rate,
            pricing_usd_per_mtok=cfg.pricing_usd_per_mtok,
            prompt_cache_ttl=cfg.prompt_cache_ttl,
        )
        self._handbook_loader = handbook_loader or HandbookLoader(
            roots=[cfg.resolve_path(r) for r in cfg.handbook["roots"]],
            include=cfg.handbook["include"],
            exclude=cfg.handbook["exclude"],
            cache_ttl_seconds=cfg.handbook_cache_ttl_seconds,
        )

    # --- 公開API ---

    def run_once(self) -> None:
        watch = self._config.discussion_watch
        if not watch.get("enabled"):
            return
        for room_id in watch.get("room_ids") or []:
            try:
                self._watch_room(int(room_id))
            except Exception:
                print(f"[error] ルーム{room_id}の巡回に失敗: " + traceback.format_exc(), flush=True)

    # --- 内部 ---

    def _watch_room(self, room_id: int) -> None:
        watch = self._config.discussion_watch
        state = self._load_state(room_id)
        me = self._chatwork.get_me()
        messages = self._chatwork.get_recent_messages(
            room_id, limit=int(watch.get("max_batch_messages", 30))
        )
        if not messages:
            return

        newest_id = max(int(m["message_id"]) for m in messages)
        last_seen = state.get("last_seen")
        # 既読位置は評価前に進める（多重投稿より取りこぼしを選ぶ）
        state["last_seen"] = newest_id
        self._save_state(room_id, state)

        if last_seen is None:
            return  # 初回は既読化のみ。過去の議論は裁かない

        fresh = self._filter_messages(messages, last_seen, me)
        if not fresh:
            return

        today = datetime.now(JST).strftime("%Y%m%d")
        if state.get("date") != today:
            state["date"] = today
            state["count"] = 0
            state["cited_today"] = []
        if state["count"] >= int(watch.get("max_interventions_per_day", 3)):
            return
        if self._cost.status().over_limit:
            return

        batch = "\n".join(
            f"{m.get('account', {}).get('name', '不明')}: {strip_chatwork_tags(m.get('body', ''))}"
            for m in fresh
        )

        # 1段目: 軽量スクリーニング（ハンドブックなし）
        screen_usage: dict[str, int] = {}
        if not self._screen(batch, screen_usage):
            # 介入しない場合もAPIは消費しているため必ず記録する（費用の可視化）
            self._audit_usage(room_id, "screen_only", screen_usage)
            return

        # 2段目: ハンドブック・法令で裏取り（Q&Aと同じ検証パイプラインを再利用）
        handbook = self._handbook_loader.load()
        answer = self._generator.generate(VERIFY_PROMPT.format(batch=batch), handbook)
        self._cost.add_usage(answer.usage)
        total_usage = dict(screen_usage)
        for key, value in (answer.usage or {}).items():
            total_usage[key] = total_usage.get(key, 0) + value
        if not answer.has_answer or not (answer.sources or answer.intent == "legal_knowledge"):
            self._audit_usage(room_id, "verified_no_issue", total_usage)
            return

        cited = sorted({s.get("file", "") or s.get("url", "") for s in answer.sources})
        if cited and cited in (state.get("cited_today") or []):
            self._audit_usage(room_id, "duplicate_source", total_usage)
            return  # 同じ根拠での再介入は同日中は行わない

        advice = sanitize_for_chatwork(answer.text)
        sources_text = self._format_sources(answer.sources)
        self._deliver(room_id, advice, sources_text, watch.get("mode", "shadow"))

        state["count"] += 1
        state.setdefault("cited_today", []).append(cited)
        self._save_state(room_id, state)
        self._audit(room_id, watch.get("mode", "shadow"), advice, answer)

    def _filter_messages(self, messages: list[dict], last_seen: int, me: int) -> list[dict]:
        min_chars = int(self._config.discussion_watch.get("min_message_chars", 10))
        fresh = []
        for m in messages:
            try:
                mid = int(m.get("message_id"))
            except (TypeError, ValueError):
                continue
            if mid <= int(last_seen):
                continue
            account = m.get("account") or {}
            if int(account.get("account_id") or 0) == me:
                continue  # 自分の発言
            body = str(m.get("body", ""))
            if f"[To:{me}]" in body or f"[rp aid={me}" in body:
                continue  # ボット宛（Q&Aフローが処理）
            if len(strip_chatwork_tags(body)) < min_chars:
                continue
            fresh.append(m)
        return fresh

    def _screen(self, batch: str, usage_out: dict[str, int] | None = None) -> bool:
        try:
            response = self._screen_client.messages.create(
                model=self._config.model,
                max_tokens=1000,
                system=SCREEN_SYSTEM,
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": SCREEN_SCHEMA},
                },
                messages=[{"role": "user", "content": batch}],
            )
        except Exception:
            print("[warn] スクリーニング呼び出しに失敗: " + traceback.format_exc(), flush=True)
            return False
        usage = {}
        for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
            value = getattr(getattr(response, "usage", None), key, None)
            if value:
                usage[key] = int(value)
        self._cost.add_usage(usage)
        if usage_out is not None:
            usage_out.update(usage)
        text = next(
            (b.text for b in reversed(response.content) if getattr(b, "type", "") == "text"),
            "",
        )
        try:
            return bool(json.loads(text).get("suspicious"))
        except (json.JSONDecodeError, TypeError, AttributeError):
            return False  # 判定不能時は沈黙（安全側）

    def _deliver(self, room_id: int, advice: str, sources_text: str, mode: str) -> None:
        if mode == "live":
            self._chatwork.send_message(room_id, advice + sources_text)
            return
        admin_room = self._config.admin_room_id
        if not admin_room:
            print("[warn] admin_room_id未設定のため見習いモードの内報を送信できません", flush=True)
            return
        self._chatwork.send_message(
            int(admin_room),
            f"[info][title]{self._config.agent_name} 見習いモード: 議論への助言候補[/title]"
            f"対象ルーム: {room_id}\n\n"
            f"{advice}{sources_text}\n\n"
            "※見習いモード中のため、ルームには投稿していません。"
            "この助言の精度が良ければ、公開モード（discussion_watch.mode=live）への切替をご検討ください。"
            "的外れな場合はそのままで結構です（内容を教えていただければ改善に使います）。[/info]",
        )

    @staticmethod
    def _format_sources(sources: list[dict]) -> str:
        from .answer import display_name

        labels = []
        for s in sources:
            label = s.get("title") or display_name(s.get("file", ""))
            if s.get("url"):
                label = (label + " " if label else "") + s["url"]
            if label and label not in labels:
                labels.append(label)
        if not labels:
            return ""
        return "\n\n【出典】\n" + "\n".join(f"・{label}" for label in labels)

    def _audit_usage(self, room_id: int, outcome: str, usage: dict[str, int]) -> None:
        """介入しなかった巡回のAPI消費を記録する（費用の可視化・効果測定の前提）。"""
        if not usage:
            return
        try:
            from .audit import AuditLogger

            AuditLogger(
                log_dir=self._config.resolve_path(self._config.audit_log_dir),
                retention_days=self._config.audit_log_retention_days,
            ).log(
                {
                    "type": "watch_scan",
                    "room_id": room_id,
                    "outcome": outcome,
                    "usage": usage,
                    "cost_jpy": round(self._cost.estimate_cost_jpy(usage), 3),
                }
            )
        except Exception:
            print("[warn] 巡回の監査記録に失敗: " + traceback.format_exc(), flush=True)

    def _audit(self, room_id: int, mode: str, advice: str, answer) -> None:
        try:
            from .audit import AuditLogger

            AuditLogger(
                log_dir=self._config.resolve_path(self._config.audit_log_dir),
                retention_days=self._config.audit_log_retention_days,
            ).log(
                {
                    "type": "watch_intervention",
                    "room_id": room_id,
                    "mode": mode,
                    "advice": advice,
                    "sources": [s.get("file") or s.get("url") for s in answer.sources],
                    "usage": answer.usage,
                }
            )
        except Exception:
            print("[warn] 介入の監査記録に失敗: " + traceback.format_exc(), flush=True)

    # --- 状態管理 ---

    def _state_path(self, room_id: int) -> Path:
        return self._config.resolve_path(self._config.state_dir) / f"watch-{room_id}.json"

    def _load_state(self, room_id: int) -> dict:
        path = self._state_path(room_id)
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return {"last_seen": None, "date": "", "count": 0, "cited_today": []}

    def _save_state(self, room_id: int, state: dict) -> None:
        path = self._state_path(room_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    DiscussionWatcher().run_once()


if __name__ == "__main__":
    main()
