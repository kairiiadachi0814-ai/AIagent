# らいずいぬ Phase 1 — Cloud Run用コンテナ
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# アプリ本体と設定
COPY src/ src/
COPY config/ config/
# ハンドブック（リポジトリ直下の*.md。管理ファイルの除外はconfig.json側で管理）
COPY *.md ./

ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1

# workers=1 固定（二重応答防止・コスト集計がプロセス内状態前提のため増やさない）
CMD exec gunicorn --bind :${PORT:-8080} --workers 1 --threads 8 --timeout 0 "raizuinu.app:create_app()"
