"""FAX受信の見張り。

実例（2026-09-04）: 「通知管理くん」が流すFAX受信通知は
「送信元番号なしのFAXです」となっていて、どこから届いたか分からない。
PDFを読んで差出人と内容（発注書なら発注内容）を篠田さん・足立さんへ知らせる。
"""

import json
from types import SimpleNamespace

import pytest

from raizuinu.config import Config
from raizuinu.faxwatch import (
    FaxWatcher,
    PartnerDirectory,
    digits_only,
    parse_attachment,
    parse_notice,
)

FAX_ROOM = 446282163
NOTIFIER = 10294683
SHINODA, ADACHI = 9228914, 6945415
CSV_URL = "https://docs.google.com/spreadsheets/d/abc/export?format=csv"

NOTICE_NO_SENDER = (
    "[info][title]FAX受信通知[/title]受信日時：2026/09/04 19:10:09\n"
    "送信元番号なしのFAXです\nRJOBNUM：8497\nファイル名：4950_001.pdf\n"
    "PDFは添付ファイルをご確認ください[/info]"
)
NOTICE_WITH_SENDER = (
    "[info][title]FAX受信通知[/title]受信日時：2026/09/05 09:12:33\n"
    "送信元番号：0745-78-7390\nRJOBNUM：8498\nファイル名：4951_001.pdf\n"
    "PDFは添付ファイルをご確認ください[/info]"
)
ATTACHMENT = "[info][title][dtext:file_uploaded][/title][download:2153301583]4950_001.pdf (13.43 KB)[/download][/info]"
ATTACHMENT_2 = "[info][title][dtext:file_uploaded][/title][download:2153301999]4951_001.pdf (21.0 KB)[/download][/info]"
PDF = b"%PDF-1.4\n1 0 obj<</Type/Page>>endobj\n%%EOF"
CSV = "FAX番号,取引先名\n0745787390,とくとく香芝SA下り\n0761768551,光パックス石川\n"


def bot(message_id, body):
    return {"message_id": str(message_id), "body": body,
            "account": {"account_id": NOTIFIER, "name": "通知管理くん"}}


def make_config(tmp_path):
    config = Config.load(tmp_path / "no-config.json")
    config.data["fax_watch"] = {
        "enabled": True, "room_id": FAX_ROOM, "notifier_account_id": NOTIFIER,
        "notify_account_ids": [SHINODA, ADACHI], "directory_csv_url": CSV_URL,
        "max_pdf_mb": 15, "max_per_day": 50,
    }
    config.data["state_dir"] = str(tmp_path / "state")
    config.data["audit_log_dir"] = str(tmp_path / "logs")
    config.base_dir = tmp_path
    return config


class FakeChatwork:
    def __init__(self, messages):
        self.messages = messages
        self.sent = []

    def get_recent_messages(self, room_id, limit=20):
        return self.messages

    def get_file_info(self, room_id, file_id):
        return {"filename": f"{file_id}.pdf", "filesize": len(PDF),
                "download_url": f"https://files.example/{file_id}"}

    def send_message(self, room_id, body):
        self.sent.append((room_id, body))
        return "1"


def fake_http(csv_text=CSV):
    def get(url, timeout=60):
        if "export?format=csv" in url:
            return 200, url, csv_text.encode("utf-8")
        return 200, url, PDF
    return get


def fake_client(payload):
    response = SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=json.dumps(payload, ensure_ascii=False))],
        usage=SimpleNamespace(input_tokens=2000, output_tokens=200,
                              cache_creation_input_tokens=0, cache_read_input_tokens=0),
    )
    client = SimpleNamespace(kwargs=None, calls=0)

    def create(**kwargs):
        client.kwargs = kwargs
        client.calls += 1
        return response

    client.messages = SimpleNamespace(create=create)
    return client


ORDER = {
    "sender": "光パックス石川", "kind": "発注書", "is_order": True,
    "summary": "ダンボール箱2種の発注です。",
    "items": [{"name": "A式ダンボール 60サイズ", "quantity": "200枚", "amount": "24,000円"},
              {"name": "A式ダンボール 80サイズ", "quantity": "100枚", "amount": ""}],
    "due": "9月12日", "notes": "担当: 山本様",
}
AD = {"sender": "", "kind": "広告", "is_order": False, "summary": "複合機リースの広告です。",
      "items": [], "due": "", "notes": ""}


def watcher(tmp_path, messages, payload, csv_text=CSV):
    return FaxWatcher(
        make_config(tmp_path), FakeChatwork(messages),
        client=fake_client(payload), http_get=fake_http(csv_text),
    )


def prime(w):
    """初回は既読にするだけ。以後の新着を処理させるために一度回す。"""
    w._chatwork.messages, saved = [], w._chatwork.messages
    w.run_once()
    w._chatwork.messages = saved


class TestParsing:
    def test_a_notice_without_a_sender_number(self):
        parsed = parse_notice(NOTICE_NO_SENDER)
        assert parsed == {"sender_number": "", "filename": "4950_001.pdf",
                          "received_at": "2026/09/04 19:10:09"}

    def test_a_notice_with_a_hyphenated_number(self):
        parsed = parse_notice(NOTICE_WITH_SENDER)
        assert parsed["sender_number"] == "0745787390"  # 台帳と同じ数字だけの形に揃える
        assert parsed["filename"] == "4951_001.pdf"

    def test_the_attachment_carries_file_id_and_name(self):
        assert parse_attachment(ATTACHMENT) == {"file_id": 2153301583, "filename": "4950_001.pdf"}
        assert parse_attachment(NOTICE_NO_SENDER) is None

    def test_digits_only_handles_fullwidth(self):
        assert digits_only("０７４５－７８－７３９０") == "0745787390"


class TestDirectory:
    def test_numbers_are_matched_regardless_of_hyphens(self):
        d = PartnerDirectory(CSV_URL, fake_http(), now=lambda: 1000.0)
        assert d.lookup("0745-78-7390") == "とくとく香芝SA下り"
        assert d.lookup("0761768551") == "光パックス石川"
        assert d.lookup("0000000000") == ""

    def test_the_sheet_is_not_fetched_on_every_lookup(self):
        calls = []

        def http(url, timeout=60):
            calls.append(url)
            return 200, url, CSV.encode("utf-8")

        d = PartnerDirectory(CSV_URL, http, ttl_seconds=3600, now=lambda: 1000.0)
        d.lookup("0745787390"); d.lookup("0761768551")
        assert len(calls) == 1

    def test_a_login_redirect_keeps_the_old_table(self):
        # 共有設定が変わって読めなくなっても、前回の表で通知は続ける
        clock = {"t": 1000.0}
        state = {"ok": True}

        def http(url, timeout=60):
            if state["ok"]:
                return 200, url, CSV.encode("utf-8")
            return 200, "https://accounts.google.com/signin", b"<html>"

        d = PartnerDirectory(CSV_URL, http, ttl_seconds=10, now=lambda: clock["t"])
        assert d.lookup("0745787390") == "とくとく香芝SA下り"
        state["ok"] = False
        clock["t"] += 100
        assert d.lookup("0745787390") == "とくとく香芝SA下り"


class TestNotifying:
    def test_first_run_only_marks_as_read(self, tmp_path):
        # 過去に溜まっていたFAXをまとめて流さない
        w = watcher(tmp_path, [bot(1, NOTICE_NO_SENDER), bot(2, ATTACHMENT)], ORDER)
        assert w.run_once() == 0
        assert w._chatwork.sent == []

    def test_an_order_is_summarised_for_both_people(self, tmp_path):
        w = watcher(tmp_path, [bot(1, NOTICE_NO_SENDER), bot(2, ATTACHMENT)], ORDER)
        prime(w)
        assert w.run_once() == 1
        room, body = w._chatwork.sent[0]
        assert room == FAX_ROOM
        assert f"[To:{SHINODA}]" in body and f"[To:{ADACHI}]" in body
        assert "光パックス石川から発注書が届きました" in body
        assert "差出人の根拠: FAXの記載から" in body  # 番号が無いので文書から
        assert "■発注内容" in body
        assert "・A式ダンボール 60サイズ　200枚　24,000円" in body
        assert "・A式ダンボール 80サイズ　100枚　（読み取れず）" in body  # 空欄を作らない
        assert "納期: 9月12日" in body
        assert "備考: 担当: 山本様" in body
        assert "受信 2026/09/04 19:10:09" in body
        assert f"to={FAX_ROOM}-2]" in body  # PDFの投稿への返信として付ける

    def test_the_directory_wins_over_the_document(self, tmp_path):
        # 番号が台帳にあれば、文書の社名よりそちらを正とする
        w = watcher(tmp_path, [bot(1, NOTICE_WITH_SENDER), bot(2, ATTACHMENT_2)],
                    {**ORDER, "sender": "文書上の別名"})
        prime(w)
        w.run_once()
        body = w._chatwork.sent[0][1]
        assert "とくとく香芝SA下りから発注書が届きました" in body
        assert "FAX番号 0745787390 を台帳で照合" in body

    def test_a_non_order_gets_a_short_notice(self, tmp_path):
        w = watcher(tmp_path, [bot(1, NOTICE_NO_SENDER), bot(2, ATTACHMENT)], AD)
        prime(w)
        w.run_once()
        body = w._chatwork.sent[0][1]
        assert "FAXが届きました（広告）" in body
        assert "特定できませんでした（送信元番号なし" in body
        assert "■発注内容" not in body

    def test_the_same_fax_is_not_announced_twice(self, tmp_path):
        w = watcher(tmp_path, [bot(1, NOTICE_NO_SENDER), bot(2, ATTACHMENT)], ORDER)
        prime(w)
        w.run_once(); w.run_once()
        assert len(w._chatwork.sent) == 1
        assert w._client.calls == 1  # PDFも1回しか読まない

    def test_other_peoples_messages_are_ignored(self, tmp_path):
        human = {"message_id": "3", "body": ATTACHMENT,
                 "account": {"account_id": ADACHI, "name": "足立"}}
        w = watcher(tmp_path, [human], ORDER)
        prime(w)
        assert w.run_once() == 0

    def test_the_daily_cap_stops_runaway_cost(self, tmp_path):
        msgs = []
        for i in range(5):
            msgs += [bot(10 + 2 * i, NOTICE_NO_SENDER.replace("4950", f"49{i}0")),
                     bot(11 + 2 * i, ATTACHMENT.replace("4950", f"49{i}0"))]
        w = watcher(tmp_path, msgs, AD)
        w._config.data["fax_watch"]["max_per_day"] = 2
        prime(w)
        assert w.run_once() == 2

    def test_an_unreadable_pdf_is_reported_not_swallowed(self, tmp_path):
        def bad_http(url, timeout=60):
            if "export?format=csv" in url:
                return 200, url, CSV.encode("utf-8")
            return 200, url, b"not a pdf"

        w = FaxWatcher(make_config(tmp_path),
                       FakeChatwork([bot(1, NOTICE_NO_SENDER), bot(2, ATTACHMENT)]),
                       client=fake_client(ORDER), http_get=bad_http)
        prime(w)
        w.run_once()
        body = w._chatwork.sent[0][1]
        assert "読み取れませんでした" in body
        assert f"[To:{SHINODA}]" in body

    def test_the_pdf_is_sent_to_the_model_as_a_document(self, tmp_path):
        w = watcher(tmp_path, [bot(1, NOTICE_NO_SENDER), bot(2, ATTACHMENT)], ORDER)
        prime(w)
        w.run_once()
        content = w._client.kwargs["messages"][0]["content"]
        assert content[0]["type"] == "document"
        assert content[0]["source"]["media_type"] == "application/pdf"
        assert "推測で補わない" in w._client.kwargs["system"]

    def test_disabled_does_nothing(self, tmp_path):
        w = watcher(tmp_path, [bot(1, NOTICE_NO_SENDER), bot(2, ATTACHMENT)], ORDER)
        w._config.data["fax_watch"]["enabled"] = False
        assert w.run_once() == 0
