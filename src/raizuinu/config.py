"""設定管理。

非秘密設定は config/config.json、秘密情報は環境変数から読む（NFR-01）。
- ANTHROPIC_API_KEY      : Claude APIキー
- CHATWORK_API_TOKEN     : エージェント専用アカウントのAPIトークン
- CHATWORK_WEBHOOK_TOKEN : Webhook署名検証用トークン
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.json"

_DEFAULTS: dict[str, Any] = {
    "agent_name": "経理財務アシスタント",
    "model": "claude-opus-5",
    "effort": "medium",
    # claude-opus-5は思考がデフォルトONで、max_tokensは思考+本文の合計上限。
    # 小さくしすぎるとJSONが途中で切れるため16000を下限の目安とする。
    "max_tokens": 16000,
    "context_message_count": 20,
    "allowed_room_ids": [],
    "admin_room_id": None,
    # 書き込みを伴う依頼（予定の登録・取り消し）を受け付けるアカウント。
    # Chatworkの「管理者」役割は部署の多くが持つため、役割ではなくIDで絞る
    "admin_account_ids": [],
    # 経理財務部メンバーのアカウントID。ここに無い人には社内の手順・ナレッジを
    # 返さない（ハンドブックをプロンプトに載せない）。空なら制限しない
    "member_account_ids": [],
    # アシスタントが聞き返した直後の返信は、メンバー以外でも通常フローで扱う。
    # 会話を途中で打ち切らないための猶予（分）
    "guest_followup_minutes": 30,
    "agent_account_id": None,
    "handbook": {
        "roots": ["."],
        "include": ["*.md"],
        "exclude": [],
    },
    "handbook_cache_ttl_seconds": 300,
    "prompt_cache_ttl": "1h",
    "fallback_enabled": True,
    "web_fetch_enabled": False,
    "web_fetch_allowed_domains": [],
    "web_fetch_max_uses": 3,
    "web_fetch_max_content_tokens": 30000,
    "legal_enabled": False,
    "body_url_allowlist": [],
    "monthly_cost_limit_jpy": 10000,
    "cost_alert_threshold": 0.7,
    "usd_jpy_rate": 155.0,
    "pricing_usd_per_mtok": {
        "input": 5.0,
        "output": 25.0,
        "cache_read": 0.5,
        "cache_write_5m": 6.25,
        "cache_write_1h": 10.0,
    },
    "audit_log_dir": "logs",
    "audit_log_retention_days": 90,
    "state_dir": "state",
    "webhook_async": True,
    "webhook_max_concurrency": 2,
    "webhook_max_queue": 10,
    # 文書つき雑務依頼（会議ファイルの議事録作成・要約など）
    "doc_task": {
        "enabled": False,
        "max_file_mb": 20,
        # PDFはbase64化で約1.33倍になるため、APIの32MB上限に収まる範囲に抑える
        "max_pdf_mb": 15,
        "max_pdf_pages": 50,
        "max_text_chars": 120000,
        # 「さっきのファイル」を探すときだけ、文脈用より広くメッセージを遡る
        "search_message_count": 60,
        # 一度読んだ文書をルーム単位で覚えておく時間（分）。会話の途中から入った
        # 人が添付し直さずに続きを聞けるようにする（既定24時間）
        "remember_minutes": 1440,
    },
    # ひな形からの書類作成（送付状・FAX送付状）。ひな形の実体は templates/ に同梱
    "doc_build": {
        "enabled": False,
        # 作れる書類は 送付状 / FAX送付状 の2種類。差出人の会社は
        # templates/companies.json で管理する（契約書は作らない。
        # 締結前のリーガルチェックが必須のため）
        "templates_dir": "templates",
        "max_items": 20,
        "attach_to_chatwork": True,
    },
    # 予定の照会・登録と朝の通知。トヨクモ スケジューラーは読み取り専用のため、
    # 読みは iCal（トヨクモ）＋Googleカレンダー、書きはGoogleカレンダーのみ
    "schedule": {
        "enabled": False,
        "owner_name": "",
        "notify_room_id": None,
        "notify_account_id": None,
        "notify_weekdays_only": True,
        "ics_labels": ["トヨクモ スケジューラー"],
        # トヨクモは外部から予定を登録できず、取り込みも一度きりで自動更新されない。
        # Googleにしか無い予定はトヨクモの画面に出ないため、印で示す
        "unsynced_source": "Googleカレンダー",
        "unsynced_note": "（トヨクモ未反映）",
        "register_note": "",
        "greeting": "おはようございます。",
    },
    # 定型の受け答えに揺らぎを持たせる（社交辞令の部分だけ。回答本文・出典・
    # 免責文・金額などは対象外）。false にすると固定の言い回しに戻る
    "phrasing": {"vary_openings": True},
    # TaskRising（社内タスク管理）への登録。Supabaseを直接叩く。
    # 経費支払いタスク・振込用CSV等が揃うまでは無効のまま置く
    "taskrising": {
        "enabled": False,
        "url": "https://dldtjyyypmltylllhqdv.supabase.co",
        "tasks_table": "tasks",
        "timeout_seconds": 30,
        # 依頼文の項目 → テーブルの列。スキーマが分かってから埋める
        "field_map": {},
    },
    # レターパックの手配依頼。送付状を作ったら総務へ取り次ぐ。
    # supplies_room_id は許可ルーム（Q&Aの対象）に入れない。投稿と巡回だけに使い、
    # 他部署のルームへ社内ナレッジが流れる経路を作らないため
    "letterpack": {
        "enabled": False,
        # 差出人の会社ごとの依頼先。company_id で引き、無ければ default を使う。
        # ライズは総務あて、楽天軒は経理財務部の担当者あて、と送り先が違う
        "routes": {},
        # 総務の返信を巡回で拾う（備品ルームはwebhookの対象外のため）
        "follow_up": True,
        # 「2枚で」「送信」といった短い返事を、いつまで続きとみなすか
        "reply_window_minutes": 120,
        # 先方からの質問に依頼者が答えるまでの猶予。相手の都合があるので長めに取る
        "answer_window_hours": 48,
        # こちらが答え終えたあと、動きがないまま閉じるまでの時間。
        # リアクションだけで済まされた場合もここで静かに閉じる
        "settle_hours": 24,
        # 一度も返事をもらえていない依頼を、依頼者へ知らせるまでの時間。
        # 土日は数えない（金曜夕方の依頼に土曜の朝「返事がない」と言わない）
        "no_reply_hours": 6,
        # 催促の経過時間を数える時間帯。夜間は相手が見られないので数えない
        "office_hours": {"start": 9, "end": 18},
        # 返信が来ないまま放置されたやり取りを閉じるまでの日数
        "max_open_days": 7,
    },
    # 議論ウォッチャー（5分ごとのタイマーで実行。modeは shadow=管理者へ内報のみ / live=ルームへ投稿）
    "discussion_watch": {
        "enabled": False,
        "room_ids": [],
        "mode": "shadow",
        "max_interventions_per_day": 3,
        # 2段目の裏取りはハンドブック全文を載せるため1回が高い。介入に至らない
        # 分も消費するので、介入回数とは別に日次の上限を持つ
        "max_verifications_per_day": 10,
        "min_message_chars": 10,
        "max_batch_messages": 30,
    },
}


@dataclass
class Config:
    """アプリ設定。値の出所は config.json → 既定値の順。"""

    data: dict[str, Any] = field(default_factory=dict)
    base_dir: Path = field(default_factory=Path.cwd)

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "Config":
        config_path = Path(path) if path else DEFAULT_CONFIG_PATH
        merged = json.loads(json.dumps(_DEFAULTS))  # deep copy
        if config_path.exists():
            with open(config_path, encoding="utf-8") as f:
                loaded = json.load(f)
            _deep_merge(merged, loaded)
            base_dir = config_path.resolve().parents[1]
        else:
            base_dir = Path.cwd()
        # デプロイ環境向けの上書き（Cloud RunでGCSマウント先を指す等）
        for env_name, key in (
            ("RAIZUINU_STATE_DIR", "state_dir"),
            ("RAIZUINU_AUDIT_LOG_DIR", "audit_log_dir"),
        ):
            value = os.environ.get(env_name)
            if value:
                merged[key] = value
        return cls(data=merged, base_dir=base_dir)

    def __getattr__(self, name: str) -> Any:
        try:
            return self.data[name]
        except KeyError:
            raise AttributeError(name) from None

    def resolve_path(self, relative: str) -> Path:
        p = Path(relative)
        return p if p.is_absolute() else self.base_dir / p

    # --- 秘密情報（環境変数のみ。コード・設定ファイルへの直書き禁止） ---

    @property
    def anthropic_api_key(self) -> str | None:
        return os.environ.get("ANTHROPIC_API_KEY")

    @property
    def chatwork_api_token(self) -> str | None:
        return os.environ.get("CHATWORK_API_TOKEN")

    @property
    def chatwork_webhook_token(self) -> str | None:
        return os.environ.get("CHATWORK_WEBHOOK_TOKEN")

    # TaskRising（Supabase）。ボットユーザーで入るための3点。
    # service_role キーは使わない（行レベルの権限制御を素通りするため）
    @property
    def taskrising_api_key(self) -> str | None:
        return os.environ.get("TASKRISING_API_KEY")

    @property
    def taskrising_bot_email(self) -> str | None:
        return os.environ.get("TASKRISING_BOT_EMAIL")

    @property
    def taskrising_bot_password(self) -> str | None:
        return os.environ.get("TASKRISING_BOT_PASSWORD")

    @property
    def taskrising_bot_refresh_token(self) -> str | None:
        """ボットのGoogleアカウントで一度だけ手動ログインして得た更新トークン。

        メール＋パスワードを使わない場合の入り口。使うたびに入れ替わるため、
        以後は状態ファイル側が正となる（環境変数は最初の1回だけ効く）。
        """
        return os.environ.get("TASKRISING_BOT_REFRESH_TOKEN")


def _deep_merge(base: dict, override: dict) -> None:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
