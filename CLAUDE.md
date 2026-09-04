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
- ひな形からの書類作成は `docbuild.py`（依頼→項目抽出→書式と差出人会社の決定）と `templatefill.py`（Word/Excelへの差し込み。標準ライブラリのみ）。ひな形の実体は `templates/`。Wordは書式ごとに1つだけ持ち（`送付状_標準.docx`／`送付状_楽天軒型.docx`）、差出人の会社情報は `templates/companies.json` から差し込む。**グループ会社が増えたときは companies.json に1件足すだけ**（ひな形は増やさない）。Boxの原本から白紙化して作り直すには `python tools/build_templates.py`（ローカルPC専用）
- 定型の受け答えの揺らぎは `phrasing.py`。コード側で書いている一言（「下書きを作成しました」等）を同義の候補から毎回選び直し、前回と同じ言い方を避ける。**揺らいでよいのは社交辞令だけ**で、回答本文・出典・免責文・金額・他部署へ送る文面は対象外。各候補が呼び出し側の判定（未来形を弾く等）を満たすことを `tests/test_phrasing.py` で固定してある。`config: phrasing.vary_openings` を false にすると固定へ戻る
- TaskRising（社内タスク管理）への登録は `taskrising.py`。実体はSupabase（PostgREST）で独自APIは無い。**service_role キーは使わず**、専用ボットユーザー（メール＋パスワード）でログインしてRLSの範囲で読み書きする。**経費支払いタスク・振込用CSV等が揃うまで `taskrising.enabled` は false のまま**。接続確認とテーブル定義の書き出しは `python tools/check_taskrising.py`（書き込みはしない）
- チャットの過去ログからの回答は `chatlog.py`。ChatworkのAPIは**直近100件しか返さない**（limit・before・offsetが効かない）ため、巡回のたびにSQLite（`state/chatlog.sqlite3`）へ書き写して積み上げる。ハンドブックに記載が無い質問のときだけ引き、**検索は質問が来たルーム内のみ**。同じ項目が複数あれば最新を正とし、過去ログから答えた旨を返信に必ず書く。パスワード等の置き場を増やさないよう**監査ログに回答本文は残さない**（要件定義書 版0.15・ガードレール6）
- 添付文書の読み取りは `doctask.py`。一度読んだ文書は `DocumentMemory` がルーム単位で覚え、**その話題への返信か、ファイル名の名指しのときだけ**思い出す（会話の途中から入った人が添付し直さずに済む。無関係な質問に文書を持ち出さないよう条件を絞ってある）
- レターパックの手配取次ぎは `letterpack.py`。`LetterpackRunner` が依頼者とのやり取り（要否→文面確認→投稿）、`LetterpackFollower` が総務からの返信の巡回（watcherのタイマーに相乗り）。送り先は**差出人の会社ごとに変わる**（`letterpack.routes` を company_id で引く。ライズ系＝総務の備品ルーム、楽天軒＝楽天軒備品チャットの経理財務部4名）。会社が増えたら routes に1件足すだけ。催促の経過時間は営業時間（平日9〜18時、土日祝を除く）で数える。祝日表は `config/holidays.json`（`python tools/build_holidays.py 2029 2030` で作り直す）。投稿先の備品ルームは **`allowed_room_ids` に入れない**（Q&Aの対象外にして、他部署のルームへ社内ナレッジが流れる経路を作らない）
- FAX受信の見張りは `faxwatch.py`。`FaxWatcher` が通知ルーム（`fax_watch.room_id`）を巡回し、通知ボット（通知管理くん）の「FAX受信通知」とPDF添付を**ファイル名で対にして**PDFを読み、差出人と種類（発注書なら発注内容も）を `notify_account_ids` あてに投稿する。To を付けるのは `mention_kinds`（発注書・注文書・請求書・見積書・納品書）だけで、案内・広告・その他は To 無しでルームに置くだけ（営業・迷惑FAXで通知を鳴らさない）。差出人は**FAX番号台帳（Googleスプレッドシートの公開CSV）を正**とし、番号が無い・台帳に無いときだけPDFの記載から拾って根拠を書く。取引先が増えたらスプレッドシートに1行足すだけ。初回起動は既読にするだけで過去分は流さない。`max_per_day` で1日の処理数を切る。知らせるのは**月〜金の 8:30〜19:30**（`notify_window`）だけで、時間外・土日に届いた分は `pending` に控えて次の時間帯の頭に知らせる（APIが直近100件しか返さないため、控えないと週末に取りこぼす）。**祝日は平日と同じ扱い**（土日だけ休む。レターパックの営業時間とは違うので注意）。発注書・注文書は `open` に控え、「対応完了」等の返事（`is_completion`）が無ければ翌営業日 9:00 に済んだかを聞き、それにも返事が無ければ 12:00 にもう一度だけ聞いて打ち切る（`follow_up`）。完了の報告には相手のメッセージへの返信で一言お礼を返す（`phrasing.py` の `fax_done_thanks`。「完了」「済」を含めないこと。自分の投稿で発注書を閉じてしまうため）。通知ルームは **`allowed_room_ids` に入れない**。宛先はテスト中は篠田・足立、**テスト完了後に篠田・福本・中浦・札葉へ差し替える予定**（config.json の `_note` 参照）
- ハンドブック転記ファイル（63件）は現状リポジトリ直下のフラット構成。参照ルートは `config/config.json` の `handbook.roots` で管理し、将来 `handbook/`・`sops/` 階層へ移す場合も設定変更のみで追従する
- 11章未決事項の推奨案（暫定採用）は `docs/phase1-requirements.md` 11章と `config/config.json` に記録
- 秘密情報は環境変数のみ: `ANTHROPIC_API_KEY`／`CHATWORK_API_TOKEN`／`CHATWORK_WEBHOOK_TOKEN`
- テストは `python -m pytest tests/`。ローカル動作確認は `python run_local.py "<質問>"`
