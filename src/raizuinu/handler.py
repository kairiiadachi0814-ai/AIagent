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


def _reply_tag(event: MentionEvent) -> str:
    """返信先を指すChatworkタグ（本文の先頭に置く）。"""
    return f"[rp aid={event.account_id} to={event.room_id}-{event.message_id}]\n"


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        print("[warn] 状態の書き込みに失敗: " + traceback.format_exc(), flush=True)


def _display_name(messages: list, account_id: int) -> str:
    """直近メッセージから依頼者の表示名を拾う（書類の差出人担当者に使う）。"""
    for msg in reversed(messages or []):
        account = msg.get("account") or {}
        if int(account.get("account_id", 0) or 0) == int(account_id):
            return str(account.get("name", "")).strip()
    return ""


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
        self._doc_memory = overrides.get("doc_memory")
        if self._doc_memory is None and self._doc_task is not None:
            from .doctask import DocumentMemory

            self._doc_memory = DocumentMemory(
                cfg.resolve_path(cfg.state_dir) / "room_documents.json",
                ttl_minutes=int(cfg.doc_task.get("remember_minutes", 1440)),
            )
        self._doc_build = overrides.get("doc_build")
        if self._doc_build is None and cfg.doc_build.get("enabled"):
            from .docbuild import DocBuildRunner

            self._doc_build = DocBuildRunner(cfg)
        self._schedule = overrides.get("schedule")
        if self._schedule is None and cfg.schedule.get("enabled"):
            from .scheduletask import ScheduleRunner

            self._schedule = ScheduleRunner(cfg)
        # 空き時間の提案と複数人の日程調整（予定機能の上に載る）
        self._plan = overrides.get("plan")
        if (
            self._plan is None
            and self._schedule is not None
            and (cfg.schedule.get("proposal") or {}).get("enabled")
        ):
            from .scheduleplan import PlanRunner

            self._plan = PlanRunner(cfg)
        self._letterpack = overrides.get("letterpack")
        if self._letterpack is None and cfg.letterpack.get("enabled"):
            from .letterpack import LetterpackRunner

            self._letterpack = LetterpackRunner(cfg, self._chatwork)
        self._chatlog = overrides.get("chatlog")
        if self._chatlog is None and cfg.chat_archive.get("enabled"):
            from .chatlog import ChatArchive, ChatLogAnswerer

            self._chatlog = ChatLogAnswerer(
                cfg,
                ChatArchive(
                    cfg.resolve_path(cfg.state_dir) / "chatlog.sqlite3",
                    retention_days=int(cfg.chat_archive.get("retention_days", 730)),
                ),
            )
        self._guest = overrides.get("guest")
        if self._guest is None and cfg.member_account_ids:
            from .guest import GuestResponder

            self._guest = GuestResponder(cfg)
        # FAXルーム（許可ルーム外）での「未処理のFAXある？」にだけ答える
        self._fax_status = overrides.get("fax_status")
        if self._fax_status is None and (cfg.fax_watch or {}).get("enabled"):
            from .faxwatch import FaxStatus

            self._fax_status = FaxStatus(cfg)
        # 管理者の確認を経て別ルームへ流すお知らせ（announce.py。控えが無ければ何もしない）
        self._announcer = overrides.get("announcer")
        if self._announcer is None and cfg.admin_room_id:
            from .announce import Announcer

            self._announcer = Announcer(cfg, self._chatwork)
        # 通知管理くんのメンションを合図に、その場でFAXを読みに行く（5分の巡回を待たない）
        self._fax_watch_factory = overrides.get("fax_watch_factory")
        if self._fax_watch_factory is None and (cfg.fax_watch or {}).get("enabled"):
            from .faxwatch import FaxWatcher

            self._fax_watch_factory = lambda: FaxWatcher(cfg, self._chatwork, cost=self._cost)
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

        # 許可ルーム制限（FR-05）: リスト外は無応答。
        # FAXルームだけは例外で、対応状況の問い合わせに限って答える（ハンドブックは使わない）
        if event.room_id not in set(self._config.allowed_room_ids):
            if self._fax_status is not None and self._fax_status.owns(event.room_id):
                if not self._mark_processed(event.message_id):
                    return WebhookResult(200, "処理済みメッセージ")
                self._process_fax_status(event)
                return WebhookResult(200, "FAXの対応状況を返答")
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
                        _reply_tag(event)
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
                    _reply_tag(event)
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
                _reply_tag(event)
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

        # 聞き返し中の依頼は1往復ぶんだけ有効。読んだ時点で消し、
        # 再度聞き返すときに改めて保存する
        pending = self._load_pending(event)
        if pending:
            self._clear_pending(event)

        # 経理財務部メンバー以外には、社内の手順・ナレッジを返さない。
        # ハンドブックを渡さない軽量フローへ回す（構造的に答えようがない）
        if self._guest is not None and not self._is_member(event, pending):
            self._process_guest(event, question)
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

        # 聞き返しへの答えは、元の依頼に足して同じフローへ戻す。
        # 「宛先は◯◯です」だけでは書類作成の依頼と分からず、Q&Aへ落ちてしまうため
        if pending and self._continues_pending(pending, question, event.body):
            merged = f"{pending['instruction']}\n{question}"
            if pending["flow"] == "doc_build" and self._doc_build is not None:
                self._process_doc_build(event, merged, context, messages)
                return
            if pending["flow"] == "schedule" and self._schedule is not None:
                # 登録の聞き返しへの答えは登録として続ける（候補の提案へ戻さない）
                self._process_schedule(
                    event, merged, force_register=(pending.get("mode") == "register")
                )
                return

        # お知らせの下書きへの「送信」「取りやめ」（管理者ルームだけ。控えが無ければ素通り）
        if self._announcer is not None:
            handled = self._announcer.handle(event.room_id, event.account_id, question)
            if handled is not None:
                self._reply_and_audit(event, question, handled, "announce")
                return

        # レターパック手配のやり取りの続き（要否の返事・文面の確認・総務への回答）。
        # 「2枚で」「送信」のような短い返事はQ&Aへ落ちてしまうため、先に見る。
        # 進行中のやり取りが無ければ None が返り、通常の振り分けへ進む
        if self._letterpack is not None:
            from .letterpack import LetterpackError

            try:
                handled = self._letterpack.handle(
                    event.room_id,
                    event.account_id,
                    int(event.send_time),
                    question,
                    display_name=_display_name(messages, event.account_id),
                )
            except LetterpackError as exc:
                self._reply_and_audit(event, question, str(exc), "letterpack_failed")
                return
            if handled is not None:
                self._reply_and_audit(event, question, handled, "letterpack")
                return

        # 予定の照会・登録・取り消し → 専用フロー。
        # 登録は管理者アカウントからの依頼だけ受け付ける（ルーム制限とは別に効かせる）
        if self._schedule is not None:
            from .scheduletask import (
                NOT_ADMIN_MESSAGE,
                is_cancel_request,
                is_register_request,
                looks_like_schedule_request,
            )

            owner = str(cfg.schedule.get("owner_name", ""))
            # 空き時間の提案（誰でも聞ける）と、出した候補からの登録（管理者だけ）。
            # 候補を出した直後の「登録して」「2番で」は、予定の言葉が無くても続きとして扱う
            proposal = follow_up = False
            if self._plan is not None:
                from .scheduleplan import looks_like_proposal_request, pick_number

                proposal = looks_like_proposal_request(question)
                if pick_number(question) is not None or is_register_request(question):
                    follow_up = (
                        self._plan.pending(event.room_id, event.account_id, int(event.send_time))
                        is not None
                    )
            if looks_like_schedule_request(question, owner) or proposal or follow_up:
                writes = follow_up or (
                    not proposal and (is_register_request(question) or is_cancel_request(question))
                )
                if writes and event.account_id not in set(cfg.admin_account_ids):
                    self._reply_and_audit(event, question, NOT_ADMIN_MESSAGE, "schedule_denied")
                    return
                self._process_schedule(event, question)
                return

        # ひな形からの書類作成（送付状・FAX送付状）→ 専用フロー。
        # 議事録フローより先に見る。ひな形ファイルを添えて依頼された場合、
        # find_document がキーワードを見ずに文書タスクへ吸い込んでしまうため
        build_request = False
        if self._doc_build is not None:
            from .docbuild import looks_like_document_build_request

            # 本文も渡す。添付つきで「要約して」と言われた依頼は文書タスクへ回す
            build_request = looks_like_document_build_request(question, event.body)
            if build_request:
                self._process_doc_build(event, question, context, messages)
                return

        # 文書つき雑務依頼（会議ファイルの議事録作成・要約など）→ 専用フロー。
        # マニュアル原本のURL（ハンドブック記載）は対象外にしてQ&Aで扱う
        if self._doc_task is not None and not build_request:
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
            if document is None and self._doc_memory is not None:
                # 会話の途中から入った人が添付し直さずに済むよう、そのルームで
                # 直近に読んだ文書を思い出す（その話題への返信か、名指しのときだけ）
                document = self._doc_memory.recall(
                    event.room_id, question, event.body, int(event.send_time)
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
                    # 添付し直してもらう案内。文面を足しても解決しないので
                    # フローは覚えず、部外判定の猶予だけ与える
                    self._save_pending(event, "doc_task", question)
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
                _reply_tag(event)
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

        # ハンドブックに無い質問は、そのルームの過去のやり取りを探す。
        # 「楽天BillPayのパスワードは？」のように、チャットにしか無い値がある
        if not answer.has_answer and answer.intent == "question" and self._chatlog is not None:
            if self._answer_from_chatlog(event, question, status):
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

    def _process_fax_status(self, event: MentionEvent) -> None:
        """FAXルームでのメンションに答える（部のメンバーからのものだけ）。

        対応状況の問い合わせは一覧で、「了解です」のような会話は会話で返す。
        ハンドブックは使わない（このルームは許可ルーム外）。
        """
        cfg = self._config.fax_watch
        if int(event.account_id) == int(cfg.get("notifier_account_id", 0) or 0):
            self._run_fax_watch_now(event)  # 通知管理くんの合図。返事はしない
            return
        question = event.question
        if not question or not self._is_member(event):
            return  # 部外のメンションには応じない
        from .faxwatch import is_completion

        if is_completion(question) and self._fax_watch_factory is not None:
            # 「対応完了」の報告は巡回側の処理（閉じて、お礼と残りを1通で返す）に任せる。
            # 実例（2026-09-08）: 会話の返事とお礼が別々に2通届いて分かりにくかった。
            # 巡回を待たずその場で回し、閉じるものが無かったときだけ先へ進む
            watcher = self._run_fax_watch_now(event, reason="completion")
            if watcher is not None and getattr(watcher, "closed_last_run", []):
                return
        try:
            if self._fax_status.is_status_question(question):
                text, usage = self._fax_status.reply(question)
            else:
                # アシスタント宛の文は内容を読んで返す。済んだ報告と読めたら（長い文でも）
                # 発注書を閉じ、お礼と残りを1通で返す
                text, usage, done = self._fax_status.converse(
                    question, replied_to=self._replied_text(event)
                )
                if (done.get("all") or done.get("filenames")) and self._fax_watch_factory is not None:
                    closed = self._fax_watch_factory().close_manually(
                        event.room_id,
                        {"account": {"account_id": event.account_id}, "message_id": event.message_id},
                        filenames=done.get("filenames") or None,
                        everything=bool(done.get("all")),
                    )
                    if closed:
                        self._add_usage_safely(usage) if usage else None
                        self._audit_safely(
                            {
                                "type": "fax_status",
                                "room_id": event.room_id,
                                "account_id": event.account_id,
                                "message_id": event.message_id,
                                "question": question,
                                "answer": f"（閉じた: {closed}件）",
                                "usage": usage,
                            }
                        )
                        return
            cost_status = self._add_usage_safely(usage) if usage else None
            self._reply_and_audit(event, question, text, "fax_status")
            if cost_status is not None:
                self._maybe_alert(cost_status)
        except Exception:
            print("[error] FAXルームでの返答に失敗: " + traceback.format_exc(), flush=True)
            try:
                self._chatwork.send_message(event.room_id, _reply_tag(event) + FAILURE_MESSAGE)
            except Exception:
                print("[error] 失敗通知の送信にも失敗", flush=True)

    def _run_fax_watch_now(self, event: MentionEvent, reason: str = "notice") -> Any:
        """FAXの通知や完了の報告が来たら、巡回を待たずに見に行く。→ 使った見張り（失敗時 None）

        通知（reason="notice"）は本文とPDFが別々の投稿で届くことがあるため、少し待ってから
        見て、何も新しいものが無ければもう一度だけ見直す。完了の報告（"completion"）は
        待たずに1回だけ回す。
        """
        import time

        settings = (self._config.fax_watch.get("on_mention") or {})
        if not settings.get("enabled", True) or self._fax_watch_factory is None:
            return None
        handled = 0
        watcher = None
        try:
            if reason == "notice":
                time.sleep(float(settings.get("delay_seconds", 5)))
            watcher = self._fax_watch_factory()
            handled = watcher.run_once()
            if reason == "notice" and handled == 0 and not watcher.has_pending():
                time.sleep(float(settings.get("retry_seconds", 10)))
                handled = watcher.run_once()
        except Exception:
            print("[error] メンション起点のFAX処理に失敗（次の巡回で拾う）: " + traceback.format_exc(), flush=True)
            watcher = None
        self._audit_safely(
            {
                "type": "fax_mention_trigger",
                "room_id": event.room_id,
                "message_id": event.message_id,
                "reason": reason,
                "handled": handled,
                "closed": [t.get("filename") for t in getattr(watcher, "closed_last_run", []) or []],
            }
        )
        return watcher

    def _replied_text(self, event: MentionEvent) -> str:
        """相手が返信しているこちらの発言（会話の文脈として渡す）。無ければ空。"""
        try:
            from .doctask import reply_target
            from .webhook import strip_chatwork_tags

            target = reply_target(event.body or "")
            if not target:
                return ""
            for message in self._chatwork.get_recent_messages(event.room_id, limit=100):
                if str(message.get("message_id")) == str(target):
                    return strip_chatwork_tags(str(message.get("body", "")))[:400]
        except Exception:
            print("[warn] 返信元の取得に失敗: " + traceback.format_exc(), flush=True)
        return ""

    def _reply_and_audit(
        self, event: MentionEvent, question: str, text: str, record_type: str
    ) -> None:
        """API消費のない定型返信を送り、監査ログに残す。"""
        from .answer import sanitize_for_chatwork

        self._chatwork.send_message(
            event.room_id,
            _reply_tag(event)
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

    # --- メンバー判定 ---

    def _pending_path(self) -> Path:
        return self._config.resolve_path(self._config.state_dir) / "pending_request.json"

    def _load_pending(self, event: MentionEvent) -> dict | None:
        """聞き返し中の依頼（期限切れなら無効）。"""
        entry = _read_json(self._pending_path()).get(f"{event.room_id}:{event.account_id}")
        if not entry:
            return None
        minutes = int(self._config.guest_followup_minutes)
        if int(event.send_time) - int(entry.get("ts", 0)) > minutes * 60:
            return None
        return entry

    def _save_pending(
        self, event: MentionEvent, flow: str, instruction: str, mode: str = ""
    ) -> None:
        """聞き返した依頼を覚えておく（返ってきた答えを元の依頼に足すため）。

        mode は同じフローの中の段階（予定なら "register"）。答えが返ってきたとき、
        その段階へまっすぐ戻すために使う。
        """
        data = _read_json(self._pending_path())
        data[f"{event.room_id}:{event.account_id}"] = {
            "flow": flow,
            "instruction": instruction,
            "ts": int(event.send_time),
            "mode": mode,
        }
        _write_json(self._pending_path(), data)

    @staticmethod
    def _continues_pending(pending: dict, question: str, body: str) -> bool:
        """聞き返しへの答えとみなしてよいか（別件の依頼なら足さない）。"""
        if pending.get("flow") not in ("doc_build", "schedule"):
            return False
        from .docbuild import looks_like_document_build_request
        from .doctask import _FILE_TAG_RE
        from .scheduletask import looks_like_schedule_request

        # 添付つき、または新しい依頼と読める文なら、聞き返しの答えではない
        if _FILE_TAG_RE.search(body or ""):
            return False
        if pending["flow"] == "doc_build" and looks_like_schedule_request(question):
            return False
        if pending["flow"] == "schedule" and looks_like_document_build_request(question):
            return False
        return True

    def _clear_pending(self, event: MentionEvent) -> None:
        data = _read_json(self._pending_path())
        if data.pop(f"{event.room_id}:{event.account_id}", None) is not None:
            _write_json(self._pending_path(), data)

    def _is_member(self, event: MentionEvent, pending: dict | None = None) -> bool:
        """通常のフローで応対してよい相手か。

        経理財務部メンバーはそのまま。メンバー以外でも、アシスタントが直前に
        聞き返した相手なら、その返信だけは通常どおり扱う（会話を途中で
        打ち切らないため）。
        """
        members = {int(i) for i in self._config.member_account_ids}
        if not members:
            return True  # 未設定なら制限しない（設定漏れで全員を遮断しないため）
        if int(event.account_id) in members:
            return True
        # 部外の方でも、聞き返した直後の返信は通常フローで受ける
        return pending is not None

    def _process_guest(self, event: MentionEvent, question: str) -> None:
        """部外の方へ、ハンドブックを使わずに短く応対する。"""
        status = self._cost.status()
        if status.over_limit:
            self._chatwork.send_message(event.room_id, _reply_tag(event) + STOPPED_MESSAGE)
            return

        reply, usage = self._guest.reply(question)
        cost_status = self._add_usage_safely(usage)
        self._chatwork.send_message(event.room_id, _reply_tag(event) + reply)
        try:
            self._audit_safely(
                {
                    "type": "guest",
                    "room_id": event.room_id,
                    "account_id": event.account_id,
                    "message_id": event.message_id,
                    "question": question,
                    "answer": reply,
                    "model": self._config.model,
                    "usage": usage,
                    "cost_jpy": round(self._cost.estimate_cost_jpy(usage), 3) if usage else 0.0,
                    "monthly_total_jpy": (
                        round(cost_status.total_jpy, 2) if cost_status else None
                    ),
                }
            )
        except Exception:
            print("[warn] 部外応対の監査に失敗: " + traceback.format_exc(), flush=True)

    def _process_schedule(
        self, event: MentionEvent, question: str, force_register: bool = False
    ) -> None:
        """予定の照会・登録・取り消し・日程調整を処理して返信する。

        同じことを二度聞かないために、直前に出した候補や条件（PlanRunner が
        覚えているもの）を先に使う。「登録して」だけなら候補を登録し、
        「午後で」なら同じ日程の話として候補を出し直す。
        """
        from .answer import sanitize_for_chatwork
        from .scheduletask import is_cancel_request, is_register_request

        status = self._cost.status()
        if status.over_limit:
            self._notify_stopped_once(status)
            self._chatwork.send_message(event.room_id, _reply_tag(event) + STOPPED_MESSAGE)
            return

        state_path = self._config.resolve_path(self._config.state_dir) / "schedule_last.json"
        is_admin = event.account_id in set(self._config.admin_account_ids)
        send_time = int(event.send_time)
        plan_pending = None
        number = None
        proposal = False
        if self._plan is not None:
            from .scheduleplan import looks_like_proposal_request, pick_number, strip_pick

            number = pick_number(question)
            proposal = looks_like_proposal_request(question) and not force_register
            plan_pending = self._plan.pending(event.room_id, event.account_id, send_time)
        registering = force_register or (is_register_request(question) and not proposal)

        handled = None
        if plan_pending is not None and number is not None and plan_pending.get("candidates"):
            # 出した候補から番号で選んで登録（管理者だけ。関門は呼び出し側）
            handled = self._plan.register_pick(
                plan_pending, number, self._schedule, extra=strip_pick(question)
            )
        elif plan_pending is not None and registering and not is_cancel_request(question):
            # 「登録して」「14:30で登録して」を候補で解決する（日付・件名を聞き返さない）
            handled = self._plan.register_from_pending(plan_pending, question, self._schedule)

        if handled is not None:
            reply, meta, usage = handled
            kind = "schedule_register"
            if meta.get("registered"):
                _write_json(state_path, {"registered": meta["registered"]})
                self._plan.clear(event.room_id, event.account_id)
        elif is_cancel_request(question):
            last = _read_json(state_path).get("registered") or []
            reply, meta, usage = self._schedule.cancel(last)
            kind = "schedule_cancel"
            if not meta.get("error"):
                _write_json(state_path, {})
        elif registering:
            # 相手の名前が出ていれば、登録の前にその人の予定と重ならないか確かめる
            check = (
                self._plan.conflict_check(question, event.account_id)
                if self._plan is not None
                else None
            )
            reply, meta, usage = self._schedule.register(question, check=check)
            kind = "schedule_register"
            if meta.get("registered"):
                # 「さっきの予定を取り消して」で消せるよう直前の1件を覚えておく
                _write_json(state_path, {"registered": meta["registered"]})
                if self._plan is not None:
                    self._plan.clear(event.room_id, event.account_id)  # 古い候補を残さない
            elif meta.get("error") == "conflict" and self._plan is not None:
                # 代わりの候補を覚えておき、「1番で登録して」で選べるようにする
                last = self._plan.last_check()
                self._plan.remember(
                    event.room_id, event.account_id, send_time,
                    {
                        "candidates": [c.as_dict() for c in last.get("candidates", [])],
                        "summary": last.get("summary", ""),
                    },
                )
        elif proposal:
            reply, meta, usage = self._plan.propose(
                question, requester_id=event.account_id, is_admin=is_admin,
                context=self._plan.context_for(event.room_id, event.account_id, send_time),
            )
            kind = "schedule_propose"
            self._plan.remember(event.room_id, event.account_id, send_time, meta)
        else:
            reply, meta, usage = self._schedule.answer(question)
            kind = "schedule_answer"

        cost_status = self._add_usage_safely(usage)
        if meta.get("error") == "missing_fields":
            self._save_pending(event, "schedule", question, mode="register")
        self._chatwork.send_message(
            event.room_id, _reply_tag(event) + sanitize_for_chatwork(reply)
        )

        # 返信成功後の後処理での例外は失敗メッセージを送らない（二重送信防止）
        try:
            if cost_status is not None:
                self._maybe_alert(cost_status)
            self._audit_safely(
                {
                    "type": kind,
                    "room_id": event.room_id,
                    "account_id": event.account_id,
                    "message_id": event.message_id,
                    "question": question,
                    "detail": meta,
                    "answer": reply[:2000],
                    "model": self._config.model,
                    "usage": usage,
                    "cost_jpy": round(self._cost.estimate_cost_jpy(usage), 3) if usage else 0.0,
                    "monthly_total_jpy": (
                        round(cost_status.total_jpy, 2) if cost_status else None
                    ),
                }
            )
        except Exception:
            print(
                "[warn] 返信後の通知・監査処理に失敗（返信自体は成功）: "
                + traceback.format_exc(),
                flush=True,
            )

    def _process_doc_build(
        self, event: MentionEvent, question: str, context: str, messages: list
    ) -> None:
        """ひな形から書類を作り、下書きをルームへ添付して返す。"""
        from .answer import sanitize_for_chatwork
        from .docbuild import DocumentBuildError

        status = self._cost.status()
        if status.over_limit:
            self._notify_stopped_once(status)
            self._chatwork.send_message(event.room_id, _reply_tag(event) + STOPPED_MESSAGE)
            return

        try:
            reply_text, meta, usage = self._doc_build.run(
                question,
                context=context,
                requester_name=_display_name(messages, event.account_id),
            )
        except DocumentBuildError as exc:
            # 失敗しても消費済みトークンは計上する（月次上限の根拠を欠かさない）
            self._add_usage_safely(exc.usage)
            self._reply_and_audit(event, question, str(exc), "doc_build_failed")
            return

        status = self._add_usage_safely(usage)
        if meta.get("error") in ("missing_fields", "template_not_found", "unknown_company"):
            # 足りない項目を答えてもらったら、元の依頼に足して作り直す
            self._save_pending(event, "doc_build", question)

        artifact = meta.pop("artifact", None)
        # 送付状を作ったということは郵送する見込みが高い。レターパックの手配を
        # 総務へ取り次ぐか、その場で確認する（FAX送付状は郵送しないので出さない）
        if artifact and self._letterpack is not None and meta.get("fields", {}).get("kind") == "送付状":
            reply_text += "\n\n" + self._letterpack.offer_text
            self._letterpack.offer(
                event.room_id,
                event.account_id,
                int(event.send_time),
                {
                    "company": (meta.get("company_name") or ""),
                    # 会社ごとに備品の依頼先が違う（ライズは総務、楽天軒は経理財務部）
                    "company_id": (meta.get("company") or ""),
                    "to_lines": meta.get("fields", {}).get("to_lines") or [],
                    "items": meta.get("fields", {}).get("items") or [],
                    "staff": meta.get("fields", {}).get("staff") or "",
                },
            )
        body = _reply_tag(event) + sanitize_for_chatwork(reply_text)
        record = {
            "type": "doc_build" if artifact else "doc_build_not_ready",
            "room_id": event.room_id,
            "account_id": event.account_id,
            "message_id": event.message_id,
            "question": question,
            "template": meta.get("template"),
            "output_filename": meta.get("output_filename"),
            "uploaded_file_id": "",
            "detail": meta,
            "answer": reply_text[:2000],
            "model": self._config.model,
            "usage": usage,
            "cost_jpy": round(self._cost.estimate_cost_jpy(usage), 3) if usage else 0.0,
            "monthly_total_jpy": round(status.total_jpy, 2) if status else None,
        }
        try:
            if artifact and self._config.doc_build.get("attach_to_chatwork", True):
                filename, data = artifact
                record["uploaded_file_id"] = self._chatwork.upload_file(
                    event.room_id, sanitize_for_chatwork(filename), data, message=body
                )
            else:
                self._chatwork.send_message(event.room_id, body)
        except Exception:
            # 送信に失敗しても「何を作ろうとしたか」は監査へ残す（NFR-03）
            record["type"] = "doc_build_send_failed"
            record["error"] = traceback.format_exc().splitlines()[-1]
            self._audit_safely(record)
            raise

        # 返信成功後の後処理での例外は失敗メッセージを送らない（二重送信防止）
        try:
            if status is not None:
                self._maybe_alert(status)
            self._audit_safely(record)
        except Exception:
            print(
                "[warn] 返信後の通知・監査処理に失敗（返信自体は成功）: "
                + traceback.format_exc(),
                flush=True,
            )

    def _answer_from_chatlog(self, event: MentionEvent, question: str, status) -> bool:
        """そのルームの過去ログから答える。答えが無ければ False（通常の返信へ戻す）。"""
        from .answer import sanitize_for_chatwork

        try:
            text, meta, usage = self._chatlog.lookup(event.room_id, question)
        except Exception:
            print("[warn] 過去ログの照会に失敗: " + traceback.format_exc(), flush=True)
            return False
        status = self._add_usage_safely(usage) or status
        if not text:
            return False

        self._chatwork.send_message(
            event.room_id, _reply_tag(event) + sanitize_for_chatwork(text)
        )
        try:
            if status is not None:
                self._maybe_alert(status)
            self._audit_safely(
                {
                    "type": "chatlog_answer",
                    "room_id": event.room_id,
                    "account_id": event.account_id,
                    "message_id": event.message_id,
                    "question": question,
                    # 本文は残さない。パスワード等をログへ写して置き場を増やさない
                    "answer": "（過去ログからの回答。本文は記録しません）",
                    "detail": meta,
                    "model": self._config.model,
                    "usage": usage,
                    "cost_jpy": round(self._cost.estimate_cost_jpy(usage), 3) if usage else 0.0,
                    "monthly_total_jpy": round(status.total_jpy, 2) if status else None,
                }
            )
        except Exception:
            print("[warn] 返信後の監査処理に失敗: " + traceback.format_exc(), flush=True)
        return True

    def _add_usage_safely(self, usage: dict) -> object | None:
        """コストを計上する。計上の失敗で返信自体を止めない。"""
        if not usage:
            return None
        try:
            return self._cost.add_usage(usage)
        except Exception:
            print("[warn] コスト計上に失敗: " + traceback.format_exc(), flush=True)
            return None

    def _process_doc_task(self, event: MentionEvent, question: str, document: dict) -> None:
        """文書つき依頼を処理して返信する（ハンドブック・出典検証は使わない）。"""
        from .answer import sanitize_for_chatwork

        # 生成直前の上限再確認（Q&Aフローと同じ扱い）
        status = self._cost.status()
        if status.over_limit:
            self._notify_stopped_once(status)
            self._chatwork.send_message(
                event.room_id,
                _reply_tag(event)
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

        answer_id = self._chatwork.send_message(
            event.room_id,
            _reply_tag(event)
            + sanitize_for_chatwork(reply_text),
        )

        # 返信成功後の後処理での例外は失敗メッセージを送らない（二重送信防止）
        try:
            if self._doc_memory is not None:
                # この文書についてのやり取り（依頼とこちらの回答）を覚えておく。
                # 続きの質問が返信で来たときに、同じ文書を見に行けるようにする
                self._doc_memory.remember(
                    event.room_id,
                    document,
                    [
                        document.get("source_message_id", ""),
                        event.message_id,
                        answer_id,
                    ],
                    int(event.send_time),
                )
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
            _reply_tag(event)
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
            _reply_tag(event) + ack,
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
