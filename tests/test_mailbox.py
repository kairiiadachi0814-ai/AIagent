"""メールサーバーの受信箱・送信済みを読む（IMAP・読み取り専用）。

指示（2026-11-24）: 営業メールは通知ルームの通知（先頭約400字）ではなく、メールサーバーを直接確認する。
読むだけで、既読にしない・印を付けない・移さない・消さない・送らない。
"""

import base64
import imaplib
import inspect
import re
import ssl
from datetime import datetime
from email.message import EmailMessage

import pytest

from raizuinu import mailbox
from raizuinu.mailbox import (
    AUTH,
    JST,
    TLS,
    MailboxError,
    MailboxReader,
    decode_folder,
    fingerprint_sha256,
    html_to_text,
    other_folders,
    parse_list_line,
    parse_message,
    pick_sent_folder,
    sent_folders,
    split_quoted,
    tls_context,
)

BOX = {"name": "森", "address": "ymori@rakutenken.co.jp", "host": "sv1151.wpx.ne.jp", "port": 993,
       "password_env": "MAIL_PASSWORD_MORI"}


def build(sender="山木 <yamaki@ad.takashimaya.co.jp>", to="ymori@rakutenken.co.jp", cc="", subject="休業日のご確認",
          body="森様\nお世話になっております。", charset="utf-8", message_id="<m1@takashimaya>", refs="",
          attach=None, html=None):
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = sender, to, subject
    if cc:
        message["Cc"] = cc
    message["Date"] = "Thu, 19 Nov 2026 15:15:00 +0900"
    message["Message-ID"] = message_id
    if refs:
        message["In-Reply-To"] = refs
        message["References"] = refs
    message.set_content(body, charset=charset)
    if html:
        message.add_alternative(html, subtype="html")
    for name in attach or []:
        message.add_attachment(b"data", maintype="application", subtype="octet-stream", filename=name)
    return message.as_bytes()


# --- メールの解釈 ---


def test_a_mail_is_read_with_its_addresses_subject_body_and_ids():
    mail = parse_message(build(cc="fujita@rakutenken.jp, Jimu <jimu@rakutenken.co.jp>", attach=["休業日確認リスト.xlsx"]))
    assert mail["sender"] == "yamaki@ad.takashimaya.co.jp" and mail["sender_name"] == "山木"
    assert mail["to"] == ["ymori@rakutenken.co.jp"]
    assert mail["cc"] == ["fujita@rakutenken.jp", "jimu@rakutenken.co.jp"]
    assert mail["subject"] == "休業日のご確認"
    assert mail["text"] == "森様\nお世話になっております。" and mail["quoted"] == ""
    assert mail["attachments"] == ["休業日確認リスト.xlsx"]
    assert mail["message_id"] == "<m1@takashimaya>" and mail["references"] == []
    assert mail["received"] == datetime(2026, 11, 19, 15, 15, tzinfo=JST)


@pytest.mark.parametrize("charset", ["iso-2022-jp", "shift_jis", "utf-8"])
def test_japanese_mails_in_the_usual_encodings_are_read(charset):
    mail = parse_message(build(subject="【要返信】年末年始のご案内", body="森様\n年末年始の休業日をご連絡ください。", charset=charset))
    assert mail["subject"] == "【要返信】年末年始のご案内"
    assert mail["text"] == "森様\n年末年始の休業日をご連絡ください。"


def test_the_time_the_server_received_it_is_used_when_given():
    received = datetime(2026, 11, 19, 15, 17, tzinfo=JST)
    assert parse_message(build(), received=received)["received"] == received


def test_a_reply_carries_the_ids_of_what_it_answers():
    mail = parse_message(build(sender="fujita@rakutenken.jp", to="yamaki@ad.takashimaya.co.jp",
                               subject="Re: 休業日のご確認", message_id="<r1@rakutenken>", refs="<m1@takashimaya>"))
    assert mail["references"] == ["<m1@takashimaya>", "<m1@takashimaya>"]


def test_what_was_written_now_is_separated_from_the_quoted_history():
    fresh, quoted = split_quoted("山木様\n記入して送ります。\n\n> 森様\n> 11/27までにご返信ください。")
    assert fresh == "山木様\n記入して送ります。" and quoted.startswith("> 森様")
    fresh, quoted = split_quoted("ご確認ください。\n\n-----Original Message-----\nFrom: a@b.co.jp\n本文")
    assert fresh == "ご確認ください。" and quoted.startswith("-----Original Message-----")
    fresh, quoted = split_quoted("転送します。\n---------- Forwarded message ---------\n差出人: x\n募集締切：11月20日")
    assert fresh == "転送します。" and "募集締切" in quoted
    fresh, quoted = split_quoted("了解しました。\n\n2026年11月19日(木) 15:03 山木 <yamaki@ad.takashimaya.co.jp>:\n元の文")
    assert fresh == "了解しました。" and quoted.endswith("元の文")
    fresh, quoted = split_quoted("了解です。\n\nFrom: 山木\nSent: Thursday, November 19, 2026\n元の文")
    assert fresh == "了解です。" and quoted.startswith("From: 山木")
    # 署名の区切り線は引用の始まりではない
    fresh, quoted = split_quoted("よろしくお願いします。\n----------------------------\n株式会社髙島屋 山木")
    assert quoted == "" and "山木" in fresh


def test_a_mail_with_only_html_is_read_as_text():
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = "a@b.co.jp", "ymori@rakutenken.co.jp", "件名"
    message.set_content("<html><style>p{}</style><body><p>森様</p><p>ご返信ください。<br>山木</p></body></html>", subtype="html")
    assert parse_message(message.as_bytes())["text"] == "森様\nご返信ください。\n山木"
    assert html_to_text("A&amp;B<script>x()</script>") == "A&B "


# --- フォルダ ---


def encode_folder(name):
    """読める名前 → サーバー上の名前（IMAPの修正UTF-7）。テスト用。"""
    out, run = "", ""

    def flush():
        nonlocal out, run
        if run:
            out += "&" + base64.b64encode(run.encode("utf-16-be")).decode().rstrip("=").replace("/", ",") + "-"
            run = ""

    for ch in name:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out += "&-" if ch == "&" else ch
        else:
            run += ch
    flush()
    return out


def test_folder_names_in_japanese_are_decoded():
    assert decode_folder("INBOX.&kAFP4W4IMH8-") == "INBOX.送信済み"
    assert decode_folder("INBOX.Sent") == "INBOX.Sent" and decode_folder("A&-B") == "A&B"


def test_the_sent_folder_is_found_by_flag_then_by_name():
    lines = [b'(\\HasNoChildren) "." "INBOX"', b'(\\HasNoChildren) "." "INBOX.Trash"',
             b'(\\HasNoChildren) "." "INBOX.&kAFP4W4IMH8-"', b'(\\HasNoChildren \\Sent) "." "INBOX.Sent"']
    folders = [parse_list_line(line) for line in lines]
    assert pick_sent_folder(folders)["raw"] == "INBOX.Sent"                      # サーバーの印
    assert pick_sent_folder(folders[:3])["name"] == "INBOX.送信済み"               # 名前
    assert pick_sent_folder(folders[:2]) is None
    assert pick_sent_folder(folders, configured="INBOX.送信済み")["raw"] == "INBOX.&kAFP4W4IMH8-"
    assert pick_sent_folder(folders, configured="無いフォルダ") is None


def test_every_sent_folder_is_read_not_just_the_first_one_found():
    # 実例（2026-11-24）: 森さんの箱には「INBOX.送信済み」（空）と「INBOX.Sent」（実際に使用）の両方があった
    lines = [b'(\\HasNoChildren) "." "INBOX.&kAFP4W4IMH8-"', b'(\\HasNoChildren) "." "INBOX.Sent"',
             b'(\\HasNoChildren) "." "INBOX"']
    folders = [parse_list_line(line) for line in lines]
    assert [f["name"] for f in sent_folders(folders)] == ["INBOX.送信済み", "INBOX.Sent"]
    assert [f["name"] for f in sent_folders(folders, configured="INBOX.Sent")] == ["INBOX.Sent"]


def test_folders_mail_gets_sorted_into_are_read_but_not_trash_or_drafts():
    names = ["INBOX", "INBOX.Sent", "INBOX.スーパーデリバリー", "INBOX.Trash", "INBOX.Junk", "INBOX.下書き",
             "INBOX.autoreply", "INBOX.mailfilter", "INBOX.高島屋.泉北店"]
    assert decode_folder(encode_folder("INBOX.高島屋.泉北店")) == "INBOX.高島屋.泉北店"
    folders = [parse_list_line(f'(\\HasNoChildren) "." "{encode_folder(name)}"'.encode()) for name in names]
    folders.append(parse_list_line(b'(\\Noselect) "." "INBOX.parent"'))
    kept = [f["name"] for f in other_folders(folders)]
    assert kept == ["INBOX.スーパーデリバリー", "INBOX.高島屋.泉北店"]
    assert [f["name"] for f in other_folders(folders, skip=["INBOX.高島屋.泉北店"])] == ["INBOX.スーパーデリバリー"]


# --- 接続（読み取り専用）---


class FakeConn:
    """imaplib の差し替え。呼ばれた命令を記録する。"""

    def __init__(self, password="ok", raws=None):
        self.calls, self.password = [], password
        self.raws = raws or {11: build(message_id="<m11@x>"), 12: build(message_id="<m12@x>", subject="二通目")}

    def login(self, user, password):
        self.calls.append(("LOGIN", user))
        if password != self.password:
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")
        return "OK", [b"logged in"]

    def logout(self):
        self.calls.append(("LOGOUT",))
        return "BYE", []

    def list(self):
        self.calls.append(("LIST",))
        return "OK", [b'(\\HasNoChildren) "." "INBOX"', b'(\\HasNoChildren \\Sent) "." "INBOX.Sent"']

    def select(self, mailbox='INBOX', readonly=False):
        self.calls.append(("EXAMINE" if readonly else "SELECT", mailbox))
        return "OK", [str(len(self.raws)).encode()]

    def response(self, code):
        return code, [{"UIDVALIDITY": b"77", "UIDNEXT": b"13"}[code]]

    def uid(self, command, *args):
        self.calls.append((f"UID {command}", *[a for a in args if a]))
        if command == "SEARCH":
            return "OK", [" ".join(str(u) for u in self.raws).encode()]
        uid, spec = int(args[0]), args[1]
        if "BODY.PEEK[]" in spec:
            return "OK", [(f"1 (UID {uid} BODY[] {{{len(self.raws[uid])}}}".encode(), self.raws[uid]), b")"]
        return "OK", [f'1 (UID {uid} RFC822.SIZE {len(self.raws[uid])} INTERNALDATE "19-Nov-2026 15:17:02 +0900")'.encode()]


def reader_with(conn, password="ok"):
    return MailboxReader(BOX, password=password, connect=lambda box: conn)


def test_new_mails_are_fetched_without_marking_them_read():
    conn = FakeConn()
    with reader_with(conn) as reader:
        assert [f["raw"] for f in reader.sent()] == ["INBOX.Sent"] and reader.others() == []
        info = reader.open(reader.inbox())
        assert info == {"messages": 2, "uidvalidity": 77, "uidnext": 13}
        assert reader.uids_after(11) == [12] and reader.uids_after(12) == [] and reader.last_uids(1) == [12]
        mail = reader.fetch(12)
    assert mail["uid"] == 12 and mail["subject"] == "二通目" and mail["message_id"] == "<m12@x>"
    assert mail["received"] == datetime(2026, 11, 19, 15, 17, 2, tzinfo=JST)  # サーバーに入った日時
    specs = [call[-1] for call in conn.calls if call[0] == "UID FETCH"]
    assert any("BODY.PEEK[]" in spec for spec in specs)
    assert not any(re.search(r"BODY\[|RFC822\b(?!\.SIZE)", spec) for spec in specs)  # 既読が付く取り方をしない
    assert conn.calls[-1] == ("LOGOUT",)


def test_only_reading_commands_are_ever_sent():
    conn = FakeConn()
    with reader_with(conn) as reader:
        reader.folders()
        reader.open(reader.inbox())
        reader.open(reader.sent()[0])
        reader.others()
        reader.uids_after(0)
        reader.last_uids(5)
        reader.fetch(11)
        reader.newest_date(12)
    assert {call[0] for call in conn.calls} == {"LOGIN", "LIST", "EXAMINE", "UID SEARCH", "UID FETCH", "LOGOUT"}
    assert ("EXAMINE", '"INBOX.Sent"') in conn.calls  # 送信済みも読み取り専用で開く


def test_the_code_has_no_way_to_change_or_send_mail():
    source = inspect.getsource(mailbox)
    # サーバーへ送る命令は、この3つの入口と、ログイン・ログアウトだけ
    assert set(re.findall(r'self\._call\("(\w+)"', source)) == {"list", "select", "uid"}
    assert set(re.findall(r"self\._conn\.(\w+)", source)) == {"login", "logout", "response"}
    assert set(re.findall(r'_call\("uid", "(\w+)"', source)) == {"SEARCH", "FETCH"}
    for forbidden in ("STORE", "EXPUNGE", "APPEND", "COPY", "MOVE", "DELETE", "smtplib", "readonly=False",
                      "BODY[", "RFC822)"):
        assert forbidden not in source, forbidden
    assert "readonly=True" in source and "BODY.PEEK" in source
    public = {name for name, _ in inspect.getmembers(MailboxReader, inspect.isfunction) if not name.startswith("_")}
    assert public == {"configured", "folders", "inbox", "sent", "others", "open", "uids_after", "last_uids", "fetch",
                      "newest_date"}


def test_a_wrong_password_is_reported_without_the_password():
    conn = FakeConn(password="right")
    with pytest.raises(MailboxError) as caught:
        with reader_with(conn, password="wrong-secret"):
            pass
    assert caught.value.kind == AUTH and "wrong-secret" not in str(caught.value)
    assert conn.calls[-1] == ("LOGOUT",)


def test_a_mailbox_without_a_password_is_not_connected(monkeypatch):
    monkeypatch.delenv("MAIL_PASSWORD_MORI", raising=False)
    reader = MailboxReader(BOX, connect=lambda box: pytest.fail("つないではいけない"))
    assert reader.configured() is False
    with pytest.raises(MailboxError):
        reader.__enter__()
    monkeypatch.setenv("MAIL_PASSWORD_MORI", "ok")
    assert MailboxReader(BOX, connect=lambda box: FakeConn()).configured() is True


def test_certificates_are_verified_unless_a_fingerprint_is_pinned():
    strict = tls_context(None)
    assert strict.verify_mode == ssl.CERT_REQUIRED and strict.check_hostname is True
    pinned = tls_context({"pin_sha256": "ff:b4"})
    assert pinned.verify_mode == ssl.CERT_NONE  # 検証の代わりに、指紋の一致を確かめる


def test_a_pinned_server_with_another_certificate_is_refused(monkeypatch):
    class Sock:
        def getpeercert(self, binary_form=False):
            return b"another certificate"

    class Conn:
        sock = Sock()
        closed = False

        def shutdown(self):
            Conn.closed = True

    monkeypatch.setattr(mailbox.imaplib, "IMAP4_SSL", lambda *a, **k: Conn())
    box = {**BOX, "tls": {"pin_sha256": fingerprint_sha256(b"the certificate we wrote down")}}
    with pytest.raises(MailboxError) as caught:
        mailbox._connect(box)
    assert caught.value.kind == TLS and Conn.closed is True
    box["tls"]["pin_sha256"] = ":".join(re.findall("..", fingerprint_sha256(b"another certificate"))).upper()
    assert isinstance(mailbox._connect(box), Conn)  # 書き方（区切り・大文字）が違っても同じ指紋なら通す
