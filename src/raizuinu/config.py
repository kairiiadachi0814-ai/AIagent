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
    "agent_name": "らいずいぬ",
    "model": "claude-opus-5",
    "effort": "medium",
    # claude-opus-5は思考がデフォルトONで、max_tokensは思考+本文の合計上限。
    # 小さくしすぎるとJSONが途中で切れるため16000を下限の目安とする。
    "max_tokens": 16000,
    "context_message_count": 20,
    "allowed_room_ids": [],
    "admin_room_id": None,
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
