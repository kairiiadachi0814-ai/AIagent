"""Box（読み取り用アプリ）への接続確認。

/etc/raizuinu/env に Box の3つの値を書いたら、まずこれを実行する。
読むだけで、Box へは何も書き込まない（ダウンロードした中身も保存しない）。

  python tools/check_box.py                       # 接続・招待フォルダの一覧・ダウンロードの試し
  python tools/check_box.py --path "2026年10月報告/ライズクリエイション"   # その下を見る
  python tools/check_box.py --no-download         # ダウンロードは試さない

必要な環境変数（値は表示しない）:
  BOX_CLIENT_ID       アプリのクライアントID
  BOX_CLIENT_SECRET   アプリのクライアントシークレット
  BOX_ENTERPRISE_ID   アプリを作ったアカウントの Enterprise ID
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from raizuinu.boxread import TYPE_FILE, TYPE_FOLDER, BoxClient, BoxError, write_permissions  # noqa: E402
from raizuinu.config import Config  # noqa: E402

MAX_LISTED = 20  # 一覧に名前を出す件数
MAX_FOLDERS_SEARCHED = 12  # ダウンロードを試すファイルを探して開くフォルダの数
MAX_TEST_BYTES = 5 * 1024 * 1024  # 試しに取るファイルの大きさの上限

TOKEN_HINTS = {
    "invalid_client": "BOX_CLIENT_ID か BOX_CLIENT_SECRET が違っています。開発者コンソールの値を貼り直してください。",
    "invalid_grant": "BOX_ENTERPRISE_ID が違うか、アプリがまだ承認されていません。"
                     "開発者コンソール右上のアイコン →「Enterprise IDをコピー」の値をご確認ください。",
    "unauthorized_client": "アプリの認証方法が「クライアント資格情報許可（Client Credentials Grant）」になっているかをご確認ください。",
}


def explain_token_error(exc: BoxError) -> str:
    return TOKEN_HINTS.get(exc.code, "3つの値（クライアントID・クライアントシークレット・Enterprise ID）を貼り直してみてください。")


def find_test_file(client: BoxClient, folder_id: str) -> tuple[dict | None, int]:
    """ダウンロードを試す小さなファイルを、上のフォルダから順に探す。(ファイル, 開いたフォルダの数)"""
    queue, opened = [folder_id], 0
    while queue and opened < MAX_FOLDERS_SEARCHED:
        items = client.items(queue.pop(0))
        opened += 1
        for item in items:
            if item.get("type") == TYPE_FILE and 0 < int(item.get("size") or 0) <= MAX_TEST_BYTES:
                return item, opened
        queue.extend(str(i["id"]) for i in items if i.get("type") == TYPE_FOLDER and i.get("id"))
    return None, opened


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", default="", help="招待フォルダの下の、見たい場所（「/」区切り）")
    parser.add_argument("--no-download", action="store_true", help="ダウンロードを試さない")
    args = parser.parse_args()

    config = Config.load()
    client = BoxClient(config)

    missing = client.missing_secrets()
    if missing:
        print("環境変数が足りません: " + "／".join(missing))
        print("/etc/raizuinu/env に追記してください（値はここには出しません）")
        return 1

    try:
        client.token()
    except BoxError as exc:
        print(f"Boxに入れませんでした: {exc}")
        print("→ " + explain_token_error(exc))
        return 1
    print("認証: OK（アプリとしてBoxに入れました）")

    try:
        me = client.me()
        print(f"利用者: {me.get('name')}（{me.get('login')}）")
    except BoxError as exc:
        print(f"利用者の情報を取得できませんでした: {exc}")

    try:
        root = client.folder(client.root_folder_id)
    except BoxError as exc:
        print(f"招待フォルダを開けませんでした: {exc}")
        if exc.status in (403, 404):
            print("→ サービスアカウントへの招待が届いていないか、フォルダが違います（config: box.root_folder_id）。")
        return 1
    print(f"招待フォルダ: {root.get('name')}（ID {root.get('id')}）")

    writable = write_permissions(root)
    if writable is None:
        print("このフォルダでの権限: 確かめられませんでした")
    elif writable:
        print("【注意】このフォルダで書き込みができる役割になっています: " + "・".join(writable))
        print("→ Boxの共有設定で、サービスアカウントの役割を「ビューアー」へ下げてください。")
    else:
        can_download = (root.get("permissions") or {}).get("can_download")
        print("このフォルダでの権限: 書き込み不可（アップロード・名前の変更・削除・招待はできません）"
              + ("／ダウンロード可" if can_download else "／ダウンロード不可（役割がプレビューアーになっていませんか）"))

    target = root
    if args.path:
        try:
            target = client.find(str(root["id"]), args.path)
        except BoxError as exc:
            print(f"「{args.path}」をたどれませんでした: {exc}")
            return 1
        if target is None:
            print(f"「{args.path}」が見つかりません。")
            return 1
        print(f"見る場所: {args.path}")

    if target.get("type", TYPE_FOLDER) == TYPE_FOLDER:
        try:
            items = client.items(str(target["id"]))
        except BoxError as exc:
            print(f"一覧を取得できませんでした: {exc}")
            return 1
        folders = [i for i in items if i.get("type") == TYPE_FOLDER]
        files = [i for i in items if i.get("type") == TYPE_FILE]
        print(f"中身: フォルダ {len(folders)}件・ファイル {len(files)}件")
        for item in (folders + files)[:MAX_LISTED]:
            mark = "📁" if item.get("type") == TYPE_FOLDER else "　"
            size = f"（{int(item.get('size') or 0):,} バイト）" if item.get("type") == TYPE_FILE else ""
            print(f"  {mark} {item.get('name')}{size}")
        if len(items) > MAX_LISTED:
            print(f"  …ほか {len(items) - MAX_LISTED}件")

    if not args.no_download:
        print("\nダウンロードの確認:")
        try:
            if target.get("type") == TYPE_FILE:
                test, opened = target, 0
            else:
                test, opened = find_test_file(client, str(target["id"]))
            if test is None:
                print(f"  試せるファイルが見つかりませんでした（フォルダ {opened}個を見ました）。--path で場所を指定してください。")
            else:
                content = client.download(str(test["id"]), int(test.get("size") or 0))
                print(f"  OK: {test.get('name')}（{len(content):,} バイトを取得。中身は保存していません）")
        except BoxError as exc:
            print(f"  ダウンロードできませんでした: {exc}")
            if exc.status == 403:
                print("  → アプリのスコープ「…への書き込み」（ダウンロードに必要）にチェックがあるか、"
                      "招待の役割が「ビューアー」かをご確認ください。")
            return 1

    print("\n※このツールは書き込みを一切行いません。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
