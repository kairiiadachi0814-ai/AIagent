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
- 人のログインはGoogleのまま。ボットはブラウザを開けないのでGoogleログインは
  使えず、次のどちらかで入る（どちらでも動くようにしてある）
  (A) メール＋パスワードのボット用ユーザー（TASKRISING_BOT_EMAIL／PASSWORD）
  (B) ボットのGoogleアカウントで一度だけ人が手動ログインし、その際に発行される
      更新トークンを預かる（TASKRISING_BOT_REFRESH_TOKEN）。更新トークンは
      使うたびに入れ替わるため、新しいものを状態ファイルへ保存して引き継ぐ
- 秘密情報は環境変数のみ（TASKRISING_API_KEY／BOT_EMAIL／BOT_PASSWORD／
  BOT_REFRESH_TOKEN）。例外メッセージにもキーを載せない
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
        """足りない環境変数の名前（値は返さない）。

        入り方は2通りあり、どちらかが揃っていればよい。
        """
        if not self._config.taskrising_api_key:
            return ["TASKRISING_API_KEY"]
        if self._refresh_token():
            return []
        if self._config.taskrising_bot_email and self._config.taskrising_bot_password:
            return []
        return [
            "TASKRISING_BOT_EMAIL と TASKRISING_BOT_PASSWORD"
            "（または TASKRISING_BOT_REFRESH_TOKEN）"
        ]

    def sign_in(self) -> str:
        """ボットユーザーとしてログインし、アクセストークンを返す。

        Googleログインはブラウザでの人の操作が要るためサーバーからは使えない。
        更新トークンを預かっていればそれを使い、無ければメール＋パスワードで入る。
        """
        if self._token and self._now() < self._token_expires_at:
            return self._token
        missing = self.missing_secrets()
        if missing:
            raise TaskRisingError(
                "TaskRisingの接続情報が設定されていません: " + "／".join(missing)
            )
        refresh = self._refresh_token()
        if refresh:
            try:
                return self._grant(
                    "refresh_token", {"refresh_token": refresh}
                )
            except TaskRisingError:
                # 更新トークンは使い回しで失効することがある。
                # メール＋パスワードが用意されていれば、そちらへ落とす
                self._save_refresh_token("")
                if not (
                    self._config.taskrising_bot_email
                    and self._config.taskrising_bot_password
                ):
                    raise
        return self._grant(
            "password",
            {
                "email": self._config.taskrising_bot_email,
                "password": self._config.taskrising_bot_password,
            },
        )

    # --- ログインの実処理 ---

    def _grant(self, grant_type: str, body: dict[str, Any]) -> str:
        payload = self._call(
            "POST",
            f"{self.base_url}/auth/v1/token?grant_type={grant_type}",
            headers={"apikey": self._config.taskrising_api_key},
            json_body=body,
            authenticate=False,
        )
        token = str(payload.get("access_token") or "")
        if not token:
            raise TaskRisingError("ログインできましたが、アクセストークンを受け取れませんでした")
        self._token = token
        # expires_in が 0 で返ることもあるため、既定値へ倒すのは「項目が無いとき」だけ
        expires_in = payload.get("expires_in")
        if not isinstance(expires_in, (int, float)):
            expires_in = 3600
        self._token_expires_at = self._now() + max(0, int(expires_in) - _TOKEN_MARGIN_SECONDS)
        # 更新トークンは使うたびに入れ替わる。新しいものを保存しないと次回入れなくなる
        rotated = str(payload.get("refresh_token") or "")
        if rotated:
            self._save_refresh_token(rotated)
        return token

    def _session_path(self):
        return self._config.resolve_path(self._config.state_dir) / "taskrising_session.json"

    def _refresh_token(self) -> str:
        """保存済みの更新トークン。無ければ環境変数の初期値を使う。"""
        path = self._session_path()
        try:
            saved = str(json.loads(path.read_text(encoding="utf-8")).get("refresh_token") or "")
        except (OSError, ValueError):
            saved = ""
        return saved or str(self._config.taskrising_bot_refresh_token or "")

    def _save_refresh_token(self, token: str) -> None:
        path = self._session_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"refresh_token": token}), encoding="utf-8")
            path.chmod(0o600)  # 他のユーザーから読めないようにする
        except OSError:
            print("[warn] TaskRisingの更新トークンを保存できませんでした", flush=True)

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
