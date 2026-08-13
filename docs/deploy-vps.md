# デプロイ構築記録 — さくらのVPS（2026-08-13構築）

らいずいぬPhase 1の本番環境。要件定義書11章の実行基盤決定（版0.3）に対応する構築時点の記録（as-built）。

## サーバー

| 項目 | 値 |
|---|---|
| サービス | さくらのVPS（v5）1G TK02（東京第2） |
| ホスト名 | `tk2-262-40529.vs.sakura.ne.jp`（IP: 160.16.240.33） |
| OS | Ubuntu 24.04 LTS |
| SSH | `ubuntu`ユーザー・鍵認証のみ（鍵: 作業PCの `C:\Users\admin\.ssh\raizuinu_vps`） |
| sudo | ubuntuユーザーにNOPASSWD付与（`/etc/sudoers.d/99-ubuntu-nopasswd`） |
| スワップ | 2GB（`/swapfile`。1GBメモリのOOM対策） |
| 同居予定 | BTC自動売買ボット（別ユーザー・別領域で追加すること） |

## 構成

```
Chatwork Webhook（mention_to_me）
  → https://tk2-262-40529.vs.sakura.ne.jp/webhook
  → Caddy（80/443、Let's Encrypt自動更新） → 127.0.0.1:8081
  → gunicorn（workers=1）raizuinu.app:create_app() ← systemd: raizuinu.service
```

| 役割 | パス |
|---|---|
| アプリ本体 | `/opt/raizuinu/app`（gitリポジトリの`git archive`を展開。所有: raizuinu） |
| venv | `/opt/raizuinu/venv` |
| 秘密情報 | `/etc/raizuinu/env`（root 600。ANTHROPIC_API_KEY／CHATWORK_API_TOKEN／CHATWORK_WEBHOOK_TOKEN） |
| 状態（コスト累計・dedupe） | `/var/lib/raizuinu`（`RAIZUINU_STATE_DIR`で指定） |
| 監査ログ | `/var/log/raizuinu/audit-YYYYMM.jsonl`（`RAIZUINU_AUDIT_LOG_DIR`で指定） |
| systemdユニット | `/etc/systemd/system/raizuinu.service` |
| Caddy設定 | `/etc/caddy/Caddyfile` |

さくらのVPSコントロールパネルの**パケットフィルター**で TCP 22/80/443 を許可済み（80/443を消すと証明書更新とWebhook受信が止まるので注意）。

## 運用コマンド（作業PCから）

```
# 状態確認
ssh -i C:\Users\admin\.ssh\raizuinu_vps ubuntu@tk2-262-40529.vs.sakura.ne.jp "sudo systemctl status raizuinu --no-pager"

# ログ確認（直近50行）
ssh -i C:\Users\admin\.ssh\raizuinu_vps ubuntu@tk2-262-40529.vs.sakura.ne.jp "sudo journalctl -u raizuinu -n 50 --no-pager"

# 再起動（環境変数ファイル変更後など）
ssh -i C:\Users\admin\.ssh\raizuinu_vps ubuntu@tk2-262-40529.vs.sakura.ne.jp "sudo systemctl restart raizuinu"
```

## アプリ更新手順（ハンドブック改訂・コード修正時）

リポジトリの変更をコミット後、作業PCのGit Bashで：

```
cd /c/Users/admin/Box/Claude/AIagent
git archive HEAD | ssh -i /c/Users/admin/.ssh/raizuinu_vps ubuntu@tk2-262-40529.vs.sakura.ne.jp \
  "sudo -u raizuinu tar -x -C /opt/raizuinu/app && sudo systemctl restart raizuinu"
```

依存パッケージ（requirements.txt）を変えた場合は、更新後に：

```
ssh -i /c/Users/admin/.ssh/raizuinu_vps ubuntu@tk2-262-40529.vs.sakura.ne.jp \
  "sudo -u raizuinu /opt/raizuinu/venv/bin/pip install -r /opt/raizuinu/app/requirements.txt"
```

## コストの実測・目安

claude-opus-5での実測（2026-08-13、変更前）:

- 初回質問（プロンプトキャッシュ構築を含む）: **約202円**（ハンドブック全文≈12.8万トークンの1時間キャッシュ書き込み）
- キャッシュ有効中（直前の質問から1時間以内）の質問: **約15〜20円**

2026-08-13に **claude-sonnet-5へ変更**（`config.json`のmodel＋単価表を更新）。単価はOpus 5の6割減（$3/$15 per MTok）のため目安は:

- キャッシュ構築を含む質問: **約120円**
- キャッシュ有効中の質問: **約8〜12円**
- 月次上限1万円 → 飛び飛びでも月約80問、連続利用なら数百問に相当

モデル変更時は `config.json` の `model` と `pricing_usd_per_mtok` をセットで更新すること（コスト集計の正確性のため）。`fallback_enabled` はOpus 5／Fable 5系でのみtrueにする（安全クラシファイア拒否時の代替モデル再実行機能）。
