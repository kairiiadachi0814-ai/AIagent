# Claude Code 開発コンテキスト

このリポジトリは、Chatwork常駐型の社内AIエージェント（経理財務アシスタント）を開発するためのものである。

## 必読

実装前に `docs/phase1-requirements.md` を読むこと。
特に「7. ガードレール」は実装で必ず担保する。

## Phase 1 で実装するもの

Chatworkの許可ルームでメンションを受けたら、`handbook/` と `sops/` を参照し、
出典（ファイル名・見出し）付きで回答を返す。

書き込み系の操作は、依頼者のいる許可ルームへ**下書きを添付する**ことだけに限る
（版0.13で解禁。書類送付状・FAX送付状の作成）。社外への送付・提出・登録は実装しない。

## コーディング方針

- 繰り返し使える構造にする
- 定数は定数化・プロパティ管理する（ルーム許可リスト、参照件数、モデル名等をコードに直書きしない）
- 重複処理は共通関数に集約する
- 修正が1か所で完結するようにする
- 役割を明確に分離する（Webhook受信／ハンドブック取得／回答生成／Chatwork送信）

## 禁止事項

- APIトークン・キーのコードへの直書き
- 出典なしの回答を返す実装
- 許可リスト外のルームへの応答

## 実装構成（Phase 1・2026-08-13時点）

- `src/raizuinu/` にコア実装（config／webhook／handbook／answer／chatwork／audit／cost／handler／app／lambda_function）。役割はパッケージdocstring参照
- ひな形からの書類作成は `docbuild.py`（依頼→項目抽出→ひな形選択）と `templatefill.py`（Word/Excelへの差し込み。標準ライブラリのみ）。ひな形の実体は `templates/`。Boxの原本から白紙化して作り直すには `python tools/build_templates.py`（ローカルPC専用）
- ハンドブック転記ファイル（63件）は現状リポジトリ直下のフラット構成。参照ルートは `config/config.json` の `handbook.roots` で管理し、将来 `handbook/`・`sops/` 階層へ移す場合も設定変更のみで追従する
- 11章未決事項の推奨案（暫定採用）は `docs/phase1-requirements.md` 11章と `config/config.json` に記録
- 秘密情報は環境変数のみ: `ANTHROPIC_API_KEY`／`CHATWORK_API_TOKEN`／`CHATWORK_WEBHOOK_TOKEN`
- テストは `python -m pytest tests/`。ローカル動作確認は `python run_local.py "<質問>"`
