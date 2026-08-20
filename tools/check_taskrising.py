"""TaskRising（Supabase）への疎通確認とテーブル定義の書き出し。

ボット用の接続情報が用意できたら、まずこれを実行する。
何も書き込まないので、稼働前でも安全に走らせられる。

  python tools/check_taskrising.py            # 疎通確認＋tasksの列を表示
  python tools/check_taskrising.py --all      # 全テーブルの列を表示
  python tools/check_taskrising.py --json out.json   # 定義をファイルへ保存

必要な環境変数（値は表示しない）:
  TASKRISING_API_KEY        Supabaseの公開キー（anon / publishable）
  TASKRISING_BOT_EMAIL      ボット用ユーザーのメールアドレス
  TASKRISING_BOT_PASSWORD   同パスワード
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from raizuinu.config import Config  # noqa: E402
from raizuinu.taskrising import (  # noqa: E402
    TaskRisingClient,
    TaskRisingError,
    required_columns,
    table_columns,
)


def show_table(schema: dict, table: str) -> None:
    columns = table_columns(schema, table)
    if not columns:
        print(f"  [{table}] 定義が見つかりません（テーブル名が違うか、権限がありません）")
        return
    required = set(required_columns(schema, table))
    print(f"  [{table}] {len(columns)}列")
    for name, spec in columns.items():
        kind = spec.get("format") or spec.get("type") or "?"
        note = spec.get("description", "").replace("\n", " ")[:60]
        mark = "必須" if name in required else "    "
        print(f"    {mark} {name:<28} {kind:<24} {note}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="全テーブルの列を表示する")
    parser.add_argument("--json", metavar="PATH", help="テーブル定義をJSONで保存する")
    args = parser.parse_args()

    config = Config.load()
    client = TaskRisingClient(config)

    print(f"接続先: {config.taskrising.get('url')}")
    missing = client.missing_secrets()
    if missing:
        print("環境変数が足りません: " + "／".join(missing))
        print("/etc/raizuinu/env に追記してください（値はここには出しません）")
        return 1

    try:
        client.sign_in()
    except TaskRisingError as exc:
        print(f"ログインできませんでした: {exc}")
        print(
            "ボット用ユーザーがメール＋パスワードで作られているか、"
            "Supabaseの Authentication → Providers で Email が有効かをご確認ください。"
        )
        return 1
    print("ログイン: OK（ボットユーザーとして認証されました）")

    try:
        schema = client.schema()
    except TaskRisingError as exc:
        print(f"テーブル定義を取得できませんでした: {exc}")
        return 1

    definitions = schema.get("definitions") or schema.get("components", {}).get("schemas", {})
    print(f"見えているテーブル: {len(definitions)}件")
    print("  " + "、".join(sorted(definitions)) or "  （なし）")

    tasks_table = str(config.taskrising.get("tasks_table", "tasks"))
    print("\n列の定義:")
    for table in sorted(definitions) if args.all else [tasks_table]:
        show_table(schema, table)

    # 読み取り権限（RLS）の確認。1件だけ取りに行き、中身は表示しない
    print("\n読み取り権限の確認:")
    try:
        rows = client.select(tasks_table, {"select": "id", "limit": "1"})
        print(f"  {tasks_table}: 読めます（{len(rows)}件取得）")
    except TaskRisingError as exc:
        print(f"  {tasks_table}: 読めません → {exc}")
        print("  ボットユーザーに SELECT を許可するRLSポリシーが要ります。")

    if args.json:
        Path(args.json).write_text(
            json.dumps(schema, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"\nテーブル定義を保存しました: {args.json}")

    print("\n※このツールは書き込みを一切行いません。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
