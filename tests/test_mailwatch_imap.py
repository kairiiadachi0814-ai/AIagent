"""営業メールの見張り — メールサーバーを直接読む経路。

指示（2026-11-24）: 通知ルームの通知（本文の先頭約400字・宛先なし）ではなく、メールサーバーを直接確認する。
全文・宛先（To／CC）・返信関係のIDで、担当者・期日・返信を確かめる。直接読めない箱のメールと、
サーバーにつながらない間は、これまでどおり通知ルームの通知で見張る。
"""

import hashlib
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from raizuinu.config import Config
from raizuinu.mailbox import AUTH, FOLDER, MailboxError, parse_message
from raizuinu.mailwatch import ANSWERED, JST, MailWatcher

from test_mailbox import build
from test_mailwatch import (
    FUJITA,
    ME,
    MORI,
    NOTIFIER,
    ROOM,
    SHINODA,
    THU_1000,
    FakeChatwork,
    FakeModel,
    check,
    notice,
    read,
)

ADMIN_ROOM, ADACHI = 444945031, 6945415
INBOX = {"raw": "INBOX", "name": "INBOX", "flags": [], "sep": ""}
SENT = {"raw": "INBOX.Sent", "name": "INBOX.Sent", "flags": ["\\Sent"], "sep": "."}
SENT_JP = {"raw": "INBOX.&kAFP4W4IMH8-", "name": "INBOX.送信済み", "flags": [], "sep": "."}
SORTED = {"raw": "INBOX.JR", "name": "INBOX.ジェイアール西日本", "flags": [], "sep": "."}
BROKEN = {"raw": "INBOX.mailfilter-like", "name": "INBOX.開けないフォルダ", "flags": [], "sep": "."}
KEYS = {"INBOX": "inbox", "INBOX.Sent": "sent", SENT_JP["raw"]: "sent2", "INBOX.JR": "sorted"}


class FakeServer:
    """メールサーバーの差し替え。箱の名前 → フォルダ → {UID: (メール, サーバーに入った日時)}。"""

    def __init__(self):
        self.boxes, self.opened, self.fail, self.next_uid = {}, [], {}, 100

    def put(self, name, folder, raw, at=None):
        self.next_uid += 1
        self.boxes.setdefault(name, {})
        self.boxes[name].setdefault(folder, {})[self.next_uid] = (raw, at or THU_1000)
        return self.next_uid

    def reader(self, box):
        return FakeReader(self, box["name"])


class FakeReader:
    def __init__(self, server, name):
        self.server, self.name, self.folder = server, name, "inbox"

    def __enter__(self):
        self.server.opened.append(self.name)
        if self.server.fail.get(self.name):
            raise MailboxError(AUTH, f"{self.name} のメールにログインできませんでした")
        self.server.boxes.setdefault(self.name, {})
        return self

    def __exit__(self, *exc_info):
        return None

    def inbox(self):
        return INBOX

    def sent(self):
        return [SENT_JP, SENT]

    def others(self):
        return [SORTED, BROKEN]

    def open(self, folder):
        if folder is BROKEN:
            raise MailboxError(FOLDER, "開けませんでした")
        self.folder = KEYS[folder["raw"]]
        mails = self.server.boxes[self.name].setdefault(self.folder, {})
        return {"messages": len(mails), "uidvalidity": 7, "uidnext": max(mails, default=0) + 1}

    def uids_after(self, after):
        return sorted(u for u in self.server.boxes[self.name][self.folder] if u > after)

    def last_uids(self, count):
        return sorted(self.server.boxes[self.name][self.folder])[-count:] if count else []

    def fetch(self, uid):
        raw, at = self.server.boxes[self.name][self.folder][uid]
        mail = parse_message(raw, received=at)
        mail["uid"] = uid
        return mail


@pytest.fixture
def env(tmp_path, monkeypatch):
    config = Config.load(tmp_path / "no-config.json")
    config.base_dir = tmp_path
    config.data["state_dir"] = str(tmp_path / "state")
    config.data["audit_log_dir"] = str(tmp_path / "audit")
    config.data["admin_room_id"] = ADMIN_ROOM
    config.data["admin_account_ids"] = [ADACHI]
    config.data["mail_watch"].update({
        "enabled": True, "room_id": ROOM, "notifier_account_id": NOTIFIER,
        "own_domains": ["rakutenken.co.jp", "rakutenken.jp"],
        "skip_senders": ["superdelivery.com"],
        "staff": [
            {"account_id": MORI, "name": "森", "addresses": ["ymori@rakutenken.co.jp"]},
            {"account_id": FUJITA, "name": "藤田", "addresses": ["fujita@rakutenken.jp"]},
        ],
        "mailboxes": [
            {"name": "森", "address": "ymori@rakutenken.co.jp", "host": "sv1151.wpx.ne.jp", "password_env": "MAIL_PASSWORD_MORI"},
            {"name": "藤田", "address": "fujita@rakutenken.jp", "host": "mail.rakutenken.jp", "password_env": "MAIL_PASSWORD_FUJITA"},
        ],
    })
    monkeypatch.setenv("MAIL_PASSWORD_MORI", "x")
    monkeypatch.setenv("MAIL_PASSWORD_FUJITA", "x")
    cw, model, server = FakeChatwork(), FakeModel(), FakeServer()
    clock = SimpleNamespace(now=THU_1000)
    watcher = MailWatcher(config, cw, client=model, now=lambda: clock.now, reader_factory=server.reader)
    server.put("森", "inbox", build(subject="前からあるメール", message_id="<old@x>"))
    cw.add(SHINODA, "テスト")
    watcher.run_once()   # 通知ルームの初回（既読にするだけ）
    watcher.run_once()   # メールサーバーの初回（いまあるメールを既読として控えるだけ）
    model.calls.clear()
    return SimpleNamespace(config=config, cw=cw, model=model, server=server, clock=clock, watcher=watcher, tmp=tmp_path)


def state_of(env):
    return json.loads((env.tmp / "state" / "mailwatch.json").read_text(encoding="utf-8"))


def room_posts(env):
    return [body for room, body in env.cw.sent if room == ROOM]


def mid_of(subject):
    """件名から作る、そのメールのID（ASCII）。"""
    return f"<{hashlib.md5(subject.encode('utf-8')).hexdigest()[:12]}@takashimaya>"


REQUEST_ID = mid_of("休業日のご確認")


def incoming(env, box="森", subject="休業日のご確認", body="森様\n11月27日(金)までにご返信ください。", at=None, **kw):
    found = kw.pop("found", None) or read(staff=["森"], partner="株式会社髙島屋 山木様", ask="休業日を回答してほしい")
    env.model.reads[subject] = found
    kw.setdefault("message_id", mid_of(subject))
    return env.server.put(box, "inbox", build(subject=subject, body=body, **kw), at)


# --- 届いたメール ---


def test_what_is_already_on_the_server_is_not_announced(env):
    assert env.cw.sent == [] and env.model.calls == []
    place = state_of(env)["imap"]["森"]["inbox"]
    assert place["last_uid"] == 101 and place["uidvalidity"] == 7


def test_a_new_mail_on_the_server_is_read_in_full_and_announced(env):
    long_tail = "\n".join(f"{n}行目の説明です。" for n in range(60)) + "\n提出締め切り：11/27(金)まで"
    env.model.reads["休業日のご確認"] = read(staff=["森"], partner="株式会社髙島屋 山木様", ask="休業日を回答してほしい",
                                       quote="11/27(金)まで", day="2026-11-27")
    env.server.put("森", "inbox", build(body="森様\nご記入のうえご返信ください。\n" + long_tail, attach=["リスト.xlsx"]))
    assert env.watcher.run_once() == 1
    body = room_posts(env)[0]
    assert body.startswith(f"[To:{MORI}]森さん\n取引先からメールが届いています。")
    assert "① 株式会社髙島屋 山木様　→ 森さん" in body
    assert "期日: 11月27日（金）・あと8日　原文「11/27(金)まで」" in body  # 400字より後ろに書かれた期日
    assert "添付1件" in body and "メール: " not in body  # 通知ルームに通知が無ければリンクは付けない
    sent_to_model = env.model.calls[0][1]
    assert "宛先（To）: ymori@rakutenken.co.jp" in sent_to_model and "提出締め切り：11/27(金)まで" in sent_to_model
    item = state_of(env)["open"][0]
    assert item["source"] == "imap" and item["message_id"].startswith("<") and item["id"].startswith("imap:森:inbox:")


def test_a_mailbox_without_a_password_is_left_to_the_room_notices(env, monkeypatch):
    monkeypatch.delenv("MAIL_PASSWORD_FUJITA")
    env.server.opened.clear()
    env.watcher.run_once()
    assert env.server.opened == ["森"]


def test_without_a_salutation_the_mail_headers_decide_who_is_told(env):
    nobody = read(staff=[], addressee="お取引先様 各位", partner="阪急阪神百貨店 橋新様")
    incoming(env, subject="フードイベント", body="お取引先様 各位\nご確認ください。", found=nobody,
             to="fujita@rakutenken.jp", cc="ymori@rakutenken.co.jp", box="藤田")
    env.watcher.run_once()
    body = room_posts(env)[0]
    assert body.startswith(f"[To:{FUJITA}]藤田さん\n") and "→ 藤田さん（メールの宛先）" in body

    incoming(env, subject="CCの共有", body="各位\nご確認ください。", found=nobody,
             to="someone@example.co.jp", cc="ymori@rakutenken.co.jp", box="森", at=datetime(2026, 11, 19, 10, 5, tzinfo=JST))
    incoming(env, subject="BCCの一斉連絡", body="各位\nご確認ください。", found=nobody,
             to="list@example.co.jp", box="藤田", at=datetime(2026, 11, 19, 10, 6, tzinfo=JST))
    env.watcher.run_once()
    body = room_posts(env)[1]
    assert "→ 森さん（CC）" in body and "→ 藤田さん（受信箱）" in body


def test_the_salutation_in_the_body_comes_before_the_headers(env):
    incoming(env, to="fujita@rakutenken.jp, ymori@rakutenken.co.jp", box="藤田")
    env.watcher.run_once()
    assert "→ 森さん" in room_posts(env)[0] and state_of(env)["open"][0]["recipients"] == [MORI]


def test_a_mail_delivered_to_two_mailboxes_is_announced_once(env):
    incoming(env, box="森", to="ymori@rakutenken.co.jp, fujita@rakutenken.jp")
    incoming(env, box="藤田", to="ymori@rakutenken.co.jp, fujita@rakutenken.jp")
    assert env.watcher.run_once() == 1
    assert len(room_posts(env)) == 1 and len(env.model.calls) == 1


def test_the_same_mail_seen_on_the_server_and_in_the_room_is_announced_once(env):
    incoming(env, sender="yamaki@ad.takashimaya.co.jp")
    mid = env.cw.mail("yamaki@ad.takashimaya.co.jp", "休業日のご確認", "森様\n11月27日(金)までに", received="2026-11-19 09:58")
    assert env.watcher.run_once() == 1
    body = room_posts(env)[0]
    assert len(room_posts(env)) == 1 and len(env.model.calls) == 1
    assert f"メール: https://www.chatwork.com/#!rid{ROOM}-{mid}" in body  # 通知があれば、その投稿へのリンクを添える


def test_a_notice_that_comes_later_is_linked_afterwards(env):
    incoming(env, sender="yamaki@ad.takashimaya.co.jp")
    env.watcher.run_once()
    mid = env.cw.mail("yamaki@ad.takashimaya.co.jp", "休業日のご確認", "森様", received="2026-11-19 10:02")
    env.watcher.run_once()
    assert len(room_posts(env)) == 1 and state_of(env)["open"][0]["link_id"] == mid


def test_a_mail_only_the_room_shows_is_still_announced(env):
    # 直接読めない箱（福本さんあて）に届いたメールは、これまでどおり通知で知らせる
    env.model.reads["確約書"] = read(staff=[], addressee="福本さま", partner="株式会社郵便局物販サービス 中谷様")
    mid = env.cw.mail("y.nakatani@jp-ts.jp", "確約書の修正依頼", "福本さま\n修正をお願いします。")
    assert env.watcher.run_once() == 1
    assert f"#!rid{ROOM}-{mid}" in room_posts(env)[0]
    assert state_of(env)["open"][0]["source"] == "chatwork"


def test_ads_on_the_server_are_dropped_without_reading_them(env):
    env.server.put("森", "inbox", build(sender="support@superdelivery.com", subject="【特集】", message_id="<ad@x>"))
    assert env.watcher.run_once() == 0 and env.model.calls == []
    assert state_of(env)["imap"]["森"]["inbox"]["last_uid"] == 102  # 既読位置は進む


# --- 振り分け先のフォルダ ---


def test_a_mail_sorted_straight_into_a_folder_is_still_announced(env):
    # 実例（2026-11-24）: 森さんの箱では、一部の差出人のメールが届くとすぐ取引先別のフォルダへ振り分けられていた
    env.model.reads["売上見込"] = read(staff=[], addressee="お取引先様 各位", needs_reply=False, ask="",
                                   partner="ジェイアール西日本デイリーサービスネット 榎並様")
    env.server.put("森", "sorted", build(subject="【情報共有】売上見込について", body="お取引先様 各位\n共有します。",
                                        to="list@dailyservice.co.jp", message_id="<jr@x>"))
    assert env.watcher.run_once() == 1
    assert "→ 森さん（受信箱）" in room_posts(env)[0]
    assert state_of(env)["imap"]["森"]["folder:INBOX.JR"]["last_uid"] == env.server.next_uid


def test_a_mail_moved_to_a_folder_after_it_was_announced_is_not_announced_again(env):
    incoming(env)
    env.watcher.run_once()
    env.server.put("森", "sorted", build(subject="休業日のご確認", body="森様\n11月27日(金)までにご返信ください。",
                                        message_id=REQUEST_ID))
    env.clock.now = datetime(2026, 11, 19, 14, 0, tzinfo=JST)
    assert env.watcher.run_once() == 0
    assert len(room_posts(env)) == 1 and len(state_of(env)["open"]) == 1


def test_an_old_mail_filed_into_a_folder_later_is_left_alone(env):
    env.clock.now = datetime(2026, 12, 7, 10, 0, tzinfo=JST)   # 2週間以上あとに、古いメールをフォルダへ整理した
    env.server.put("森", "sorted", build(subject="古いメール", message_id="<filed@x>"), datetime(2026, 12, 7, 9, 55, tzinfo=JST))
    assert env.watcher.run_once() == 0 and env.model.calls == []
    assert state_of(env)["imap"]["森"]["folder:INBOX.JR"]["last_uid"] == env.server.next_uid  # 既読位置は進める


def test_a_reply_saved_in_the_other_sent_folder_is_found_too(env):
    incoming(env)
    env.watcher.run_once()
    env.model.checks["休業日のご確認"] = check(ANSWERED, "休業日を記入したリストを送付しました。")
    env.server.put("森", "sent2", build(sender="ymori@rakutenken.co.jp", to="yamaki@ad.takashimaya.co.jp",
                                       subject="Re: 休業日のご確認", body="山木様\n別添のとおりです。",
                                       message_id="<r2@rakutenken>", refs=REQUEST_ID),
                   datetime(2026, 11, 19, 11, 0, tzinfo=JST))
    env.clock.now = datetime(2026, 11, 19, 11, 5, tzinfo=JST)
    env.watcher.run_once()
    assert "への返信を確認しました（森さん・" in room_posts(env)[1] and state_of(env)["open"] == []


# --- 当社の返信 ---


def reply(env, box="藤田", subject="Re: 休業日のご確認", refs=REQUEST_ID, at=None,
          body="山木様\n休業日を記入して送付します。\n\n> 森様\n> 11月27日(金)までにご返信ください。"):
    env.model.checks["休業日のご確認"] = check(ANSWERED, "休業日を記入したリストを送付しました。")
    return env.server.put(box, "sent", build(sender="fujita@rakutenken.jp", to="yamaki@ad.takashimaya.co.jp",
                                             subject=subject, body=body, message_id=f"<r{env.server.next_uid}@rakutenken>",
                                             refs=refs, attach=["リスト.xlsx"]),
                          at or datetime(2026, 11, 19, 11, 0, tzinfo=JST))


def test_a_reply_in_the_sent_folder_closes_the_mail(env):
    incoming(env)
    env.watcher.run_once()
    reply(env)
    env.clock.now = datetime(2026, 11, 19, 11, 5, tzinfo=JST)
    env.watcher.run_once()
    body = room_posts(env)[1]
    assert "① 株式会社髙島屋 山木様 への返信を確認しました（藤田さん・11/19（木）11:00）。" in body
    assert "返信の内容: 休業日を記入したリストを送付しました。" in body
    assert "返信のメール" not in body and "[To:" not in body
    assert state_of(env)["open"] == []
    # 確かめるときに渡すのは当社が今回書いた部分。引用された先方の文は分けて渡す
    kind, text = env.model.calls[-1]
    assert kind == "check" and "本文:\n山木様\n休業日を記入して送付します。" in text
    assert "[引用されている過去のやり取り（転送された文面を含む）]\n> 森様" in text


def test_a_reply_under_another_subject_is_tied_by_the_ids_it_carries(env):
    incoming(env)
    env.watcher.run_once()
    reply(env, subject="休業日リスト送付の件", body="ご担当者様\n別添のとおりです。")
    env.watcher.run_once()
    assert state_of(env)["open"] == []


def test_a_mail_and_its_reply_in_the_same_round_are_read_in_order(env):
    # 送信済み（藤田）が受信箱（森）より先に読まれても、届いたメール → 返信 の順で突き合わせる
    reply(env, box="藤田")
    incoming(env, box="森", at=datetime(2026, 11, 19, 10, 30, tzinfo=JST))
    env.watcher.run_once()
    posts = room_posts(env)
    assert "への返信を確認しました" in posts[0] and "取引先からメールが届いています。" in posts[1]
    assert state_of(env)["open"] == []


def test_a_mail_sent_before_the_request_is_not_its_reply(env):
    reply(env, refs="", at=datetime(2026, 11, 19, 9, 0, tzinfo=JST))   # 先方のメールより前に送ったもの
    incoming(env, at=datetime(2026, 11, 19, 10, 30, tzinfo=JST))
    env.watcher.run_once()
    assert len(state_of(env)["open"]) == 1 and not any(kind == "check" for kind, _ in env.model.calls)


def test_a_reply_seen_on_the_server_and_in_the_room_is_checked_once(env):
    incoming(env)
    env.watcher.run_once()
    env.model.checks["休業日のご確認"] = check("一部のみ", remaining="最終受注日")
    env.server.put("藤田", "sent", build(sender="fujita@rakutenken.jp", to="yamaki@ad.takashimaya.co.jp",
                                        subject="Re: 休業日のご確認", body="山木様\n休業日は12/30〜1/3です。",
                                        message_id="<r@rakutenken>", refs=REQUEST_ID),
                   datetime(2026, 11, 19, 11, 0, tzinfo=JST))
    env.cw.mail("fujita@rakutenken.jp", "Re: 休業日のご確認", "山木様\n休業日は12/30〜1/3です。", received="2026-11-19 11:01")
    env.clock.now = datetime(2026, 11, 19, 11, 5, tzinfo=JST)
    env.watcher.run_once()
    assert sum("求めの一部への回答のように読めました" in body for body in room_posts(env)) == 1
    assert sum(kind == "check" for kind, _ in env.model.calls) == 1


# --- 読めなかったとき ---


def test_a_mail_the_model_could_not_read_stays_unread_until_it_can(env):
    uid = incoming(env)
    env.model.fail = 1
    assert env.watcher.run_once() == 0
    assert state_of(env)["imap"]["森"]["inbox"]["last_uid"] == uid - 1  # 既読位置を進めない
    assert env.watcher.run_once() == 1
    assert state_of(env)["imap"]["森"]["inbox"] == {"uidvalidity": 7, "last_uid": uid, "done": []}
    assert env.watcher.run_once() == 0 and len(room_posts(env)) == 1    # 二度は知らせない


def test_a_server_that_cannot_be_reached_is_reported_once_a_day_and_the_room_still_works(env):
    env.server.fail["森"] = True
    env.model.reads["確約書"] = read(staff=[], addressee="福本さま")
    env.cw.mail("y.nakatani@jp-ts.jp", "確約書の修正依頼", "福本さま\n修正をお願いします。")
    for _ in range(2):
        env.watcher.run_once()
    assert [room for room, _ in env.cw.sent] == [ROOM]      # 通知ルームの通知は、その間も知らせる
    env.watcher.run_once()
    room, text = env.cw.sent[-1]
    assert room == ADMIN_ROOM and text.startswith(f"[To:{ADACHI}]\n【営業メール】森さんのメールサーバーを読めていません")
    assert "通知ルームに流れる通知（本文の先頭のみ）で見張りを続けます" in text
    for _ in range(3):
        env.watcher.run_once()
    assert sum(room == ADMIN_ROOM for room, _ in env.cw.sent) == 1   # 同じ日に繰り返さない
    env.server.fail["森"] = False
    env.watcher.run_once()
    assert env.cw.sent[-1] == (ADMIN_ROOM, f"[To:{ADACHI}]\n【営業メール】森さんのメールサーバーを、また読めるようになりました。")


def test_mails_finished_out_of_order_are_not_read_twice(env):
    first = incoming(env, subject="先に届いたメール")
    env.server.put("森", "inbox", build(sender="support@superdelivery.com", subject="広告", message_id="<ad2@x>"))
    env.config.data["mail_watch"]["max_per_run"] = 1
    env.model.reads["後から届いたメール"] = read(staff=["森"])
    later = env.server.put("森", "inbox", build(subject="後から届いたメール", message_id="<later@x>"),
                           datetime(2026, 11, 19, 10, 1, tzinfo=JST))
    env.watcher.run_once()
    place = state_of(env)["imap"]["森"]["inbox"]
    assert place["last_uid"] == first and place["done"] == []
    env.watcher.run_once()
    assert state_of(env)["imap"]["森"]["inbox"]["last_uid"] == later
    assert sum("取引先からメールが届いています。" in body for body in room_posts(env)) == 2


def test_the_log_and_the_state_keep_no_body(env):
    incoming(env, body="森様\n秘密の商談メモ：仕入値は1個195円です。")
    env.watcher.run_once()
    logged = "".join(path.read_text(encoding="utf-8") for path in (env.tmp / "audit").glob("audit-*.jsonl"))
    assert "休業日のご確認" not in logged and "195円" not in logged
    assert "195円" not in (env.tmp / "state" / "mailwatch.json").read_text(encoding="utf-8")


def test_the_real_settings_name_only_variables_never_passwords():
    settings = Config.load().mail_watch
    assert [box["name"] for box in settings["mailboxes"]] == ["森", "藤田", "福本"]
    for box in settings["mailboxes"]:
        assert set(box) <= {"name", "address", "host", "port", "password_env", "user", "sent_folder", "tls"}
        assert box["password_env"].startswith("MAIL_PASSWORD_") and "password" not in box
        assert not (box.get("tls") or {}).get("pin_sha256")  # 証明書の検証を外す設定は、判断を得てから入れる
