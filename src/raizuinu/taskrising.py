"""TaskRising（社内タスク管理）への登録。

TaskRisingはNext.js＋Supabase（Postgres＋認証）で動いており、独自のREST APIを
持たない。SupabaseのPostgREST（/rest/v1/）を直接叩く。

方針:
- 管理者権限のキー（service_role）は使わない。専用のボットユーザーとして
  ログインし、そのユーザーの権限（RLS）の範囲でだけ読み書きする。
  service_role は行レベルの権限制御を素通りするため、取り違えや不具合が
  そのまま全データへ及ぶ
- 画面の自動操作（ブラウザ）は使わない。画面の作り替えで壊れるうえ、
  VPSでブラウザを動かす必要が出るため
- 秘密情報は環境変数のみ（TASKRISING_API_KEY／BOT_EMAIL／BOT_PASSWORD）。
  例外メッセージにもキーを載せない
- 稼働は経費支払いタスク・振込用CSV等が揃ってから。既定では無効
  （config: taskrising.enabled = false）
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

import requests

# アクセストークンの寿命ぎりぎりまで使うと、処理の途中で切れる。少し手前で取り直す
_TOKEN_MARGIN_SECONDS = 120


class TaskRisingError(Exception):
    """TaskRising（Supabase）とのやり取りに失敗した。"""


def _redact(text: str, secrets: tuple[str, ...]) -> str:
    """例外やログにキー・パスワードが混ざらないようにする。"""
    for secret in secrets:
        if secret and len(secret) >= 8:
            text = text.replace(secret, "***")
    return text


class TaskRisingClient:
    """Supabase（PostgREST＋認証）への最小限の窓口。

    ボットユーザーとしてログインし、そのトークンで読み書きする。
    トークンは寿命が来るまで使い回す（毎回ログインしない）。
    """

    def __init__(
        self,
        config: Any,
        http: Callable[..., requests.Response] | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._http = http or requests.request
        self._now = now
        self._token = ""
        self._token_expires_at = 0.0

    # --- 公開API ---

    @property
    def settings(self) -> dict[str, Any]:
        return self._config.taskrising

    @property
    def base_url(self) -> str:
        url = str(self.settings.get("url") or "").rstrip("/")
        if not url:
            raise TaskRisingError("TaskRisingのURLが設定されていません（config: taskrising.url）")
        return url

    def missing_secrets(self) -> list[str]:
        """足りない環境変数の名前（値は返さない）。"""
        pairs = (
            ("TASKRISING_API_KEY", self._config.taskrising_api_key),
            ("TASKRISING_BOT_EMAIL", self._config.taskrising_bot_email),
            ("TASKRISING_BOT_PASSWORD", self._config.taskrising_bot_password),
        )
        return [name for name, value in pairs if not value]

    def sign_in(self) -> str:
        """ボットユーザーとしてログインし、アクセストークンを返す。

        Googleログインは人の操作が要るためサーバーからは使えない。
        ボット用にはメール＋パスワードのユーザーを1つ用意してもらう。
        """
        if self._token and self._now() < self._token_expires_at:
            return self._token
        missing = self.missing_secrets()
        if missing:
            raise TaskRisingError(
                "TaskRisingの接続情報が設定されていません: " + "／".join(missing)
            )
        payload = self._call(
            "POST",
            f"{self.base_url}/auth/v1/token?grant_type=password",
            headers={"apikey": self._config.taskrising_api_key},
            json_body={
                "email": self._config.taskrising_bot_email,
                "password": self._config.taskrising_bot_password,
            },
            authenticate=False,
        )
        token = str(payload.get("access_token") or "")
        if not token:
            raise TaskRisingError("ログインに成功しましたが、アクセストークンを受け取れませんでした")
        self._token = token
        self._token_expires_at = self._now() + max(
            0, int(payload.get("expires_in") or 3600) - _TOKEN_MARGIN_SECONDS
        )
        return token

    def schema(self) -> dict[str, Any]:
        """PostgRESTが公開しているテーブル定義（OpenAPI）。

        どの項目が必須かをこちらで推測せずに済ませるために使う。
        """
        return self._call("GET", f"{self.base_url}/rest/v1/")

    def select(
        self, table: str, params: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        query = dict(params or {})
        query.setdefault("select", "*")
        result = self._call("GET", f"{self.base_url}/rest/v1/{table}", params=query)
        return result if isinstance(result, list) else []

    def insert(self, table: str, row: dict[str, Any]) -> dict[str, Any]:
        """1件登録して、登録された行を返す。"""
        result = self._call(
            "POST",
            f"{self.base_url}/rest/v1/{table}",
            json_body=row,
            headers={"Prefer": "return=representation"},
        )
        if isinstance(result, list):
            return result[0] if result else {}
        return result if isinstance(result, dict) else {}

    # --- 内部 ---

    def _call(
        self,
        method: str,
        url: str,
        params: dict[str, str] | None = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        authenticate: bool = True,
    ) -> Any:
        sent = {
            "apikey": self._config.taskrising_api_key or "",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if authenticate:
            sent["Authorization"] = f"Bearer {self.sign_in()}"
        sent.update(headers or {})
        try:
            response = self._http(
                method,
                url,
                params=params,
                json=json_body,
                headers=sent,
                timeout=int(self.settings.get("timeout_seconds", 30)),
            )
        except Exception as exc:  # ネットワーク断など
            raise TaskRisingError(f"TaskRisingへ接続できませんでした（{type(exc).__name__}）") from exc

        if response.status_code >= 400:
            secrets = (
                self._config.taskrising_api_key or "",
                self._config.taskrising_bot_password or "",
                self._token,
            )
            body = _redact(str(response.text)[:300], secrets)
            raise TaskRisingError(f"TaskRisingがエラーを返しました: HTTP {response.status_code} {body}")
        if not response.text.strip():
            return {}
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise TaskRisingError("TaskRisingの応答を読み取れませんでした") from exc


def table_columns(schema: dict[str, Any], table: str) -> dict[str, dict[str, Any]]:
    """OpenAPIのテーブル定義から、列名→定義 を取り出す。"""
    definitions = schema.get("definitions") or schema.get("components", {}).get("schemas", {})
    entry = definitions.get(table) or {}
    return entry.get("properties") or {}


def required_columns(schema: dict[str, Any], table: str) -> list[str]:
    """登録時に値を入れないといけない列。

    PostgRESTのOpenAPIでは、NOT NULL かつ既定値の無い列が required に載る。
    主キーの自動採番などは除いて考える必要があるため、判断材料として返す。
    """
    definitions = schema.get("definitions") or schema.get("components", {}).get("schemas", {})
    entry = definitions.get(table) or {}
    return list(entry.get("required") or [])
