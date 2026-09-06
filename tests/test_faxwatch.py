"""FAX受信の見張り。

実例（2026-09-04）: 「通知管理くん」が流すFAX受信通知は
「送信元番号なしのFAXです」となっていて、どこから届いたか分からない。
PDFを読んで差出人と内容（発注書なら発注内容）を篠田さん・足立さんへ知らせる。
知らせるのは月〜金の 8:30〜19:30 だけ。発注書は「対応完了」の返事をもらうまで
見ておき、翌営業日の朝と昼に一度ずつ聞く。
"""

import json
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from raizuinu.config import Config
from raizuinu.phrasing import BANKS
from raizuinu.faxwatch import (
    ASK_DONE,
    JST,
    FaxWatcher,
    PartnerDirectory,
    digits_only,
    in_notify_window,
    is_completion,
    next_business_day,
    parse_attachment,
    parse_notice,
)

FAX_ROOM = 446282163
NOTIFIER = 10294683
SHINODA, ADACHI = 9228914, 6945415
AGENT = 7777
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
CSV = "FAX番号,取引先名\n0745787390,とくとく香芝SA下り\n0761768551,光パックス石川\n"


def blank_pdf():
    import io
    import pypdf

    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=280)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


PDF = blank_pdf()


def page_rotation(pdf_bytes):
    import io
    import pypdf

    return pypdf.PdfReader(io.BytesIO(pdf_bytes)).pages[0].rotation


def at(*args):
    return datetime(*args, tzinfo=JST)


THU = at(2026, 9, 3, 10, 0)  # 木曜 10:00（時間帯の中）


def bot(message_id, body):
    return {"message_id": str(message_id), "body": body,
            "account": {"account_id": NOTIFIER, "name": "通知管理くん"}}


def human(message_id, body, account=SHINODA):
    return {"message_id": str(message_id), "body": body,
            "account": {"account_id": account, "name": "篠田"}}


def make_config(tmp_path):
    config = Config.load(tmp_path / "no-config.json")
    config.data["fax_watch"] = {
        "enabled": True, "room_id": FAX_ROOM, "notifier_account_id": NOTIFIER,
        "notify_account_ids": [SHINODA, ADACHI], "directory_csv_url": CSV_URL,
        "max_pdf_mb": 15, "max_per_day": 50,
        "mention_kinds": ["発注書", "注文書", "請求書", "見積書", "納品書"],
        "notify_window": {"start": "08:30", "end": "19:30"},
        "follow_up": {"enabled": True, "check_time": "09:00", "recheck_time": "12:00"},
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

    def get_me(self):
        return AGENT

    def get_file_info(self, room_id, file_id):
        return {"filename": f"{file_id}.pdf", "filesize": len(PDF),
                "download_url": f"https://files.example/{file_id}"}

    def send_message(self, room_id, body):
        self.sent.append((room_id, body))
        return str(9000 + len(self.sent) - 1)  # 自分の投稿は 9000, 9001, ...


def fake_http(csv_text=CSV):
    def get(url, timeout=60):
        if "export?format=csv" in url:
            return 200, url, csv_text.encode("utf-8")
        return 200, url, PDF
    return get


def fake_client(payload):
    """payload は1つの読み取り結果か、呼び出し順に返す結果のリスト（最後を繰り返す）。"""
    payloads = list(payload) if isinstance(payload, list) else [payload]
    client = SimpleNamespace(kwargs=None, calls=0, history=[])

    def create(**kwargs):
        client.kwargs = kwargs
        client.history.append(kwargs)
        found = {"rotation": 0, "readable": True, **payloads[min(client.calls, len(payloads) - 1)]}
        client.calls += 1
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=json.dumps(found, ensure_ascii=False))],
            usage=SimpleNamespace(input_tokens=2000, output_tokens=200,
                                  cache_creation_input_tokens=0, cache_read_input_tokens=0),
        )

    client.messages = SimpleNamespace(create=create)
    return client


def document_bytes(kwargs):
    import base64

    return base64.b64decode(kwargs["messages"][0]["content"][0]["source"]["data"])


UPSIDE_DOWN = {"sender": "", "kind": "その他", "is_order": False, "rotation": 180, "readable": False,
               "summary": "上下反転していて判読できない（判読しづらい）", "items": [], "due": "",
               "notes": "TEL/FAX 06-6644-0780 の記載あり"}
BLURRY = {"sender": "", "kind": "その他", "is_order": False, "rotation": 0, "readable": False,
          "summary": "不鮮明で判読できない", "items": [], "due": "", "notes": ""}


ORDER = {
    "sender": "光パックス石川", "kind": "発注書", "is_order": True,
    "summary": "ダンボール箱2種の発注です。",
    "items": [{"name": "A式ダンボール 60サイズ", "quantity": "200枚", "amount": "24,000円"},
              {"name": "A式ダンボール 80サイズ", "quantity": "100枚", "amount": ""}],
    "due": "9月12日", "notes": "担当: 山本様",
}
AD = {"sender": "", "kind": "広告", "is_order": False, "summary": "複合機リースの広告です。",
      "items": [], "due": "", "notes": ""}


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def watcher(tmp_path, messages, payload, csv_text=CSV, now=THU, http=None):
    clock = Clock(now)
    w = FaxWatcher(
        make_config(tmp_path), FakeChatwork(messages),
        client=fake_client(payload), http_get=http or fake_http(csv_text), now=clock,
    )
    w.clock = clock
    return w


def prime(w):
    """初回は既読にするだけ。以後の新着を処理させるために一度回す。"""
    w._chatwork.messages, saved = [], w._chatwork.messages
    w.run_once()
    w._chatwork.messages = saved


def bodies(w):
    return [body for _, body in w._chatwork.sent]


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


class TestCompletionWording:
    @pytest.mark.parametrize("body", [
        "対応完了", "確認完了です", f"[rp aid={AGENT} to={FAX_ROOM}-9000]対応済です",
        "発注入力しました", "終わりました", "確認しました。手配済みです",
    ])
    def test_done(self, body):
        assert is_completion(body)

    @pytest.mark.parametrize("body", [
        "未対応です", "まだ対応できていません", "対応中です", "これから確認します",
        "確認します", "これは誰の担当？", "完了していません",
    ])
    def test_not_done(self, body):
        assert not is_completion(body)


class TestClock:
    def test_weekdays_between_830_and_1930(self):
        window = {"start": "08:30", "end": "19:30"}
        assert in_notify_window(at(2026, 9, 3, 8, 30), window)
        assert in_notify_window(at(2026, 9, 3, 19, 29), window)
        assert not in_notify_window(at(2026, 9, 3, 8, 29), window)
        assert not in_notify_window(at(2026, 9, 3, 19, 30), window)
        assert not in_notify_window(at(2026, 9, 5, 10, 0), window)  # 土
        assert not in_notify_window(at(2026, 9, 6, 10, 0), window)  # 日
        assert in_notify_window(at(2026, 9, 23, 10, 0), window)  # 秋分の日（水）も平日扱い

    def test_next_business_day_skips_the_weekend_only(self):
        assert next_business_day(date(2026, 9, 3)) == date(2026, 9, 4)  # 木→金
        assert next_business_day(date(2026, 9, 4)) == date(2026, 9, 7)  # 金→月
        assert next_business_day(date(2026, 9, 22)) == date(2026, 9, 23)  # 祝日は飛ばさない


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
        assert body.endswith(ASK_DONE)  # 返し方を示しておく

    def test_the_directory_wins_over_the_document(self, tmp_path):
        # 番号が台帳にあれば、文書の社名よりそちらを正とする
        w = watcher(tmp_path, [bot(1, NOTICE_WITH_SENDER), bot(2, ATTACHMENT_2)],
                    {**ORDER, "sender": "文書上の別名"})
        prime(w)
        w.run_once()
        body = bodies(w)[0]
        assert "とくとく香芝SA下りから発注書が届きました" in body
        assert "FAX番号 0745787390 を台帳で照合" in body

    def test_a_non_order_gets_a_short_notice(self, tmp_path):
        w = watcher(tmp_path, [bot(1, NOTICE_NO_SENDER), bot(2, ATTACHMENT)], AD)
        prime(w)
        w.run_once()
        body = bodies(w)[0]
        assert "FAXが届きました（広告）" in body
        assert "特定できませんでした（送信元番号なし" in body
        assert "■発注内容" not in body
        assert ASK_DONE not in body  # 発注書でなければ返事は求めない
        assert "[To:" not in body  # 広告で呼び出さない（ルームに置くだけ）
        assert body.startswith(f"[rp aid={NOTIFIER} to={FAX_ROOM}-2]\nFAXが届きました")  # 空行を残さない

    @pytest.mark.parametrize("kind", ["請求書", "見積書", "納品書", "注文書"])
    def test_business_documents_still_call_people(self, tmp_path, kind):
        payload = {**AD, "kind": kind, "sender": "光パックス石川", "summary": f"{kind}です。"}
        w = watcher(tmp_path, [bot(1, NOTICE_NO_SENDER), bot(2, ATTACHMENT)], payload)
        prime(w)
        w.run_once()
        body = bodies(w)[0]
        assert f"[To:{SHINODA}]" in body and f"[To:{ADACHI}]" in body
        assert f"光パックス石川からFAXが届きました（{kind}）" in body

    @pytest.mark.parametrize("kind", ["案内", "その他"])
    def test_notices_and_the_rest_are_posted_quietly(self, tmp_path, kind):
        w = watcher(tmp_path, [bot(1, NOTICE_NO_SENDER), bot(2, ATTACHMENT)], {**AD, "kind": kind})
        prime(w)
        w.run_once()
        assert "[To:" not in bodies(w)[0]

    def test_the_same_fax_is_not_announced_twice(self, tmp_path):
        w = watcher(tmp_path, [bot(1, NOTICE_NO_SENDER), bot(2, ATTACHMENT)], ORDER)
        prime(w)
        w.run_once(); w.run_once()
        assert len(w._chatwork.sent) == 1
        assert w._client.calls == 1  # PDFも1回しか読まない

    def test_other_peoples_messages_are_ignored(self, tmp_path):
        w = watcher(tmp_path, [human(3, ATTACHMENT, ADACHI)], ORDER)
        prime(w)
        assert w.run_once() == 0

    def test_the_daily_cap_defers_the_rest_to_tomorrow(self, tmp_path):
        msgs = []
        for i in range(5):
            msgs += [bot(10 + 2 * i, NOTICE_NO_SENDER.replace("4950", f"49{i}0")),
                     bot(11 + 2 * i, ATTACHMENT.replace("4950", f"49{i}0"))]
        w = watcher(tmp_path, msgs, AD)
        w._config.data["fax_watch"]["max_per_day"] = 2
        prime(w)
        assert w.run_once() == 2
        w.clock.now = at(2026, 9, 4, 9, 0)
        assert w.run_once() == 2  # 捨てずに翌日へ持ち越す

    def test_an_unreadable_pdf_is_reported_not_swallowed(self, tmp_path):
        def bad_http(url, timeout=60):
            if "export?format=csv" in url:
                return 200, url, CSV.encode("utf-8")
            return 200, url, b"not a pdf"

        w = watcher(tmp_path, [bot(1, NOTICE_NO_SENDER), bot(2, ATTACHMENT)], ORDER, http=bad_http)
        prime(w)
        w.run_once()
        body = bodies(w)[0]
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


class TestUpsideDown:
    """実例（2026-09-05）: 珍味屋の天津栗発注書が逆さまに届き、「その他」で流れかけた。"""

    def test_an_upside_down_fax_is_turned_and_read_again(self, tmp_path):
        w = watcher(tmp_path, [bot(1, NOTICE_NO_SENDER), bot(2, ATTACHMENT)],
                    [UPSIDE_DOWN, {**ORDER, "sender": "有限会社珍味屋"}])
        prime(w)
        assert w.run_once() == 1
        assert w._client.calls == 2  # 回してもう一度だけ
        assert page_rotation(document_bytes(w._client.history[0])) == 0
        assert page_rotation(document_bytes(w._client.history[1])) == 180
        body = bodies(w)[0]
        assert "有限会社珍味屋から発注書が届きました" in body
        assert f"[To:{SHINODA}]" in body
        assert "判読できない" not in body
        assert len(w._load_state()["open"]) == 1  # 発注書として見届ける

    def test_a_readable_fax_is_read_only_once(self, tmp_path):
        w = watcher(tmp_path, [bot(1, NOTICE_NO_SENDER), bot(2, ATTACHMENT)], ORDER)
        prime(w)
        w.run_once()
        assert w._client.calls == 1

    def test_a_blurry_fax_is_tried_upside_down_then_handed_to_people(self, tmp_path):
        # 向きが分からなくても180度回して一度試す。それでも読めなければ To 付きで人に渡す
        w = watcher(tmp_path, [bot(1, NOTICE_NO_SENDER), bot(2, ATTACHMENT)], [BLURRY, BLURRY])
        prime(w)
        w.run_once()
        assert w._client.calls == 2
        body = bodies(w)[0]
        assert f"[To:{SHINODA}]" in body and f"[To:{ADACHI}]" in body  # 「その他」扱いで黙らない
        assert "内容を読み取れませんでした" in body
        assert "PDFを直接ご確認ください" in body
        assert "読み取れた範囲: 不鮮明で判読できない" in body
        assert "受信 2026/09/04 19:10:09" in body
        assert w._load_state()["open"] == []  # 発注書かどうか分からないので催促はしない

    def test_without_pypdf_the_first_reading_is_used(self, tmp_path, monkeypatch):
        import raizuinu.faxwatch as module

        monkeypatch.setattr(module, "rotate_pdf", lambda data, degrees: None)
        w = watcher(tmp_path, [bot(1, NOTICE_NO_SENDER), bot(2, ATTACHMENT)], [UPSIDE_DOWN, ORDER])
        prime(w)
        w.run_once()
        assert w._client.calls == 1
        assert "内容を読み取れませんでした" in bodies(w)[0]


class TestNotifyWindow:
    def test_a_saturday_fax_waits_until_monday_morning(self, tmp_path):
        w = watcher(tmp_path, [bot(1, NOTICE_NO_SENDER), bot(2, ATTACHMENT)], ORDER,
                    now=at(2026, 9, 5, 10, 0))  # 土
        prime(w)
        assert w.run_once() == 0
        w.clock.now = at(2026, 9, 6, 15, 0)  # 日
        assert w.run_once() == 0
        w.clock.now = at(2026, 9, 7, 8, 25)  # 月 8:25 はまだ
        assert w.run_once() == 0
        w.clock.now = at(2026, 9, 7, 8, 30)
        assert w.run_once() == 1
        assert w._client.calls == 1  # PDFを読むのも知らせるときだけ

    def test_a_late_fax_waits_for_the_next_morning(self, tmp_path):
        w = watcher(tmp_path, [bot(1, NOTICE_NO_SENDER), bot(2, ATTACHMENT)], ORDER,
                    now=at(2026, 9, 3, 20, 0))
        prime(w)
        assert w.run_once() == 0
        w.clock.now = at(2026, 9, 4, 8, 30)
        assert w.run_once() == 1

    def test_a_fax_scrolled_out_over_the_weekend_is_still_announced(self, tmp_path):
        # APIは直近100件しか返さない。土曜に見えた添付は控えておき、月曜に
        # 一覧から消えていても知らせる
        w = watcher(tmp_path, [bot(1, NOTICE_NO_SENDER), bot(2, ATTACHMENT)], ORDER,
                    now=at(2026, 9, 5, 10, 0))
        prime(w)
        w.run_once()
        w._chatwork.messages = []
        w.clock.now = at(2026, 9, 7, 9, 0)
        assert w.run_once() == 1
        assert "光パックス石川から発注書" in bodies(w)[0]

    def test_a_holiday_is_treated_as_a_working_day(self, tmp_path):
        w = watcher(tmp_path, [bot(1, NOTICE_NO_SENDER), bot(2, ATTACHMENT)], ORDER,
                    now=at(2026, 9, 23, 10, 0))  # 秋分の日（水）
        prime(w)
        assert w.run_once() == 1


class TestFollowUp:
    def order(self, tmp_path, now=THU):
        w = watcher(tmp_path, [bot(1, NOTICE_NO_SENDER), bot(2, ATTACHMENT)], ORDER, now=now)
        prime(w)
        assert w.run_once() == 1
        return w

    def test_the_next_morning_asks_whether_it_is_done(self, tmp_path):
        w = self.order(tmp_path)
        w.clock.now = at(2026, 9, 4, 8, 55)
        w.run_once()
        assert len(w._chatwork.sent) == 1  # 9時前はまだ
        w.clock.now = at(2026, 9, 4, 9, 0)
        w.run_once()
        assert len(w._chatwork.sent) == 2
        body = bodies(w)[1]
        assert body.startswith(f"[rp aid={NOTIFIER} to={FAX_ROOM}-2]")  # PDFの投稿につなげる
        assert f"[To:{SHINODA}]" in body and f"[To:{ADACHI}]" in body
        assert "おはようございます。" in body
        assert "9月4日に光パックス石川から届いた発注書（4950_001.pdf）ですが、確認と対応は完了していますでしょうか" in body
        assert "「対応完了」とお知らせください" in body
        w.run_once()
        assert len(w._chatwork.sent) == 2  # 同じ朝に二度は聞かない

    def test_noon_asks_once_more_and_then_stops(self, tmp_path):
        w = self.order(tmp_path)
        w.clock.now = at(2026, 9, 4, 9, 0)
        w.run_once()
        w.clock.now = at(2026, 9, 4, 11, 55)
        w.run_once()
        assert len(w._chatwork.sent) == 2
        w.clock.now = at(2026, 9, 4, 12, 0)
        w.run_once()
        assert len(w._chatwork.sent) == 3
        body = bodies(w)[2]
        assert "たびたび失礼します。" in body
        assert "確認漏れになっていないでしょうか" in body
        assert "9月4日に光パックス石川から届いた発注書（4950_001.pdf）" in body
        # 3度目は無い（以後は人に任せる）
        for later in (at(2026, 9, 4, 15, 0), at(2026, 9, 7, 9, 0), at(2026, 9, 8, 12, 0)):
            w.clock.now = later
            w.run_once()
        assert len(w._chatwork.sent) == 3
        assert w._load_state()["open"] == []

    def test_a_friday_order_is_checked_on_monday(self, tmp_path):
        w = self.order(tmp_path, now=at(2026, 9, 4, 15, 0))  # 金
        for weekend in (at(2026, 9, 5, 9, 0), at(2026, 9, 6, 9, 0)):
            w.clock.now = weekend
            w.run_once()
        assert len(w._chatwork.sent) == 1
        w.clock.now = at(2026, 9, 7, 9, 0)  # 月
        w.run_once()
        assert len(w._chatwork.sent) == 2

    def test_a_done_reply_to_the_notice_stops_the_follow_up(self, tmp_path):
        w = self.order(tmp_path)
        w._chatwork.messages.append(
            human(9500, f"[rp aid={AGENT} to={FAX_ROOM}-9000]対応完了です。")
        )
        w.clock.now = at(2026, 9, 4, 9, 0)
        w.run_once()
        assert len(w._chatwork.sent) == 2  # 通知と、報告へのお礼だけ（朝の確認は無い）
        assert w._load_state()["open"] == []
        thanks = bodies(w)[1]
        assert thanks.startswith(f"[rp aid={SHINODA} to={FAX_ROOM}-9500]\n")  # 相手の報告に返す
        assert thanks.split("\n", 1)[1] in BANKS["fax_done_thanks"]

    def test_a_plain_done_message_also_counts(self, tmp_path):
        # 返信タグ無しで「確認完了しました」と書かれても済んだと読む
        w = self.order(tmp_path)
        w._chatwork.messages.append(human(9500, "確認完了しました", ADACHI))
        w.clock.now = at(2026, 9, 4, 9, 0)
        w.run_once()
        assert len(w._chatwork.sent) == 2
        assert bodies(w)[1].startswith(f"[rp aid={ADACHI} to={FAX_ROOM}-9500]")

    def test_one_report_closing_two_orders_gets_one_thanks(self, tmp_path):
        msgs = [bot(1, NOTICE_NO_SENDER), bot(2, ATTACHMENT),
                bot(3, NOTICE_NO_SENDER.replace("4950", "4960")),
                bot(4, ATTACHMENT.replace("4950", "4960"))]
        w = watcher(tmp_path, msgs, ORDER)
        prime(w)
        assert w.run_once() == 2
        w._chatwork.messages.append(human(9500, "2件とも対応完了です"))
        w.run_once()
        assert len(w._chatwork.sent) == 3  # お礼は1回
        assert w._load_state()["open"] == []
        w.clock.now = at(2026, 9, 4, 9, 0)
        w.run_once()
        assert len(w._chatwork.sent) == 3

    def test_thanks_wording_never_reads_as_a_completion_report(self):
        # お礼の文が「完了」の報告と誤読されると、自分の投稿で発注書を閉じてしまう
        for phrase in BANKS["fax_done_thanks"]:
            assert not is_completion(phrase), phrase
            assert "ありがとう" in phrase

    def test_still_working_is_not_done(self, tmp_path):
        w = self.order(tmp_path)
        w._chatwork.messages.append(
            human(9500, f"[rp aid={AGENT} to={FAX_ROOM}-9000]対応中です。")
        )
        w.clock.now = at(2026, 9, 4, 9, 0)
        w.run_once()
        assert len(w._chatwork.sent) == 2

    def test_a_reply_to_another_fax_does_not_close_this_one(self, tmp_path):
        w = self.order(tmp_path)
        w._chatwork.messages.append(
            human(9500, f"[rp aid={AGENT} to={FAX_ROOM}-8888]対応完了です。")
        )
        w.clock.now = at(2026, 9, 4, 9, 0)
        w.run_once()
        assert len(w._chatwork.sent) == 2

    def test_a_done_reply_to_the_morning_check_stops_the_noon_one(self, tmp_path):
        w = self.order(tmp_path)
        w.clock.now = at(2026, 9, 4, 9, 0)
        w.run_once()
        w._chatwork.messages.append(
            human(9500, f"[rp aid={AGENT} to={FAX_ROOM}-9001]完了しました。")
        )
        w.clock.now = at(2026, 9, 4, 12, 0)
        w.run_once()
        assert len(w._chatwork.sent) == 3  # 通知・朝の確認・お礼。昼の再確認は無い
        assert "ありがとう" in bodies(w)[2]

    def test_the_bots_own_wording_is_not_mistaken_for_a_reply(self, tmp_path):
        # 自IDが取れないときも、自分の通知文（「対応完了」とお知らせください）で閉じない
        w = self.order(tmp_path)
        w._chatwork.get_me = lambda: (_ for _ in ()).throw(RuntimeError("down"))
        w._chatwork.messages.append({
            "message_id": "9000", "body": f"[rp aid={NOTIFIER} to={FAX_ROOM}-2]\n" + bodies(w)[0],
            "account": {"account_id": 0, "name": "経理財務アシスタント"},
        })
        w.clock.now = at(2026, 9, 4, 9, 0)
        w.run_once()
        assert len(w._chatwork.sent) == 2

    def test_non_orders_are_not_followed_up(self, tmp_path):
        w = watcher(tmp_path, [bot(1, NOTICE_NO_SENDER), bot(2, ATTACHMENT)], AD)
        prime(w)
        w.run_once()
        w.clock.now = at(2026, 9, 4, 9, 0)
        w.run_once()
        assert len(w._chatwork.sent) == 1

    def test_the_follow_up_survives_a_restart(self, tmp_path):
        w = self.order(tmp_path)
        again = FaxWatcher(
            make_config(tmp_path), w._chatwork, client=fake_client(ORDER),
            http_get=fake_http(), now=lambda: at(2026, 9, 4, 9, 0),
        )
        again.run_once()
        assert len(w._chatwork.sent) == 2

    def test_follow_up_can_be_switched_off(self, tmp_path):
        w = watcher(tmp_path, [bot(1, NOTICE_NO_SENDER), bot(2, ATTACHMENT)], ORDER)
        w._config.data["fax_watch"]["follow_up"]["enabled"] = False
        prime(w)
        w.run_once()
        assert ASK_DONE not in bodies(w)[0]
        w.clock.now = at(2026, 9, 4, 9, 0)
        w.run_once()
        assert len(w._chatwork.sent) == 1
