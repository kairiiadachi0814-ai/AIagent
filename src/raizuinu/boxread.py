"""Box の資料の読み取り（発行請求書の確認の段階2で使う）。

Box のアプリ（サーバー認証・Client Credentials Grant）として入り、アプリ専用の利用者
（サービスアカウント）の権限で読む。

方針:
- 読み取りだけ。このモジュールにはアップロード・更新・削除・共有の関数を置かない
  （Box へ送るのは、トークンの取得を除いて GET だけ）
- 守りは2重にする。Box は「アプリのスコープ」と「フォルダでの役割」の両方が許す操作だけを
  通す。スコープに書き込みが入っているのは Box がダウンロードにそれを求めるためで、
  書き込みはフォルダ側（サービスアカウントを **ビューアー** で招待）で止める。
  役割が強くなっていないかは `write_permissions` で確かめられる
- 見えるのは、サービスアカウントが招待されたフォルダ（config: box.root_folder_id）の下だけ
- 秘密情報は環境変数のみ（BOX_CLIENT_ID／BOX_CLIENT_SECRET／BOX_ENTERPRISE_ID）。
  例外メッセージにも載せない
"""

from __future__ import annotations

import json
import time
import unicodedata
from typing import Any, Callable

import requests

# アクセストークンの寿命ぎりぎりまで使うと、処理の途中で切れる。少し手前で取り直す
_TOKEN_MARGIN_SECONDS = 120
_DEFAULT_TOKEN_SECONDS = 3600
_PAGE_SIZE = 1000  # 1回の一覧で返る件数の上限（Boxの仕様）
_FOLDER_FIELDS = "id,name,path_collection,permissions"
_ITEM_FIELDS = "id,type,name,size,modified_at"
_ERROR_CHARS = 200

TYPE_FOLDER, TYPE_FILE = "folder", "file"

# フォルダでの役割がビューアーなら、どれも「できない」はずの操作
WRITE_PERMISSIONS = {
    "can_upload": "アップロード",
    "can_rename": "名前の変更",
    "can_delete": "削除",
    "can_invite_collaborator": "他の人の招待",
}


class BoxError(Exception):
    """Box とのやり取りに失敗した。status は HTTP の番号、code は Box の理由コード。"""

    def __init__(self, message: str, status: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _redact(text: str, secrets: tuple[str, ...]) -> str:
    """例外やログに ID・シークレット・トークンが混ざらないようにする。"""
    for secret in secrets:
        if secret and len(secret) >= 6:
            text = text.replace(secret, "***")
    return text


def _same_name(a: str, b: str) -> bool:
    """名前の一致。Mac から上げたファイルは濁点が分かれた形で入っていることがある。"""
    return unicodedata.normalize("NFC", a).strip() == unicodedata.normalize("NFC", b).strip()


def write_permissions(folder: dict[str, Any]) -> list[str] | None:
    """このフォルダで許されている書き込みの操作（日本語の名前）。ビューアーなら空。

    Box が権限を返さなかったときは None（確かめられない）。
    """
    permissions = folder.get("permissions")
    if not isinstance(permissions, dict):
        return None
    return [label for key, label in WRITE_PERMISSIONS.items() if permissions.get(key)]


class BoxClient:
    """Box への読み取り専用の窓口。トークンは寿命が来るまで使い回す。"""

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

    # --- 設定 ---

    @property
    def settings(self) -> dict[str, Any]:
        return self._config.box

    @property
    def api_url(self) -> str:
        return str(self.settings.get("api_url") or "").rstrip("/")

    @property
    def root_folder_id(self) -> str:
        folder_id = str(self.settings.get("root_folder_id") or "").strip()
        if not folder_id:
            raise BoxError("Boxのフォルダが設定されていません（config: box.root_folder_id）")
        return folder_id

    @property
    def max_file_bytes(self) -> int:
        return int(float(self.settings.get("max_file_mb", 20)) * 1024 * 1024)

    def missing_secrets(self) -> list[str]:
        """足りない環境変数の名前（値は返さない）。"""
        values = (
            ("BOX_CLIENT_ID", self._config.box_client_id),
            ("BOX_CLIENT_SECRET", self._config.box_client_secret),
            ("BOX_ENTERPRISE_ID", self._config.box_enterprise_id),
        )
        return [name for name, value in values if not value]

    # --- 認証 ---

    def token(self) -> str:
        """アプリ（サービスアカウント）としてのアクセストークン。"""
        if self._token and self._now() < self._token_expires_at:
            return self._token
        missing = self.missing_secrets()
        if missing:
            raise BoxError("Boxの接続情報が設定されていません: " + "／".join(missing))
        payload = self._call(
            "POST",
            "/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self._config.box_client_id,
                "client_secret": self._config.box_client_secret,
                "box_subject_type": "enterprise",
                "box_subject_id": self._config.box_enterprise_id,
            },
            authenticate=False,
        )
        token = str(payload.get("access_token") or "")
        if not token:
            raise BoxError("Boxに入れましたが、アクセストークンを受け取れませんでした")
        expires_in = payload.get("expires_in")
        if not isinstance(expires_in, (int, float)):
            expires_in = _DEFAULT_TOKEN_SECONDS
        self._token = token
        self._token_expires_at = self._now() + max(0, int(expires_in) - _TOKEN_MARGIN_SECONDS)
        return token

    # --- 読み取り ---

    def me(self) -> dict[str, Any]:
        """いま入っている利用者（サービスアカウント）。"""
        return self._call("GET", "/2.0/users/me", params={"fields": "id,name,login"})

    def folder(self, folder_id: str) -> dict[str, Any]:
        """フォルダの名前・場所・このフォルダでの権限。"""
        return self._call("GET", f"/2.0/folders/{folder_id}", params={"fields": _FOLDER_FIELDS})

    def items(self, folder_id: str) -> list[dict[str, Any]]:
        """フォルダの直下にあるもの（フォルダとファイル）をすべて。"""
        found: list[dict[str, Any]] = []
        while True:
            page = self._call(
                "GET",
                f"/2.0/folders/{folder_id}/items",
                params={"fields": _ITEM_FIELDS, "limit": str(_PAGE_SIZE), "offset": str(len(found))},
            )
            entries = [e for e in page.get("entries") or [] if isinstance(e, dict)]
            found.extend(entries)
            total = page.get("total_count")
            if not entries or not isinstance(total, int) or len(found) >= total:
                return found

    def find(self, folder_id: str, path: str) -> dict[str, Any] | None:
        """フォルダの下を「2026年10月報告/ライズクリエイション」のように名前でたどる。無ければ None。"""
        current: dict[str, Any] = {"id": folder_id, "type": TYPE_FOLDER}
        for name in [part for part in path.replace("\\", "/").split("/") if part.strip()]:
            if current.get("type") != TYPE_FOLDER:
                return None
            hit = next((i for i in self.items(str(current["id"])) if _same_name(str(i.get("name") or ""), name)), None)
            if hit is None:
                return None
            current = hit
        return current

    def download(self, file_id: str, size: int | None = None) -> bytes:
        """ファイルの中身。大きすぎるもの（config: box.max_file_mb）は取りに行かない。"""
        limit = self.max_file_bytes
        if size is not None and size > limit:
            raise BoxError(f"ファイルが大きすぎます（{size:,} バイト。上限 {limit:,} バイト）")
        content = self._call("GET", f"/2.0/files/{file_id}/content", raw=True)
        if len(content) > limit:
            raise BoxError(f"ファイルが大きすぎます（{len(content):,} バイト。上限 {limit:,} バイト）")
        return content

    # --- 内部 ---

    def _secrets(self) -> tuple[str, ...]:
        return (
            self._config.box_client_secret or "",
            self._config.box_client_id or "",
            self._config.box_enterprise_id or "",
            self._token,
        )

    def _call(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None = None,
        data: dict[str, Any] | None = None,
        authenticate: bool = True,
        raw: bool = False,
    ) -> Any:
        if not self.api_url:
            raise BoxError("Boxの接続先が設定されていません（config: box.api_url）")
        headers = {"Accept": "application/json"}
        if authenticate:
            headers["Authorization"] = f"Bearer {self.token()}"
        try:
            response = self._http(
                method,
                self.api_url + path,
                params=params,
                data=data,
                headers=headers,
                timeout=int(self.settings.get("timeout_seconds", 30)),
            )
        except Exception as exc:  # ネットワーク断など
            raise BoxError(f"Boxへ接続できませんでした（{type(exc).__name__}）") from exc

        status = int(response.status_code)
        if status >= 400:
            raise self._error(status, str(response.text or ""))
        if raw:
            if status != 200:  # 202 は「まだ用意できていない」
                raise BoxError(f"ファイルをまだ取得できません（HTTP {status}）", status=status)
            return response.content
        if not str(response.text or "").strip():
            return {}
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise BoxError("Boxの応答を読み取れませんでした") from exc

    def _error(self, status: int, text: str) -> BoxError:
        try:
            body = json.loads(text)
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        code = str(body.get("code") or body.get("error") or "")
        detail = str(body.get("message") or body.get("error_description") or text)
        message = _redact(f"HTTP {status} {code} {detail[:_ERROR_CHARS]}".strip(), self._secrets())
        return BoxError(f"Boxがエラーを返しました: {message}", status=status, code=_redact(code, self._secrets()))
