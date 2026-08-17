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
    },
    # ひな形からの書類作成（送付状・FAX送付状）。ひな形の実体は templates/ に同梱
    "doc_build": {
        "enabled": False,
        "templates_dir": "templates",
        # 契約書ひな形はここに入れない（締結前のリーガルチェックが必須のため）
        "allowed_templates": [
            "書類送付状_ライズ",
            "書類送付状_ヤマトライジング",
            "書類送付状_楽天軒",
            "FAX送付状",
        ],
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


def _deep_merge(base: dict, override: dict) -> None:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
