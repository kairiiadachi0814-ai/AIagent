"""コアハンドラ（基盤非依存）。

Webhook受信 → 署名検証 → 許可ルーム判定（FR-05）→ コスト上限判定（NFR-04）
→ 会話コンテキスト取得（FR-01）→ ハンドブック参照回答（FR-02〜04）
→ Chatwork返信 → 監査ログ（NFR-03）を1本のフローとして提供する。

HTTPアダプタ（app.py / lambda_function.py）は本モジュールを呼ぶだけの薄い層。
"""

from __future__ import annotations

import json
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path

from .answer import AnswerGenerator, format_reply
from .audit import AuditLogger
from .chatwork import ChatworkClient, format_context
from .config import Config
from .cost import CostTracker
from .handbook import HandbookLoader
from .webhook import MentionEvent, SignatureError, parse_mention, verify_signature

FAILURE_MESSAGE = (
    "申し訳ありません、処理に失敗してしまいました。"
    "少し時間をおいて、もう一度お声がけいただけますか。"
)
STOPPED_MESSAGE = (
    "申し訳ありません。今月のAI利用枠が上限に達したため、いったんお休みしています。"
    "お急ぎの場合は管理者までご連絡ください。"
)
BUSY_MESSAGE = (
    "すみません、いまご質問が混み合っています。"
    "少し時間をおいて、もう一度お声がけいただけますか。"
)

_DEDUPE_MAX = 1000


@dataclass
class WebhookResult:
    status: int
    detail: str


class RaizuinuHandler:
    def __init__(self, config: Config | None = None, **overrides) -> None:
        self._config = config or Config.load()
        cfg = self._config

        self._handbook_loader = overrides.get("handbook_loader") or HandbookLoader(
            roots=[cfg.resolve_path(r) for r in cfg.handbook["roots"]],
            include=cfg.handbook["include"],
            exclude=cfg.handbook["exclude"],
            cache_ttl_seconds=cfg.handbook_cache_ttl_seconds,
        )
        self._chatwork = overrides.get("chatwork") or ChatworkClient(
            cfg.chatwork_api_token or ""
        )
        self._generator = overrides.get("generator") or AnswerGenerator(cfg)
        self._cost = overrides.get("cost") or CostTracker(
            state_dir=cfg.resolve_path(cfg.state_dir),
            limit_jpy=cfg.monthly_cost_limit_jpy,
            alert_threshold=cfg.cost_alert_threshold,
            usd_jpy_rate=cfg.usd_jpy_rate,
            pricing_usd_per_mtok=cfg.pricing_usd_per_mtok,
            prompt_cache_ttl=cfg.prompt_cache_ttl,
        )
        self._audit = overrides.get("audit") or AuditLogger(
            log_dir=cfg.resolve_path(cfg.audit_log_dir),
            retention_days=cfg.audit_log_retention_days,
        )
        self._doc_task = overrides.get("doc_task")
        if self._doc_task is None and cfg.doc_task.get("enabled"):
            from .doctask import DocTaskRunner

            self._doc_task = DocTaskRunner(cfg, self._chatwork)
        self._dedupe_lock = threading.Lock()
        self._processed_ids: dict[str, None] = {}  # 挿入順を保つLRU代替
        self._dedupe_path = cfg.resolve_path(cfg.state_dir) / "processed_messages.json"
        self._load_dedupe()
        # バースト時のコスト超過・スレッド枯渇を防ぐ（同時実行と滞留数を制限）
        from concurrent.futures import ThreadPoolExecutor

        self._executor = ThreadPoolExecutor(
            max_workers=int(cfg.webhook_max_concurrency), thread_name_prefix="raizuinu"
        )
        self._queue_slots = threading.Semaphore(int(cfg.webhook_max_queue))

    # --- エントリポイント ---

    def handle_webhook(self, raw_body: bytes, signature: str | None) -> WebhookResult:
        """Webhookリクエストを処理する。戻り値はHTTPステータスと説明。"""
        try:
            verify_signature(raw_body, signature, self._config.chatwork_webhook_token)
        except SignatureError as exc:
            return WebhookResult(401, str(exc))

        try:
            event = parse_mention(raw_body)
        except (json.JSONDecodeError, KeyError, ValueError, UnicodeDecodeError) as exc:
            return WebhookResult(400, f"ペイロード解析エラー: {exc}")

        if event is None:
            return WebhookResult(200, "対象外イベント")

        # 許可ルーム制限（FR-05）: リスト外は無応答
        if event.room_id not in set(self._config.allowed_room_ids):
            return WebhookResult(200, f"許可外ルーム: {event.room_id}")

        # 再送による二重応答の防止
        if not self._mark_processed(event.message_id):
            return WebhookResult(200, "処理済みメッセージ")

        if self._config.webhook_async:
            if not self._queue_slots.acquire(blocking=False):
                # 滞留上限超過。無言で捨てない（FR-06の趣旨）
                try:
                    self._chatwork.send_message(
                        event.room_id,
                        f"[rp aid={event.account_id} to={event.room_id}-{event.message_id}]\n"
                        + BUSY_MESSAGE,
                    )
                except Exception:
                    print("[warn] 混雑通知の送信に失敗", flush=True)
                return WebhookResult(200, "滞留上限超過")
            self._executor.submit(self._process_and_release, event)
            return WebhookResult(200, "受付（非同期処理中）")
        self._process_safely(event)
        return WebhookResult(200, "処理完了")

    def answer_question(self, question: str, context: str = "") -> "tuple[str, dict]":
        """Chatworkを介さず質問に回答する（ローカル動作確認用）。"""
        handbook = self._handbook_loader.load()
        answer = self._generator.generate(question, handbook, context)
        status = self._cost.add_usage(answer.usage)
        return answer.text + _sources_suffix(answer), {
            "usage": answer.usage,
            "monthly_total_jpy": round(status.total_jpy, 2),
        }

    # --- 内部フロー ---

    def _process_and_release(self, event: MentionEvent) -> None:
        try:
            self._process_safely(event)
        finally:
            self._queue_slots.release()

    def _process_safely(self, event: MentionEvent) -> None:
        try:
            self._process(event)
        except Exception:
            # 無言で終わらせない（FR-06）: ルームへ失敗を通知し、詳細はログへ
            error_detail = traceback.format_exc()
            print("[error] " + error_detail, flush=True)
            try:
                self._chatwork.send_message(
                    event.room_id,
                    f"[rp aid={event.account_id} to={event.room_id}-{event.message_id}]\n"
                    + FAILURE_MESSAGE,
                )
            except Exception:
                print("[error] 失敗通知の送信にも失敗: " + traceback.format_exc(), flush=True)
            self._audit_safely(
                {
                    "type": "error",
                    "room_id": event.room_id,
                    "account_id": event.account_id,
                    "message_id": event.message_id,
                    "question": event.question,
                    "answer": FAILURE_MESSAGE,
                    "error": error_detail.splitlines()[-1] if error_detail else "",
                }
            )

    def _process(self, event: MentionEvent) -> None:
        cfg = self._config

        # コスト上限（NFR-04）: 到達時は停止し、その旨を通知
        status = self._cost.status()
        if status.over_limit:
            self._notify_stopped_once(status)
            self._chatwork.send_message(
                event.room_id,
                f"[rp aid={event.account_id} to={event.room_id}-{event.message_id}]\n"
                + STOPPED_MESSAGE,
            )
            self._audit_safely(
                {
                    "type": "stopped",
                    "room_id": event.room_id,
                    "account_id": event.account_id,
                    "message_id": event.message_id,
                    "question": event.question,
                    "answer": STOPPED_MESSAGE,
                }
            )
            return

        question = event.question
        if not question:
            return

        # 同一ルームの直近会話を文脈として付与（FR-01）
        context = ""
        messages: list = []
        try:
            messages = self._chatwork.get_recent_messages(
                event.room_id, limit=cfg.context_message_count
            )
            context = format_context(messages, exclude_message_id=event.message_id)
        except Exception:
            # 文脈取得の失敗は回答自体を止めない
            print("[warn] 会話コンテキスト取得に失敗: " + traceback.format_exc(), flush=True)

        handbook = self._handbook_loader.load()
        if not handbook.files:
            # ハンドブック0件のまま回答し続ける事故を防ぐ（サイレント劣化の禁止）
            raise RuntimeError(
                "ハンドブックが1件も読み込めません。handbook.roots の設定を確認してください"
            )

        # 文書つき雑務依頼（会議ファイルの議事録作成・要約など）→ 専用フロー。
        # マニュアル原本のURL（ハンドブック記載）は対象外にしてQ&Aで扱う
        if self._doc_task is not None:
            from .answer import _url_in_handbook
            from .doctask import (
                NO_DOCUMENT_GUIDANCE,
                find_document,
                looks_like_document_request,
                mentions_google_doc,
            )

            url_check = lambda url: _url_in_handbook(url, handbook)  # noqa: E731
            document = find_document(
                event.body, messages, question,
                bot_account_id=event.to_account_id, handbook_url_check=url_check,
            )
            if (
                document is None
                and looks_like_document_request(question)
                and not mentions_google_doc(event.body)  # 原本URLはQ&Aで扱う
            ):
                # 「さっきのファイル」を探すときだけ、文脈用より広く遡って再検索する
                try:
                    wider = self._chatwork.get_recent_messages(
                        event.room_id,
                        limit=int(cfg.doc_task.get("search_message_count", 60)),
                    )
                    document = find_document(
                        event.body, wider, question,
                        bot_account_id=event.to_account_id, handbook_url_check=url_check,
                    )
                except Exception:
                    print("[warn] 添付ファイル探索に失敗: " + traceback.format_exc(), flush=True)
                if document is None:
                    # 文書依頼と分かっているものをQ&Aへ流すと「機能がない」等の
                    # 誤った回答になるため、ここで案内文を返して終える
                    self._reply_and_audit(
                        event, question, NO_DOCUMENT_GUIDANCE, "doc_task_not_found"
                    )
                    return
            if document is not None:
                self._process_doc_task(event, question, document)
                return
        # 生成直前に上限を再確認（コンテキスト取得の間に他リクエストが計上した分を反映）
        status = self._cost.status()
        if status.over_limit:
            self._notify_stopped_once(status)
            self._chatwork.send_message(
                event.room_id,
                f"[rp aid={event.account_id} to={event.room_id}-{event.message_id}]\n"
                + STOPPED_MESSAGE,
            )
            return
        answer = self._generator.generate(question, handbook, context)

        # コストはAPI消費が確定した時点で計上する（送信失敗でも計上漏れさせない）
        status = None
        try:
            status = self._cost.add_usage(answer.usage)
        except Exception:
            print("[warn] コスト計上に失敗: " + traceback.format_exc(), flush=True)

        # マニュアル更新の報告 → 受付リストへ記録し、管理者へ通知（FR外の運用機能）
        if answer.intent == "manual_update_report":
            self._handle_update_report(event, answer, status)
            return

        # 回答への指摘・改善要望 → 改善リストへ記録し、管理者へ通知（自己改善サイクル）
        if answer.intent == "answer_feedback":
            self._handle_feedback(event, answer, status)
            return

        reply = format_reply(answer, event.account_id, event.room_id, event.message_id)
        self._chatwork.send_message(event.room_id, reply)

        # 返信成功後の後処理での例外は失敗メッセージを送らない（二重送信防止）
        try:
            if status is not None:
                self._maybe_alert(status)
            self._audit_safely(
                {
                    "type": "answer",
                    "room_id": event.room_id,
                    "account_id": event.account_id,
                    "message_id": event.message_id,
                    "question": question,
                    "has_answer": answer.has_answer,
                    "refused": answer.refused,
                    "intent": answer.intent,
                    "model": cfg.model,
                    "reference_url": answer.reference_url,
                    "stage2": answer.stage2,
                    "sources": [s["file"] for s in answer.sources],
                    "answer": answer.text,
                    "usage": answer.usage,
                    "cost_jpy": round(self._cost.estimate_cost_jpy(answer.usage), 3),
                    "monthly_total_jpy": round(status.total_jpy, 2) if status else None,
                }
            )
        except Exception:
            print(
                "[warn] 返信後の通知・監査処理に失敗（返信自体は成功）: "
                + traceback.format_exc(),
                flush=True,
            )

    def _reply_and_audit(
        self, event: MentionEvent, question: str, text: str, record_type: str
    ) -> None:
        """API消費のない定型返信を送り、監査ログに残す。"""
        from .answer import sanitize_for_chatwork

        self._chatwork.send_message(
            event.room_id,
            f"[rp aid={event.account_id} to={event.room_id}-{event.message_id}]\n"
            + sanitize_for_chatwork(text),
        )
        self._audit_safely(
            {
                "type": record_type,
                "room_id": event.room_id,
                "account_id": event.account_id,
                "message_id": event.message_id,
                "question": question,
                "answer": text,
            }
        )

    def _process_doc_task(self, event: MentionEvent, question: str, document: dict) -> None:
        """文書つき依頼を処理して返信する（ハンドブック・出典検証は使わない）。"""
        from .answer import sanitize_for_chatwork

        # 生成直前の上限再確認（Q&Aフローと同じ扱い）
        status = self._cost.status()
        if status.over_limit:
            self._notify_stopped_once(status)
            self._chatwork.send_message(
                event.room_id,
                f"[rp aid={event.account_id} to={event.room_id}-{event.message_id}]\n"
                + STOPPED_MESSAGE,
            )
            return

        reply_text, meta, usage = self._doc_task.run(question, document, event.room_id)

        status = None
        try:
            if usage:
                status = self._cost.add_usage(usage)
        except Exception:
            print("[warn] コスト計上に失敗: " + traceback.format_exc(), flush=True)

        self._chatwork.send_message(
            event.room_id,
            f"[rp aid={event.account_id} to={event.room_id}-{event.message_id}]\n"
            + sanitize_for_chatwork(reply_text),
        )

        # 返信成功後の後処理での例外は失敗メッセージを送らない（二重送信防止）
        try:
            if status is not None:
                self._maybe_alert(status)
            self._audit_safely(
                {
                    "type": "doc_task",
                    "room_id": event.room_id,
                    "account_id": event.account_id,
                    "message_id": event.message_id,
                    "question": question,
                    "document": meta,
                    "answer": reply_text[:2000],
                    "model": self._config.model,
                    "usage": usage,
                    "cost_jpy": round(self._cost.estimate_cost_jpy(usage), 3) if usage else 0.0,
                    "monthly_total_jpy": round(status.total_jpy, 2) if status else None,
                }
            )
        except Exception:
            print(
                "[warn] 返信後の通知・監査処理に失敗（返信自体は成功）: "
                + traceback.format_exc(),
                flush=True,
            )

    def _handle_update_report(self, event: MentionEvent, answer, status) -> None:
        """マニュアル更新報告を受付リストに記録し、報告者へ受領返信・管理者へ通知する。

        実際の反映（原本の再転記→デプロイ）は品質確認のため管理者側の作業とする。
        """
        from datetime import datetime, timezone, timedelta

        manuals = answer.reported_manuals or ["（対象マニュアル名は本文参照）"]
        entry = {
            "ts": datetime.now(timezone(timedelta(hours=9))).isoformat(),
            "room_id": event.room_id,
            "account_id": event.account_id,
            "message_id": event.message_id,
            "manuals": answer.reported_manuals,
            "body": event.question,
            "status": "pending",
        }
        queue_path = self._config.resolve_path(self._config.state_dir) / "pending_updates.jsonl"
        try:
            queue_path.parent.mkdir(parents=True, exist_ok=True)
            with open(queue_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            print("[warn] 更新受付リストの書き込みに失敗: " + traceback.format_exc(), flush=True)

        from .answer import sanitize_for_chatwork

        ack = sanitize_for_chatwork(answer.text) or "マニュアル更新のご報告、受け付けました。"
        reply = (
            f"[rp aid={event.account_id} to={event.room_id}-{event.message_id}]\n"
            + ack
            + "\n\n反映作業のリストに追加しました。反映が済むまでは、少し前の内容でお答えすることがある点だけご了承ください。"
        )
        self._chatwork.send_message(event.room_id, reply)

        # 管理者への通知（報告があったルームと別の場合のみ。同一なら受領返信で足りる）
        admin_room = self._config.admin_room_id
        if admin_room and admin_room != event.room_id:
            try:
                self._chatwork.send_message(
                    admin_room,
                    f"[info][title]{self._config.agent_name} マニュアル更新報告[/title]"
                    f"対象: {sanitize_for_chatwork('、'.join(manuals))}\n"
                    f"報告ルーム: {event.room_id} / 報告者アカウント: {event.account_id}\n"
                    "反映するには、Claude Codeで「マニュアル更新を反映して」と依頼してください。[/info]",
                )
            except Exception:
                print("[warn] 更新報告の管理者通知に失敗", flush=True)

        self._audit_safely(
            {
                "type": "update_report",
                "room_id": event.room_id,
                "account_id": event.account_id,
                "message_id": event.message_id,
                "question": event.question,
                "manuals": answer.reported_manuals,
                "answer": ack,
                "usage": answer.usage,
                "monthly_total_jpy": round(status.total_jpy, 2) if status else None,
            }
        )

    def _handle_feedback(self, event: MentionEvent, answer, status) -> None:
        """回答への指摘・改善要望を改善リストに記録し、受領返信・管理者通知する。

        記録は提案材料としてのみ使い、反映は管理者承認のもとClaude Code経由で行う。
        """
        from datetime import datetime, timezone, timedelta

        entry = {
            "ts": datetime.now(timezone(timedelta(hours=9))).isoformat(),
            "room_id": event.room_id,
            "account_id": event.account_id,
            "message_id": event.message_id,
            "body": event.question,
            "status": "pending",
        }
        queue_path = self._config.resolve_path(self._config.state_dir) / "feedback.jsonl"
        try:
            queue_path.parent.mkdir(parents=True, exist_ok=True)
            with open(queue_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            print("[warn] 改善リストの書き込みに失敗: " + traceback.format_exc(), flush=True)

        from .answer import sanitize_for_chatwork

        ack = (
            sanitize_for_chatwork(answer.text)
            or "ご指摘ありがとうございます。改善リストに追加し、管理者が確認します。"
        )
        self._chatwork.send_message(
            event.room_id,
            f"[rp aid={event.account_id} to={event.room_id}-{event.message_id}]\n" + ack,
        )

        admin_room = self._config.admin_room_id
        if admin_room and admin_room != event.room_id:
            try:
                self._chatwork.send_message(
                    admin_room,
                    f"[info][title]{self._config.agent_name} フィードバック受付[/title]"
                    f"内容: {sanitize_for_chatwork(event.question[:300])}\n"
                    f"報告ルーム: {event.room_id} / 報告者アカウント: {event.account_id}\n"
                    "週次の自己分析レポートに反映されます。[/info]",
                )
            except Exception:
                print("[warn] フィードバックの管理者通知に失敗", flush=True)

        self._audit_safely(
            {
                "type": "feedback",
                "room_id": event.room_id,
                "account_id": event.account_id,
                "message_id": event.message_id,
                "question": event.question,
                "answer": ack,
                "usage": answer.usage,
                "monthly_total_jpy": round(status.total_jpy, 2) if status else None,
            }
        )

    def _audit_safely(self, record: dict) -> None:
        try:
            self._audit.log(record)
        except Exception:
            print("[warn] 監査ログ書き込みに失敗: " + traceback.format_exc(), flush=True)

    def _maybe_alert(self, status) -> None:
        if status.over_limit and not status.stopped_notified:
            self._notify_stopped_once(status)
            return
        if self._cost.needs_alert(status):
            admin_room = self._config.admin_room_id
            if not admin_room:
                print(
                    "[warn] admin_room_id未設定のためコストアラートを送信できません "
                    f"（{int(status.ratio() * 100)}%到達）",
                    flush=True,
                )
                return  # フラグは立てない（設定後に通知させる）
            try:
                self._chatwork.send_message(
                    admin_room,
                    f"[info][title]{self._config.agent_name} コスト通知[/title]"
                    f"今月のAPI利用額が上限の{int(status.ratio() * 100)}%に達しました"
                    f"（{status.total_jpy:,.0f}円 / {status.limit_jpy:,.0f}円）。[/info]",
                )
            except Exception:
                print("[warn] コストアラート送信に失敗（次回再試行）", flush=True)
                return  # 送信成功時のみフラグを立てる
            self._cost.mark_alert_sent()

    def _notify_stopped_once(self, status) -> None:
        if status.stopped_notified:
            return
        admin_room = self._config.admin_room_id
        if not admin_room:
            print(
                "[warn] admin_room_id未設定のため停止通知を送信できません（上限到達で応答停止中）",
                flush=True,
            )
            return  # フラグは立てない（設定後に通知させる）
        try:
            self._chatwork.send_message(
                admin_room,
                f"[info][title]{self._config.agent_name} 停止通知[/title]"
                f"今月のAPI利用額が上限（{status.limit_jpy:,.0f}円）に達したため、"
                "応答を停止しました。再開するには上限額の見直しが必要です。[/info]",
            )
        except Exception:
            print("[warn] 停止通知の送信に失敗（次回再試行）", flush=True)
            return  # 送信成功時のみフラグを立てる
        self._cost.mark_stopped_notified()

    # --- 二重処理防止 ---

    def _load_dedupe(self) -> None:
        try:
            if self._dedupe_path.exists():
                ids = json.loads(self._dedupe_path.read_text(encoding="utf-8"))
                self._processed_ids = {str(i): None for i in ids[-_DEDUPE_MAX:]}
        except (json.JSONDecodeError, OSError):
            self._processed_ids = {}

    def _mark_processed(self, message_id: str) -> bool:
        """未処理ならTrueを返し記録する。処理済みならFalse。"""
        with self._dedupe_lock:
            if message_id in self._processed_ids:
                return False
            self._processed_ids[message_id] = None
            while len(self._processed_ids) > _DEDUPE_MAX:
                self._processed_ids.pop(next(iter(self._processed_ids)))
            try:
                self._dedupe_path.parent.mkdir(parents=True, exist_ok=True)
                self._dedupe_path.write_text(
                    json.dumps(list(self._processed_ids)), encoding="utf-8"
                )
            except OSError:
                pass
            return True


def _sources_suffix(answer) -> str:
    from .answer import display_name

    if answer.has_answer and answer.sources:
        labels = []
        for s in answer.sources:
            label = (s.get("title") or display_name(s["file"])) + (
                f"（{s['heading']}）" if s.get("heading") else ""
            )
            if s.get("url"):
                label += f" {s['url']}"
            if label not in labels:
                labels.append(label)
        return "\n\n【出典】\n" + "\n".join(f"・{label}" for label in labels)
    return ""
