"""メールサーバー（営業メールの受信箱・送信済み）への接続確認。

/etc/raizuinu/env にメールのパスワードを書いたら、まずこれを実行する。
読むだけで、メールサーバーへは何も書き込まない（既読にもしない）。件名も本文も表示しない。
表示するのは、つながったか・フォルダの名前・通数・いちばん新しいメールの日時だけ。

  python tools/check_mailbox.py            # 設定されている受信箱をすべて確かめる
  python tools/check_mailbox.py --name 森  # その人の受信箱だけ

パスワードの環境変数（値は表示しない）は config の mail_watch.mailboxes[].password_env に書いてある名前:
  MAIL_PASSWORD_MORI／MAIL_PASSWORD_FUJITA／MAIL_PASSWORD_FUKUMOTO
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from raizuinu.config import Config  # noqa: E402
from raizuinu.mailbox import AUTH, JST, TLS, MailboxError, MailboxReader  # noqa: E402

STALE_DAYS = 3   # 受信箱のいちばん新しいメールがこれより古ければ、サーバーに残していない疑い
FEW_MESSAGES = 5

HINTS = {
    AUTH: "ユーザー名かパスワードが違います。Thunderbird の「アカウント設定 → サーバー設定」のユーザー名と、"
          "メールのパスワードをご確認ください（画面は送らないでください）。",
    TLS: "サーバーの証明書を検証できません。足立さんの判断が要ります（証明書を更新するか、"
         "証明書の指紋を設定に控えてつなぐか）。",
}


def describe(reader: MailboxReader, folder: dict, label: str) -> datetime | None:
    info = reader.open(folder)
    newest = None
    last = reader.last_uids(1)
    if last:
        newest = reader.newest_date(last[-1])
    when = f"{newest:%Y-%m-%d %H:%M}" if newest else "なし"
    print(f"  {label}: 「{folder['name']}」 {info['messages']}通／いちばん新しいメール {when}")
    return newest


def check(box: dict) -> bool:
    name = str(box.get("name") or box.get("address"))
    variable = str(box.get("password_env") or "")
    print(f"■ {name}（{box.get('address')}／{box.get('host')}:{box.get('port', 993)}）")
    if not os.environ.get(variable):
        print(f"  未設定: 環境変数 {variable} が入っていません（この受信箱は通知ルームの通知で見張ります）")
        return True
    try:
        with MailboxReader(box) as reader:
            print("  接続・ログイン: できました")
            folders = reader.folders()
            print("  フォルダ: " + "、".join(f["name"] for f in folders))
            now = datetime.now(JST)
            newest = describe(reader, reader.inbox(), "受信箱")
            inbox_count = reader.open(reader.inbox())["messages"]
            if newest is None or (now - newest).days >= STALE_DAYS or inbox_count < FEW_MESSAGES:
                print("  【注意】受信箱に新しいメールが残っていません。Thunderbird が POP で受信して"
                      "サーバーから消している可能性があります（その場合、直接は読めません）。")
            sent = reader.sent()
            if not sent:
                print("  【注意】送信済みフォルダが見つかりません。送ったメールがサーバーに残らない設定だと、"
                      "返信の確認は通知ルームの通知に頼ることになります。フォルダ名が上の一覧にあれば、"
                      "設定の sent_folder に書けば読めます。")
            else:
                dates = [d for d in (describe(reader, folder, "送信済み") for folder in sent) if d]
                if not dates or (now - max(dates)).days >= STALE_DAYS * 3:
                    print("  【注意】送信済みに最近のメールがありません。送ったメールが PC にだけ保存されている"
                          "可能性があります。")
            print(f"  振り分け先として読むフォルダ: {len(reader.others())}個")
        return True
    except MailboxError as exc:
        print(f"  できませんでした: {exc}")
        if exc.kind in HINTS:
            print("  " + HINTS[exc.kind])
        return False


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="メールサーバーへの接続確認（読むだけ）")
    parser.add_argument("--name", help="この名前の受信箱だけ確かめる")
    args = parser.parse_args()
    boxes = Config.load().mail_watch.get("mailboxes") or []
    if args.name:
        boxes = [b for b in boxes if b.get("name") == args.name]
    if not boxes:
        print("確かめる受信箱がありません（config の mail_watch.mailboxes）")
        return 1
    results = [check(box) for box in boxes]
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
