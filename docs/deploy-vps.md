# デプロイ構築記録 — さくらのVPS（2026-08-13構築）

経理財務アシスタントPhase 1の本番環境。要件定義書11章の実行基盤決定（版0.3）に対応する構築時点の記録（as-built）。

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
| systemdユニット | `/etc/systemd/system/raizuinu.service`（Webhook本体）、`raizuinu-selfreview.{service,timer}`（週次自己分析）、`raizuinu-watcher.{service,timer}`（議論ウォッチャー） |
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

## マニュアル更新報告の受付フロー（2026-08-13追加）

メンバーがChatworkで経理財務アシスタントに「◯◯のマニュアルを更新した」とメンションすると：

1. 経理財務アシスタントが更新報告と判定し、受付リスト `/var/lib/raizuinu/pending_updates.jsonl` に追記
2. 報告者へ受領を返信し、管理者ルーム（報告ルームと別の場合）へ通知
3. 実際の反映は管理者作業: Claude Codeで「マニュアル更新を反映して」と依頼
   → 受付リストを読み取り→Drive差分検知→再転記→コミット→デプロイ→処理済みエントリをリストから除去

受付リストの確認コマンド:

```
ssh -i C:\Users\admin\.ssh\raizuinu_vps ubuntu@tk2-262-40529.vs.sakura.ne.jp "sudo cat /var/lib/raizuinu/pending_updates.jsonl 2>/dev/null || echo '(受付なし)'"
```

## 自己改善サイクル（承認制・2026-08-13追加）

1. **フィードバック受付**: 利用者が回答の誤りを指摘すると `/var/lib/raizuinu/feedback.jsonl` に記録され、管理者ルームへ通知
2. **週次自己分析**: systemdタイマー（`raizuinu-selfreview.timer`、毎週月曜9:00 JST）が
   `python -m raizuinu.selfreview` を実行。直近7日の監査ログ・フィードバック・未反映の
   更新報告を集計し、改善提案レポートを管理者ルームへ投稿（`/var/lib/raizuinu/reports/` にも保存）
3. **反映は承認制**: レポートは提案のみで何も変更しない。管理者がClaude Codeで
   「自己分析レポートの提案◯番を反映して」と指示 → 実装・テスト・レビューのうえ反映

手動実行:

```
ssh -i C:\Users\admin\.ssh\raizuinu_vps ubuntu@tk2-262-40529.vs.sakura.ne.jp "sudo systemctl start raizuinu-selfreview.service && sudo journalctl -u raizuinu-selfreview -n 5 --no-pager"
```

## 議論ウォッチャー（2026-08-13追加）

systemdタイマー（`raizuinu-watcher.timer`、5分ごと）が `python -m raizuinu.watcher` を実行し、
`config.json` の `discussion_watch.room_ids` のルームを巡回する。

1. **差分取得**: 前回巡回以降の新規発言のみ評価（既読位置は `/var/lib/raizuinu/watch-{room_id}.json`）。
   初回はその時点までを既読化するだけで評価しない。ボット宛メンション・自分の発言・10文字未満は除外
2. **2段階判定**: 1段目は軽量スクリーニング（ハンドブックなし・effort low）で「誤りの疑い」だけを検出。
   疑いがある場合のみ2段目で通常のQ&Aパイプライン（出典検証・URLスクラブ・e-Govツール込み）により裏取り
3. **沈黙がデフォルト**: 裏取りできた「明らかな誤り」のみ助言。意見の相違・雑談には介入しない。
   介入は1ルームあたり1日3回まで。同じ出典による再介入は同日中は抑止
4. **見習いモード**（`discussion_watch.mode: "shadow"`・現在の設定）: ルームには投稿せず、
   管理者ルームへ助言候補を内報。精度確認後 `"live"` に変更（＋デプロイ）するとルームへ直接投稿
5. 介入時のみ監査ログ（`type: watch_intervention`）へ記録。月次コスト上限到達時は巡回停止

運用コマンド:

```
# タイマー状態・直近の実行結果
ssh -i C:\Users\admin\.ssh\raizuinu_vps ubuntu@tk2-262-40529.vs.sakura.ne.jp "systemctl list-timers raizuinu-watcher.timer --no-pager && sudo journalctl -u raizuinu-watcher -n 20 --no-pager"

# 手動で1回実行
ssh -i C:\Users\admin\.ssh\raizuinu_vps ubuntu@tk2-262-40529.vs.sakura.ne.jp "sudo systemctl start raizuinu-watcher.service"
```

コスト目安: スクリーニングはハンドブックを読まないため1回≈0.1〜0.5円。発言が無い巡回はAPI呼び出しゼロ（Chatwork取得のみ）。
2段目の裏取りが走った場合のみ通常質問と同等（キャッシュ有効中≈8〜12円）。

## 補助金情報の参照回答（2026-08-14追加）

補助金・助成金・支援制度の質問は、一般会計知識（freee）と同じ2段構えで回答する:

1. 1段目でハンドブックの「補助金リンク集_ミラサポplus」から該当ページURLを特定
2. 2段目でweb_fetch（`mirasapo-plus.go.jp` 限定）により当日の掲載内容を取得して要約。
   末尾に「※ミラサポplusの掲載情報に基づく。申請前に公式サイト・公募要領を確認」の免責文を必ず付ける

リンク集は主要ページの索引（人気の補助金8種・基礎知識・経営課題別・チラシ集など約25件）。
ミラサポplusのサイト改編でURLが変わった場合は `補助金リンク集_ミラサポplus.md` を更新して再デプロイする。
旧・制度ナビAPI（jirei-seido-api.mirasapo-plus.go.jp）は2026-08時点で提供終了（DNS消滅）を確認済みのため、ページ参照方式を採用している。

リンク切れの検知（2段構え）:

1. 回答時のページ取得失敗は監査ログに `stage2: fetch_failed` として記録される
2. 週次自己分析（raizuinu-selfreview）が、取得失敗したURLの集計と、補助金リンク集全URLの
   ヘルスチェック（HTTP 4xx・トップページへの転送の検出）をレポートに含め、管理者ルームへ通知する

## 税法の参照回答（国税庁。2026-08-17追加）

印紙税額・源泉徴収税率・インボイス・交際費などの質問は、`税法リンク集_国税庁.md`（94件）から
該当ページを特定し、web_fetch（`www.nta.go.jp` 限定）で当日の内容を取得して回答する。

- 税額・税率の質問では**国税庁を最優先**（freee・ミラサポより先）。記憶での税額回答は禁止
- 税額表の要約では、該当区分と金額の帯（「○円を超え○円以下」）を省略せず正確に記載させる
- 免責文は「※国税庁の掲載情報に基づきます。…原文の確認および税理士へのご相談を」
- 改正・年度更新でURLが変わるため、週次自己分析のリンク切れ検知（`fetch_failed`）で監視する。
  特に源泉徴収税額表は年度でURLが変わる（ディレクトリ名の数字と和暦が一致しないため要手動更新）

## 文書つき雑務依頼（議事録作成・会議まとめ。2026-08-14追加）

メンバーが会議の文字起こし等をルームに添付（.docx／.txt等）またはGoogleドキュメント／
スプレッドシートのURLを貼ってメンションすると、議事録作成・要約・宿題の抽出などを行う。

- 判定はコード側で決定的に行う: メンション本文に対応形式の添付（`[download:ID]`）か
  docs.google.comのURLがあれば文書タスク。画像・PDF等の添付だけの通常質問はQ&Aのまま。
  引用（[qt]）内の添付・URLと、ハンドブック記載の原本マニュアルURLは対象外。
  過去メッセージの添付は「議事録」「要約」等のキーワードを含む依頼のときだけ遡って使う
- 複数ファイル添付は最大5件まで連結して1回で処理（超過分は案内）。Wordの変更履歴で
  削除されたテキスト・フィールドコード・テキストボックスの重複は抽出時に除外する
- 文書内の指示文には従わない（プロンプトインジェクション対策）。文書内URLは返信へ転記しない
- ハンドブック・プロンプトキャッシュを使わない軽量フロー（コストは文書の長さ次第。
  文字起こし3万字で1回あたり約20〜40円）
- GoogleドキュメントはURLから直接読み込む。閲覧権限が必要な設定の場合は
  「リンクを知っている全員が閲覧可」への変更か、Wordファイルでの添付を案内する
- 監査ログは `type: doc_task`。設定は `config.json` の `doc_task`（enabled／max_file_mb／max_text_chars）

## コストの実測・目安

claude-opus-5での実測（2026-08-13、変更前）:

- 初回質問（プロンプトキャッシュ構築を含む）: **約202円**（ハンドブック全文≈12.8万トークンの1時間キャッシュ書き込み）
- キャッシュ有効中（直前の質問から1時間以内）の質問: **約15〜20円**

2026-08-13に **claude-sonnet-5へ変更**（`config.json`のmodel＋単価表を更新）。単価はOpus 5の6割減（$3/$15 per MTok）のため目安は:

- キャッシュ構築を含む質問: **約120円**
- キャッシュ有効中の質問: **約8〜12円**
- 月次上限1.5万円（2026-08-13に1万円から引き上げ。議論ウォッチャー追加に伴う） → 飛び飛びでも月約120問、連続利用なら数百問に相当

モデル変更時は `config.json` の `model` と `pricing_usd_per_mtok` をセットで更新すること（コスト集計の正確性のため）。`fallback_enabled` はOpus 5／Fable 5系でのみtrueにする（安全クラシファイア拒否時の代替モデル再実行機能）。
