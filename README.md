# 社内AIエージェント（経理財務アシスタント）

チームみらいのAIエージェント「みらいいぬ」の公開情報を参考に、
株式会社ライズクリエイション向けに構築する社内AIエージェントのリポジトリ。

## 構成

- `docs/phase1-requirements.md`：Phase 1 要件定義書
- `handbook/`：組織の決まりごとと意思決定の正本（最初の整備対象は経理・財務）
- `sops/`：定型業務の手順書
- `CLAUDE.md`：Claude Codeでの開発コンテキスト

## 最初にやること

1. このフォルダをGitHubプライベートリポジトリとして登録する
2. `handbook/10-経理財務/` の `【要記入】` を実際のルールで埋める
3. `docs/phase1-requirements.md` の未決事項（11章）を決める
4. Claude Codeでリポジトリを開き、Phase 1 の実装を開始する

## Phase 1 実装（2026-08-13）

`src/raizuinu/` にPhase 1（メンション応答・出典付き回答）を実装済み。
11章未決事項の推奨案は `docs/phase1-requirements.md` 11章を参照（暫定採用・責任者承認待ち）。

### セットアップ

```
pip install -r requirements.txt
python -m pytest tests/          # テスト
```

環境変数（コード・設定ファイルへの直書き禁止）:

| 変数 | 内容 |
|---|---|
| `ANTHROPIC_API_KEY` | Claude APIキー |
| `CHATWORK_API_TOKEN` | エージェント専用アカウントのAPIトークン |
| `CHATWORK_WEBHOOK_TOKEN` | Webhook署名検証用トークン |

`config/config.json` の `allowed_room_ids`（許可ルーム）と `admin_room_id`（コスト通知先）を設定すること。許可ルーム未設定時はどのルームにも応答しない（安全側）。

### ローカル動作確認（Chatwork不要）

```
python run_local.py "銀行明細の取得手順は？"
```

### デプロイ

**本番環境はさくらのVPSに構築済み（2026-08-13）。** サーバー情報・運用コマンド・更新手順は [docs/deploy-vps.md](docs/deploy-vps.md) を参照。以下はCloud Runを選ぶ場合の参考手順。

### 参考: Google Cloud Runの場合

1. コンテナ化して `gunicorn "raizuinu.app:create_app()"` を起動（PYTHONPATH=src、**workersは既定の1のまま**にする）
2. **「CPU always allocated」（--no-cpu-throttling）を有効にする**（Webhook応答後にバックグラウンドで回答処理を行うため）
3. **`--max-instances=1` を設定する**（コスト集計・二重応答防止がプロセス内状態＋ローカルファイル前提のため）
4. **状態・ログを永続化する**: Cloud Runのローカルファイルシステムは揮発性のため、GCSボリュームマウントを設定し、`config.json` の `state_dir`・`audit_log_dir` をマウント先の絶対パス（例: `/mnt/state`・`/mnt/logs`）に変更する
   ```
   gcloud run deploy ... \
     --no-cpu-throttling --max-instances=1 \
     --add-volume name=data,type=cloud-storage,bucket=<バケット名> \
     --add-volume-mount volume=data,mount-path=/mnt
   ```
   これを省くと、インスタンス再作成のたびに月次コスト累計がゼロに戻り上限停止（NFR-04）が機能しない
5. 環境変数3種をSecret Manager経由で注入
6. `config.json` の `admin_room_id` を必ず設定する（未設定だとコスト上限の70%アラート・停止通知が届かない）
7. ChatworkのWebhook（自分宛メンション）に `https://<サービスURL>/webhook` を登録

AWS Lambdaを選ぶ場合は `raizuinu.lambda_function.lambda_handler` を使用（同期処理・API Gatewayの29秒制限に注意。`state_dir`・`audit_log_dir` は `/tmp` 配下＋外部永続化が必要）。
