"""HTTPアダプタ（Flask）。Cloud Run想定の薄い層。

起動: gunicorn "raizuinu.app:create_app()" など。
注意: webhook_async=true（既定）で非同期処理するため、Cloud Runでは
「CPU always allocated」（--no-cpu-throttling）を有効にすること。
"""

from __future__ import annotations

from flask import Flask, request

from .handler import RaizuinuHandler
from .webhook import SIGNATURE_HEADER


def create_app(handler: RaizuinuHandler | None = None) -> Flask:
    app = Flask(__name__)
    # 巨大ペイロードによるDoS防止（Chatwork Webhookは数KB程度）
    app.config["MAX_CONTENT_LENGTH"] = 1_000_000
    app.config["RAIZUINU_HANDLER"] = handler or RaizuinuHandler()

    @app.post("/webhook")
    def webhook():
        h: RaizuinuHandler = app.config["RAIZUINU_HANDLER"]
        result = h.handle_webhook(
            request.get_data(), request.headers.get(SIGNATURE_HEADER)
        )
        return {"detail": result.detail}, result.status

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=8080)
