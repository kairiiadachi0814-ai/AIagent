"""経理財務アシスタント Phase 1 — Chatwork常駐ハンドブック参照エージェント。

構成（役割分離）:
- config    : 設定の読み込み（定数のプロパティ管理。コード直書き禁止）
- webhook   : Chatwork Webhookの署名検証とペイロード解析
- handbook  : ハンドブック（Markdown群）の取得と見出し索引
- answer    : Claude APIによる回答生成と出典検証（FR-02/03/04）
- chatwork  : Chatwork REST APIクライアント（送信・取得）
- audit     : 監査ログ（NFR-03）
- cost      : 月次コスト集計・上限停止・アラート（NFR-04）
- handler   : コア処理（基盤非依存）
- app       : HTTPアダプタ（Flask / Cloud Run向け）
- lambda_function : AWS Lambdaアダプタ
"""

__version__ = "0.1.0"
