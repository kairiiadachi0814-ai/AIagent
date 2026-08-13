"""AWS Lambdaアダプタ（API Gateway / Function URL想定の薄い層）。

実行基盤の推奨はCloud Run（要件定義書11章の推奨案）だが、コアが基盤非依存の
ため、AWSを選ぶ場合も本ファイルだけで動く。Lambdaではバックグラウンド
スレッドが応答後に凍結されるため、同期処理（webhook_async=false）で使うこと。
API Gatewayの29秒タイムアウトを超える場合はFunction URL＋応答ストリーミング
またはSQS挟み込みを検討する。
"""

from __future__ import annotations

import base64

from .config import Config
from .handler import RaizuinuHandler
from .webhook import SIGNATURE_HEADER

_handler: RaizuinuHandler | None = None


def _get_handler() -> RaizuinuHandler:
    global _handler
    if _handler is None:
        config = Config.load()
        config.data["webhook_async"] = False  # Lambdaでは同期処理
        _handler = RaizuinuHandler(config)
    return _handler


def lambda_handler(event: dict, context: object) -> dict:
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    body = event.get("body") or ""
    raw_body = (
        base64.b64decode(body) if event.get("isBase64Encoded") else body.encode("utf-8")
    )
    result = _get_handler().handle_webhook(
        raw_body, headers.get(SIGNATURE_HEADER.lower())
    )
    return {"statusCode": result.status, "body": result.detail}
