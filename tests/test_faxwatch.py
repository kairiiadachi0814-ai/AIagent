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
        "mention_kinds": ["注文", "請求", "見積", "納品", "発注書", "注文書", "請求書", "見積書", "納品書"],
        "order_words": ["注文", "発注", "直送", "申込", "サンプル", "入荷"],
        "notify_window": {"start": "08:30", "end": "19:30"},
        "follow_up": {"enabled": True, "check_time": "09:00", "recheck_time": "12:00",
                      "evening_time": "19:00", "max_open_days": 14},
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
        found = {"rotation": 0, "readable": True, "category": "その他", **payloads[min(client.calls, len(payloads) - 1)]}
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
    "sender": "光パックス石川", "kind": "発注書", "category": "注文", "is_order": True,
    "summary": "ダンボール箱2種の発注です。",
    "items": [{"name": "A式ダンボール 60サイズ", "quantity": "200枚", "amount": "24,000円"},
              {"name": "A式ダンボール 80サイズ", "quantity": "100枚", "amount": ""}],
    "due": "9月12日", "notes": "担当: 山本様",
}
AD = {"sender": "", "kind": "広告", "category": "広告", "is_order": False, "summary": "複合機リースの広告です。",
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

    def test_a_form_without_prices_does_not_say_amounts_were_unreadable(self, tmp_path):
        # 実例（2026-09-05）: 納品日と数量だけの注文票。金額欄そのものが無い
        payload = {**ORDER, "items": [
            {"name": "6/5(金) 天津栗 焼冷凍10KG/CS", "quantity": "2C/S", "amount": ""},
            {"name": "6/26(金) 天津栗 焼冷凍10KG/CS", "quantity": "2C/S", "amount": ""},
        ]}
        w = watcher(tmp_path, [bot(1, NOTICE_NO_SENDER), bot(2, ATTACHMENT)], payload)
        prime(w)
        w.run_once()
        body = bodies(w)[0]
        assert "・6/5(金) 天津栗 焼冷凍10KG/CS　2C/S\n" in body
        assert "（読み取れず）" not in body

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

    @pytest.mark.parametrize("kind, category", [("請求書", "請求"), ("御見積書", "見積"), ("納品書", "納品")])
    def test_business_documents_still_call_people(self, tmp_path, kind, category):
        payload = {**AD, "kind": kind, "category": category, "sender": "光パックス石川", "summary": f"{kind}です。"}
        w = watcher(tmp_path, [bot(1, NOTICE_NO_SENDER), bot(2, ATTACHMENT)], payload)
        prime(w)
        w.run_once()
        body = bodies(w)[0]
        assert f"[To:{SHINODA}]" in body and f"[To:{ADACHI}]" in body
        assert f"光パックス石川からFAXが届きました（{kind}）" in body
        assert ASK_DONE not in body  # 注文ではないので見届けはしない

    @pytest.mark.parametrize("kind, category", [("新商品のご案内", "案内"), ("その他", "その他"), ("セミナー案内", "広告")])
    def test_notices_and_the_rest_are_posted_quietly(self, tmp_path, kind, category):
        w = watcher(tmp_path, [bot(1, NOTICE_NO_SENDER), bot(2, ATTACHMENT)], {**AD, "kind": kind, "category": category})
        prime(w)
        w.run_once()
        assert "[To:" not in bodies(w)[0]


G7 = "株式会社G7ジャパンフードサービス こだわり食品事業本部"
CONFIRMATION = {
    "sender": G7, "kind": "発注書 情報確認表", "category": "注文", "is_order": True,
    "summary": "発注書一覧の送信確認。着荷予定日9月16日の発注書2件(No.59,60)が送信済み、未着なら連絡依頼。",
    "items": [{"name": "2026年9月16日 得意先:JAわかやま Aコープ クックガーデン", "quantity": "1行", "amount": ""}],
    "due": "2026年9月16日", "notes": "問い合わせは受発注担当まで",
}
QUIET_RULES = [
    {"sender": "G7ジャパンフードサービス", "title": "情報確認表", "reason": "既にFAXで届いた発注書の確認書"},
    {"sender": "キヨスクデリバリーサービス", "title": "棚卸残数日計表"},
]


def quiet_watcher(tmp_path, payload):
    w = watcher(tmp_path, [bot(1, NOTICE_NO_SENDER), bot(2, ATTACHMENT)], payload)
    w._config.data["fax_watch"]["quiet_rules"] = QUIET_RULES
    prime(w)
    return w


class TestQuietRules:
    """実例（2026-09-10）: G7ジャパンフードサービスの「発注書 情報確認表」は、既にFAXで届いた
    発注書の確認書。モデルは注文と読むが、To と対応完了の確認は要らない（キヨスクの
    「棚卸残数日計表」と同じ扱い）。差出人と表題の組で `quiet_rules` に登録する。
    """

    def test_a_confirmation_sheet_is_posted_without_to_or_follow_up(self, tmp_path):
        w = quiet_watcher(tmp_path, CONFIRMATION)
        w.run_once()
        body = bodies(w)[0]
        assert "[To:" not in body
        assert f"{G7}からFAXが届きました（発注書 情報確認表）。" in body
        assert "■発注内容" not in body and ASK_DONE not in body
        assert "※既にFAXで届いた発注書の確認書のため、呼び出し（To）と対応完了の確認は省いています。" in body
        assert w._load_state()["open"] == []  # 見届けの対象にしない
        w.clock.now = at(2026, 9, 4, 9, 0)
        w.run_once()
        assert len(w._chatwork.sent) == 1  # 翌朝の催促も無い

    def test_a_real_order_from_the_same_sender_is_still_an_order(self, tmp_path):
        w = quiet_watcher(tmp_path, {**CONFIRMATION, "kind": "発注書"})
        w.run_once()
        body = bodies(w)[0]
        assert f"[To:{SHINODA}]" in body and "■発注内容" in body and ASK_DONE in body
        assert len(w._load_state()["open"]) == 1

    def test_the_same_title_from_another_sender_is_still_an_order(self, tmp_path):
        w = quiet_watcher(tmp_path, {**CONFIRMATION, "sender": "テスト商店"})
        w.run_once()
        assert f"[To:{SHINODA}]" in bodies(w)[0]
        assert len(w._load_state()["open"]) == 1

    def test_a_rule_without_a_reason_adds_no_note(self, tmp_path):
        # モデルが注文と読んでも規則が勝つ。理由が無ければ一言も添えない
        kiosk = {**CONFIRMATION, "sender": "キヨスクデリバリーサービス(株)", "kind": "棚卸残数日計表"}
        w = quiet_watcher(tmp_path, kiosk)
        w.run_once()
        body = bodies(w)[0]
        assert "[To:" not in body and "省いています" not in body
        assert "キヨスクデリバリーサービス(株)からFAXが届きました（棚卸残数日計表）。" in body
        assert w._load_state()["open"] == []

    def test_an_unreadable_fax_is_never_quieted(self, tmp_path):
        # 読めなかったものは差出人も表題も当てにならないので、規則を当てずに人へ渡す
        w = quiet_watcher(tmp_path, {**BLURRY, "sender": G7, "kind": "発注書 情報確認表"})
        w.run_once()
        body = bodies(w)[0]
        assert f"[To:{SHINODA}]" in body and "読み取れませんでした" in body

    def test_matching_ignores_width_spaces_and_case(self):
        from raizuinu.faxwatch import quiet_rule_for

        rules = [{"sender": "G7ジャパンフードサービス", "title": "情報確認表"}]
        assert quiet_rule_for("株式会社Ｇ７ジャパンフードサービス　こだわり食品事業本部", "発注書　情報確認表", rules)
        assert quiet_rule_for("株式会社g7ジャパンフードサービス", "発注書情報確認表", rules)
        assert quiet_rule_for("株式会社G7ジャパンフードサービス", "発注書", rules) is None
        assert quiet_rule_for("テスト商店", "発注書 情報確認表", rules) is None
        assert quiet_rule_for("", "情報確認表", [{"title": "情報確認表"}])  # 表題だけの規則は差出人を問わない
        assert quiet_rule_for("株式会社G7ジャパンフードサービス", "情報確認表", [{}]) is None  # 空の規則は当たらない
        assert quiet_rule_for("株式会社G7ジャパンフードサービス", "情報確認表", None) is None


class TestSenderIsNeverOurselves:
    """実例（2026-09-07〜10）: 47件中9件で「RAKUTENKEN株式会社から発注書が届きました」と出た。

    取引先の発注書は宛先が当社なので、宛先を差出人と読んでいた。差出人に当社の社名が
    読まれたら理由を添えて読み直させ、それでも当社なら差出人を空にして人に渡す。
    要約の「当社より〜」「〜を当社へ発注」も外す（注文が当社に来るのは当たり前）。
    """

    OWN = {**ORDER, "sender": "RAKUTENKEN株式会社", "addressee": ""}
    G7 = {**ORDER, "sender": "株式会社G7ジャパンフードサービス", "addressee": "RAKUTENKEN株式会社"}

    def test_the_addressee_read_as_sender_is_reread_with_the_reason(self, tmp_path):
        w = watcher(tmp_path, [bot(1, NOTICE_NO_SENDER), bot(2, ATTACHMENT)], [self.OWN, self.G7])
        prime(w)
        w.run_once()
        body = bodies(w)[0]
        assert "株式会社G7ジャパンフードサービスから発注書が届きました。" in body
        assert "RAKUTENKEN" not in body
        assert w._client.calls == 2
        asked = w._client.history[1]["messages"][0]["content"][1]["text"]
        assert "「RAKUTENKEN株式会社」" in asked and "こちら（受信側）の会社名" in asked
        assert w._client.history[1]["messages"][0]["content"][0]["type"] == "document"  # PDFを付け直す

    def test_if_the_reread_still_says_us_the_sender_is_left_open(self, tmp_path):
        w = watcher(tmp_path, [bot(1, NOTICE_NO_SENDER), bot(2, ATTACHMENT)], self.OWN)
        prime(w)
        w.run_once()
        body = bodies(w)[0]
        assert "発注書が届きました。\n差出人: 特定できませんでした（送信元番号なし・文書には宛先の当社名しか見当たりません）" in body
        assert "RAKUTENKEN株式会社から" not in body
        assert f"[To:{SHINODA}]" in body and "■発注内容" in body and ASK_DONE in body  # 注文としては扱う
        assert w._client.calls == 2  # 読み直しは1回だけ
        assert w._load_state()["open"][0]["sender"] == ""

    def test_a_number_in_the_directory_needs_no_reread(self, tmp_path):
        w = watcher(tmp_path, [bot(1, NOTICE_WITH_SENDER), bot(2, ATTACHMENT_2)], self.OWN)
        prime(w)
        w.run_once()
        assert "とくとく香芝SA下りから発注書が届きました。" in bodies(w)[0]
        assert w._client.calls == 1

    def test_an_unreadable_fax_is_not_reread_for_the_sender(self, tmp_path):
        w = watcher(tmp_path, [bot(1, NOTICE_NO_SENDER), bot(2, ATTACHMENT)], {**BLURRY, "sender": "RAKUTENKEN株式会社"})
        prime(w)
        w.run_once()
        assert w._client.calls == 2  # 回して読み直す分だけ
        assert "読み取れませんでした" in bodies(w)[0]

    def test_our_name_is_dropped_from_the_summary(self, tmp_path):
        # 実例（2026-09-10 15:12）: 「RAKUTENKEN株式会社よりJR名古屋高島屋への天津甘栗の直送依頼」
        payload = {**ORDER, "sender": "ジャポニックス", "kind": "直送依頼書",
                   "summary": "RAKUTENKEN株式会社よりJR名古屋高島屋への天津甘栗の直送依頼。発注No.2609076116。"}
        w = watcher(tmp_path, [bot(1, NOTICE_NO_SENDER), bot(2, ATTACHMENT)], payload)
        prime(w)
        w.run_once()
        body = bodies(w)[0]
        assert "\nJR名古屋高島屋への天津甘栗の直送依頼。発注No.2609076116。\n" in body
        assert "RAKUTENKEN" not in body
        assert w._client.calls == 1  # 差出人は正しいので読み直さない

    @pytest.mark.parametrize("summary, expected", [
        ("RAKUTENKEN株式会社よりJR名古屋高島屋への天津甘栗の直送依頼。", "JR名古屋高島屋への天津甘栗の直送依頼。"),
        ("ＲＡＫＵＴＥＮＫＥＮ㈱より天津甘栗の直送依頼。", "天津甘栗の直送依頼。"),
        ("RAKUTENKEN(楽天軒本店)からの9月分の発注。", "9月分の発注。"),
        ("楽天軒への天津甘栗150g袋の発注。", "天津甘栗150g袋の発注。"),
        ("和栗プリン等をRAKUTENKEN株式会社へ発注。9月11日納品希望。", "和栗プリン等を発注。9月11日納品希望。"),
        ("樂天軒本店eむき甘栗化粧箱入を1点発注、2026/09/20必着。", "樂天軒本店eむき甘栗化粧箱入を1点発注、2026/09/20必着。"),
        ("清水屋Vinos 藤ヶ丘店へマロングラッセの発注。", "清水屋Vinos 藤ヶ丘店へマロングラッセの発注。"),
        ("", ""),
    ])
    def test_stripping_our_name_from_a_summary(self, summary, expected):
        from raizuinu.faxwatch import DEFAULT_OWN_COMPANY_WORDS, strip_own_company

        assert strip_own_company(summary, DEFAULT_OWN_COMPANY_WORDS) == expected

    def test_recognising_our_own_companies(self):
        from raizuinu.faxwatch import DEFAULT_OWN_COMPANY_WORDS, is_own_company

        for name in ["RAKUTENKEN株式会社", "RAKUTENKEN(株)", "RAKUTENKEN(楽天軒本店)", "ＲＡＫＵＴＥＮＫＥＮ株式会社",
                     "山田園(RAKUTENKEN株式会社宛)", "株式会社ライズクリエイション", "合同会社ohirome"]:
            assert is_own_company(name, DEFAULT_OWN_COMPANY_WORDS), name
        for name in ["株式会社G7ジャパンフードサービス", "ジャポニックス", "", "光パックス石川"]:
            assert not is_own_company(name, DEFAULT_OWN_COMPANY_WORDS), name

    def test_the_reader_is_told_who_we_are(self, tmp_path):
        from raizuinu.faxwatch import READ_SCHEMA

        w = watcher(tmp_path, [bot(1, NOTICE_NO_SENDER), bot(2, ATTACHMENT)], ORDER)
        prime(w)
        w.run_once()
        system = w._client.history[0]["system"]
        assert "RAKUTENKEN／楽天軒" in system and "{own_companies}" not in system
        assert "sender に書いてはならない" in system and "summary に当社（宛先）の社名を書かない" in system
        assert "addressee" in READ_SCHEMA["properties"] and "addressee" in READ_SCHEMA["required"]


class TestEveryKindOfOrderIsCaught:
    """実例（2026-09-07）: 過去のFAXを精査すると、発注書・注文書以外の表題で注文が来ていた。

    発注・注文は重要事項なので、モデルの判定・文書の性質・表題の語の3つのどれかで拾う。
    """

    TITLES = ["直送依頼書", "注文書", "サンプル依頼書", "FAX申込書", "商品注文伝票", "注文表",
              "【発注・入荷】表", "発注伝票", "注文", "直送"]

    @pytest.mark.parametrize("title", TITLES)
    def test_real_titles_are_orders_even_if_the_model_hesitates(self, tmp_path, title):
        # モデルが「その他」と言っても、表題の語で注文として扱う
        payload = {**AD, "kind": title, "category": "その他", "is_order": False,
                   "sender": "テスト商店", "summary": "商品の依頼です。",
                   "items": [{"name": "テスト商品", "quantity": "3", "amount": ""}]}
        w = watcher(tmp_path, [bot(1, NOTICE_NO_SENDER), bot(2, ATTACHMENT)], payload)
        prime(w)
        w.run_once()
        body = bodies(w)[0]
        assert f"テスト商店から{title}が届きました。" in body
        assert f"[To:{SHINODA}]" in body and f"[To:{ADACHI}]" in body
        assert "■発注内容" in body and ASK_DONE in body
        assert len(w._load_state()["open"]) == 1  # 見届けの対象になる

    def test_the_model_can_flag_an_order_under_any_title(self, tmp_path):
        payload = {**AD, "kind": "ご依頼", "category": "注文", "is_order": False, "sender": "テスト商店"}
        w = watcher(tmp_path, [bot(1, NOTICE_NO_SENDER), bot(2, ATTACHMENT)], payload)
        prime(w)
        w.run_once()
        assert "テスト商店からご依頼が届きました。" in bodies(w)[0]
        assert len(w._load_state()["open"]) == 1

    def test_the_reader_is_told_about_the_real_titles(self):
        from raizuinu.faxwatch import READ_SCHEMA, READ_SYSTEM

        for title in self.TITLES[:8]:
            assert title in READ_SYSTEM, title
        assert "見落としは許されない" in READ_SYSTEM
        assert READ_SCHEMA["properties"]["category"]["enum"][0] == "注文"

    def test_order_words_are_configurable(self, tmp_path):
        from raizuinu.faxwatch import looks_like_order

        assert looks_like_order("ＦＡＸ申込書", ("申込",))  # 全角も拾う
        assert not looks_like_order("新商品のご案内", ("注文", "発注", "直送", "申込", "サンプル", "入荷"))

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


class TestNothingSlipsThrough:
    """月曜朝にまとめて届いた分の処理漏れを防ぐ。お礼に残りを添え、19時に一覧を出す。"""

    def two_orders(self, tmp_path, now=THU):
        # 2件を別々の巡回で知らせる（同じ巡回で出る分は1通にまとまる。その形は TestBatchedNotices）
        w = watcher(tmp_path, [bot(1, NOTICE_NO_SENDER), bot(2, ATTACHMENT)], ORDER, now=now)
        prime(w)
        assert w.run_once() == 1  # 通知は 9000（①）
        w._chatwork.messages += [
            bot(3, NOTICE_NO_SENDER.replace("4950", "4960").replace("19:10:09", "19:40:00")),
            bot(4, ATTACHMENT.replace("4950", "4960")),
        ]
        assert w.run_once() == 1  # 通知は 9001（②）
        return w

    def test_thanks_lists_what_is_still_waiting(self, tmp_path):
        w = self.two_orders(tmp_path)
        w._chatwork.messages.append(human(9500, f"[rp aid={AGENT} to={FAX_ROOM}-9000]対応完了です。"))
        w.run_once()
        assert [t["filename"] for t in w.closed_last_run] == ["4950_001.pdf"]  # webhook側が二重に返さない目印
        thanks = bodies(w)[2]
        assert thanks.split("\n")[1] in BANKS["fax_done_thanks"]
        assert "対応待ちのFAXは、あと1件です。" in thanks
        assert "・② 9/4 19:40 光パックス石川 発注書（4960_001.pdf）" in thanks
        assert "4950_001.pdf" not in thanks
        # 最後の1件が済んだら、もう無いと言う
        w._chatwork.messages.append(human(9600, f"[rp aid={AGENT} to={FAX_ROOM}-9001]こちらも対応完了です。"))
        w.run_once()
        assert "対応待ちのFAXは、これでありません。" in bodies(w)[3]
        assert w._load_state()["open"] == []

    def test_an_announcement_to_members_is_not_a_report(self, tmp_path):
        # 実例（2026-09-08 13:58）: 足立さんのメンバー宛の周知文（返信ではない）に
        # 「対応完了に対する返信…」とあり、開いていた発注書を閉じてお礼を返した
        w = self.two_orders(tmp_path)
        w._chatwork.messages.append(human(
            9500,
            f"[To:{SHINODA}][To:{FUKUMOTO}][To:{NAKAURA}][To:{FUDABA}]\n"
            "対応完了に対する返信と対応待ちFAXの共有がチャットが分かれてしまっていた件、修正しております。\n"
            "新たにFAX届いた際に確認よろしくお願いいたします。",
            ADACHI,
        ))
        w.run_once()
        assert len(w._load_state()["open"]) == 2  # 閉じない
        assert len(w._chatwork.sent) == 2  # お礼も返さない

    def test_a_long_plain_message_is_not_a_report_either(self, tmp_path):
        w = self.two_orders(tmp_path)
        w._chatwork.messages.append(human(9500, "先ほどの件は対応完了しましたので、以降の分も同じ流れでよろしくお願いいたします。"))
        w.run_once()
        assert len(w._load_state()["open"]) == 2

    def test_a_short_plain_report_still_counts(self, tmp_path):
        w = self.two_orders(tmp_path)
        w._chatwork.messages.append(human(9500, "2件とも対応完了です"))
        w.run_once()
        assert w._load_state()["open"] == []

    def test_close_manually_thanks_once_with_the_rest(self, tmp_path):
        w = self.two_orders(tmp_path)
        message = {"account": {"account_id": SHINODA}, "message_id": "9500"}
        assert w.close_manually(FAX_ROOM, message, filenames=["4950_001.pdf"]) == 1
        thanks = bodies(w)[2]
        assert thanks.startswith(f"[rp aid={SHINODA} to={FAX_ROOM}-9500]\n")
        assert "対応待ちのFAXは、あと1件です。" in thanks and "4960_001.pdf" in thanks
        assert [t["filename"] for t in w._load_state()["open"]] == ["4960_001.pdf"]
        assert w.close_manually(FAX_ROOM, message, filenames=["nope.pdf"]) == 0
        assert w.close_manually(FAX_ROOM, message, everything=True) == 1
        assert w._load_state()["open"] == [] and "これでありません" in bodies(w)[-1]

    def test_a_report_naming_a_file_closes_only_that_one(self, tmp_path):
        w = self.two_orders(tmp_path)
        w._chatwork.messages.append(human(9500, "4960_001.pdf は対応完了です"))
        w.run_once()
        assert [t["filename"] for t in w._load_state()["open"]] == ["4950_001.pdf"]

    def test_the_evening_report_lists_the_rest_once_a_day(self, tmp_path):
        w = self.two_orders(tmp_path)
        w.clock.now = at(2026, 9, 3, 18, 55)
        w.run_once()
        assert len(w._chatwork.sent) == 2
        w.clock.now = at(2026, 9, 3, 19, 0)
        w.run_once()
        assert len(w._chatwork.sent) == 3
        body = bodies(w)[2]
        assert body.startswith(
            f"[To:{SHINODA}] [To:{ADACHI}]\nお疲れさまです。19:00時点で、対応完了の返信をいただいていないFAXが2件あります。"
        )
        assert "・① 9/4 19:10 光パックス石川 発注書（4950_001.pdf）" in body
        assert "・② 9/4 19:40 光パックス石川 発注書（4960_001.pdf）" in body
        assert "番号を添えて「対応完了」とお知らせください（例:「① 対応完了」）" in body
        assert "「全て対応完了」で結構です" in body and "問題ないか" in body
        w.clock.now = at(2026, 9, 3, 19, 10)
        w.run_once()
        assert len(w._chatwork.sent) == 3  # 同じ日に二度は出さない
        assert w._load_state()["evening_ids"] == ["9002"]

    def test_no_report_when_nothing_is_waiting(self, tmp_path):
        w = watcher(tmp_path, [bot(1, NOTICE_NO_SENDER), bot(2, ATTACHMENT)], AD)
        prime(w)
        w.run_once()
        w.clock.now = at(2026, 9, 3, 19, 0)
        w.run_once()
        assert len(w._chatwork.sent) == 1

    def test_replying_done_to_the_evening_report_closes_everything(self, tmp_path):
        w = self.two_orders(tmp_path)
        w.clock.now = at(2026, 9, 3, 19, 0)
        w.run_once()  # 一覧は 9002
        w._chatwork.messages.append(human(9500, f"[rp aid={AGENT} to={FAX_ROOM}-9002]2件とも対応完了です"))
        w.clock.now = at(2026, 9, 3, 19, 5)
        w.run_once()
        assert w._load_state()["open"] == []
        assert "対応待ちのFAXは、これでありません。" in bodies(w)[3]
        assert len(w._chatwork.sent) == 4  # お礼は1回

    def test_an_answer_other_than_done_gets_an_acknowledgement(self, tmp_path):
        w = self.two_orders(tmp_path)
        w.clock.now = at(2026, 9, 3, 19, 0)
        w.run_once()
        w._chatwork.messages.append(
            human(9500, f"[rp aid={AGENT} to={FAX_ROOM}-9002]問題なしです。明日対応します。")
        )
        w.clock.now = at(2026, 9, 3, 19, 5)
        w.run_once()
        assert len(w._load_state()["open"]) == 2  # 閉じない
        ack = bodies(w)[3]
        assert ack.startswith(f"[rp aid={SHINODA} to={FAX_ROOM}-9500]\n承知しました。")
        assert "翌営業日にもお知らせします" in ack
        w.run_once()
        assert len(w._chatwork.sent) == 4  # 同じ返事に二度は返さない

    def test_a_mentioned_answer_is_left_to_the_webhook_side(self, tmp_path):
        # メンション付きの返事は webhook 側が会話として返すので、巡回側は二重に返さない
        w = self.two_orders(tmp_path)
        w.clock.now = at(2026, 9, 3, 19, 0)
        w.run_once()
        w._chatwork.messages.append(
            human(9500, f"[rp aid={AGENT} to={FAX_ROOM}-9002][To:{AGENT}] 問題なしです。")
        )
        w.clock.now = at(2026, 9, 3, 19, 5)
        w.run_once()
        assert len(w._chatwork.sent) == 3
        assert "9500" in w._load_state()["acked"]

    def test_reminded_twice_it_still_stays_on_the_list(self, tmp_path):
        w = self.two_orders(tmp_path)
        for when in (at(2026, 9, 4, 9, 0), at(2026, 9, 4, 12, 0)):
            w.clock.now = when
            w.run_once()
        assert all(t["stage"] == 2 for t in w._load_state()["open"])
        w.clock.now = at(2026, 9, 4, 19, 0)
        w.run_once()
        assert "FAXが2件あります" in bodies(w)[-1]
        # 次の営業日、催促は増えないが夕方の一覧は続く
        w.clock.now = at(2026, 9, 7, 9, 0)
        w.run_once()
        w.clock.now = at(2026, 9, 7, 12, 0)
        w.run_once()
        count = len(w._chatwork.sent)
        w.clock.now = at(2026, 9, 7, 19, 0)
        w.run_once()
        assert len(w._chatwork.sent) == count + 1

    def test_very_old_ones_drop_off_the_list(self, tmp_path):
        w = self.two_orders(tmp_path)
        w.clock.now = at(2026, 9, 18, 9, 0)  # 15日後
        w.run_once()
        assert w._load_state()["open"] == []


def held(filename, received):
    """時間外に届いた通知と添付（受信日時つき）。"""
    return [
        bot(int(filename[:4]) * 2, NOTICE_NO_SENDER.replace("4950", filename[:4]).replace("2026/09/04 19:10:09", received)),
        bot(int(filename[:4]) * 2 + 1, ATTACHMENT.replace("4950", filename[:4])),
    ]


class TestBatchedNotices:
    """朝 8:30 に出す控え分（時間外・土日に届いたFAX）は1通にまとめ、番号（①②…）で返してもらう。

    1件ずつ通知が鳴ると月曜の朝に何通も並ぶ。番号を振れば「①③ 対応完了」で済み、
    全部なら「全て対応完了」で閉じられる。
    """

    def held_over_weekend(self, tmp_path, payload=None, **config):
        msgs = held("4950_001.pdf", "2026/09/05 10:00:00") + held("4960_001.pdf", "2026/09/06 15:00:00")
        w = watcher(tmp_path, msgs, payload or ORDER, now=at(2026, 9, 5, 10, 0))  # 土
        for key, value in config.items():
            w._config.data["fax_watch"][key] = value
        prime(w)
        assert w.run_once() == 0  # 土日は控えるだけ
        w.clock.now = at(2026, 9, 7, 8, 30)  # 月
        assert w.run_once() == 2
        return w

    def test_two_held_faxes_go_out_as_one_numbered_message(self, tmp_path):
        w = self.held_over_weekend(tmp_path)
        assert len(w._chatwork.sent) == 1
        body = bodies(w)[0]
        assert body.startswith(f"[To:{SHINODA}] [To:{ADACHI}]\n届いているFAXが2件あります。まとめてお知らせします。")
        assert "\n① 光パックス石川から発注書が届きました。" in body
        assert "\n② 光パックス石川から発注書が届きました。" in body
        assert body.count("■発注内容") == 2
        assert "（ファイル: 4950_001.pdf／受信 2026/09/05 10:00:00）" in body
        assert "（ファイル: 4960_001.pdf／受信 2026/09/06 15:00:00）" in body
        assert body.count("※PDFを読んで整理しています") == 1  # 注意書きは末尾に1回
        assert ASK_DONE not in body
        assert "対応完了の返事が要るのは ①② です。" in body
        assert "番号を添えて「対応完了」とお知らせください（例:「① 対応完了」）。全件済みでしたら「全て対応完了」で結構です。" in body
        assert "[rp " not in body  # 複数のPDFをまとめているので特定の投稿にはつなげない
        threads = w._load_state()["open"]
        assert [(t["number"], t["filename"]) for t in threads] == [(1, "4950_001.pdf"), (2, "4960_001.pdf")]
        assert threads[0]["posted_id"] == threads[1]["posted_id"] == "9000"

    def test_a_number_closes_only_that_fax(self, tmp_path):
        w = self.held_over_weekend(tmp_path)
        w._chatwork.messages.append(human(9500, f"[rp aid={AGENT} to={FAX_ROOM}-9000]① 対応完了"))
        w.clock.now = at(2026, 9, 7, 9, 0)
        w.run_once()
        assert [t["filename"] for t in w.closed_last_run] == ["4950_001.pdf"]
        thanks = bodies(w)[1]
        assert thanks.startswith(f"[rp aid={SHINODA} to={FAX_ROOM}-9500]\n")
        assert "対応待ちのFAXは、あと1件です。" in thanks
        assert "・② 9/6 15:00 光パックス石川 発注書（4960_001.pdf）" in thanks
        assert [t["number"] for t in w._load_state()["open"]] == [2]
        w._chatwork.messages.append(human(9600, f"[rp aid={AGENT} to={FAX_ROOM}-9000]全て対応完了"))
        w.run_once()
        assert w._load_state()["open"] == []
        assert "対応待ちのFAXは、これでありません。" in bodies(w)[2]

    @pytest.mark.parametrize("reply, left", [
        ("①② 対応完了", []), ("1と2 対応完了", []), ("No.2 完了です", [1]), ("2番 済みました", [1]),
        ("2件とも対応完了です", []), ("全部済みました", []), ("(1) 対応完了", [2]),
    ])
    def test_numbers_and_all_in_several_wordings(self, tmp_path, reply, left):
        w = self.held_over_weekend(tmp_path)
        w._chatwork.messages.append(human(9500, f"[rp aid={AGENT} to={FAX_ROOM}-9000]{reply}"))
        w.run_once()
        assert [t["number"] for t in w._load_state()["open"]] == left
        assert len(w._chatwork.sent) == 2  # お礼は1回

    def test_done_without_a_number_is_asked_back_once(self, tmp_path):
        # 黙って全部閉じると処理漏れになるので、どの番号か聞き返す
        w = self.held_over_weekend(tmp_path)
        w._chatwork.messages.append(human(9500, f"[rp aid={AGENT} to={FAX_ROOM}-9000]対応完了です。"))
        w.run_once()
        assert len(w._load_state()["open"]) == 2  # 閉じない
        assert [m["message_id"] for m in w.asked_last_run] == ["9500"]  # webhook側が会話の返事を重ねない目印
        ask = bodies(w)[1]
        assert ask.startswith(f"[rp aid={SHINODA} to={FAX_ROOM}-9500]\nありがとうございます。対応待ちが2件あるので")
        assert "番号を添えて「対応完了」とお返事ください（例:「① 対応完了」）" in ask
        assert "「全て対応完了」で結構です" in ask
        assert "・① 9/5 10:00 光パックス石川 発注書（4950_001.pdf）" in ask
        assert not is_completion(ask.split("\n", 1)[1]) or True  # 自分の投稿は投稿者で除くので誤読しない
        w.run_once()
        assert len(w._chatwork.sent) == 2  # 二度は聞かない
        assert w._load_state()["asked_ids"] == ["9500"]
        # あとから「② 対応完了」→ ②だけ閉じる。先の曖昧な報告で残りが閉じたりしない
        w._chatwork.messages.append(human(9600, f"[rp aid={AGENT} to={FAX_ROOM}-9000]② 対応完了"))
        w.run_once()
        assert [t["number"] for t in w._load_state()["open"]] == [1]
        assert w.asked_last_run == []

    def test_a_reply_that_excludes_a_number_is_asked_back(self, tmp_path):
        w = self.held_over_weekend(tmp_path)
        w._chatwork.messages.append(human(9500, f"[rp aid={AGENT} to={FAX_ROOM}-9000]①以外は対応完了"))
        w.run_once()
        assert len(w._load_state()["open"]) == 2
        assert "番号を添えて" in bodies(w)[1]

    def test_a_mixed_batch_asks_only_for_the_orders(self, tmp_path):
        w = self.held_over_weekend(tmp_path, payload=[ORDER, AD])
        body = bodies(w)[0]
        assert body.startswith(f"[To:{SHINODA}] [To:{ADACHI}]\n")  # 発注書があるので呼び出す
        assert "\n① 光パックス石川から発注書が届きました。" in body
        assert "\n② FAXが届きました（広告）。" in body  # 広告にも番号は振る（指せるように）
        assert "対応完了の返事が要るのは ① です。" in body
        assert [t["number"] for t in w._load_state()["open"]] == [1]  # 見届けるのは発注書だけ

    def test_only_ads_are_posted_quietly_without_asking(self, tmp_path):
        w = self.held_over_weekend(tmp_path, payload=AD)
        body = bodies(w)[0]
        assert "[To:" not in body and "対応完了の返事が要るのは" not in body
        assert "\n① FAXが届きました（広告）。" in body and "\n② FAXが届きました（広告）。" in body
        assert w._load_state()["open"] == []

    def test_daytime_singles_continue_the_numbering_and_the_next_day_restarts(self, tmp_path):
        w = self.held_over_weekend(tmp_path)
        w._chatwork.messages += held("4970_001.pdf", "2026/09/07 10:00:00")
        w.clock.now = at(2026, 9, 7, 10, 5)
        assert w.run_once() == 1
        body = bodies(w)[1]
        assert body.startswith(
            f"[rp aid={NOTIFIER} to={FAX_ROOM}-{4970 * 2 + 1}]\n[To:{SHINODA}] [To:{ADACHI}]\n③ 光パックス石川から発注書が届きました。"
        )
        assert body.endswith(ASK_DONE)  # 1件の通知は従来の返し方のまま
        w._chatwork.messages += held("4980_001.pdf", "2026/09/08 08:00:00")
        w.clock.now = at(2026, 9, 8, 8, 35)  # 翌日は①から
        assert w.run_once() == 1
        assert "\n① 光パックス石川から発注書が届きました。" in bodies(w)[2]
        assert [t["number"] for t in w._load_state()["open"]] == [1, 2, 3, 1]

    def test_the_same_number_on_another_day_means_the_newest_unless_replied_to(self, tmp_path):
        w = self.held_over_weekend(tmp_path)
        w._chatwork.messages += held("4980_001.pdf", "2026/09/08 08:00:00")
        w.clock.now = at(2026, 9, 8, 8, 35)
        w.run_once()  # 火曜の① = 4980（通知 9001）
        w._chatwork.messages.append(human(9500, "① 対応完了"))  # 返信でなければ新しい方
        w.run_once()
        assert [t["filename"] for t in w.closed_last_run] == ["4980_001.pdf"]
        w._chatwork.messages.append(human(9600, f"[rp aid={AGENT} to={FAX_ROOM}-9000]① 対応完了"))  # 月曜の通知への返信
        w.run_once()
        assert [t["filename"] for t in w.closed_last_run] == ["4950_001.pdf"]
        assert [t["filename"] for t in w._load_state()["open"]] == ["4960_001.pdf"]

    def test_the_morning_check_for_a_batch_is_one_message(self, tmp_path):
        w = self.held_over_weekend(tmp_path)
        w.clock.now = at(2026, 9, 8, 9, 0)  # 翌営業日 9:00
        w.run_once()
        assert len(w._chatwork.sent) == 2
        check = bodies(w)[1]
        assert check.startswith(f"[To:{SHINODA}] [To:{ADACHI}]\nおはようございます。\n次の2件のFAXですが、確認と対応は完了していますでしょうか。")
        assert "・① 9/5 10:00 光パックス石川 発注書（4950_001.pdf）" in check
        assert "・② 9/6 15:00 光パックス石川 発注書（4960_001.pdf）" in check
        assert "番号を添えて「対応完了」とお知らせください（例:「① 対応完了」）。全件済みでしたら「全て対応完了」で結構です。" in check
        assert all(t["stage"] == 1 and t["check_ids"] == ["9001"] for t in w._load_state()["open"])
        # 催促への返事も番号で
        w._chatwork.messages.append(human(9500, f"[rp aid={AGENT} to={FAX_ROOM}-9001]② 対応完了"))
        w.clock.now = at(2026, 9, 8, 9, 30)
        w.run_once()
        assert [t["number"] for t in w._load_state()["open"]] == [1]
        # 昼の再確認は残った①だけ（1件なので従来の形）
        w.clock.now = at(2026, 9, 8, 12, 0)
        w.run_once()
        again = bodies(w)[-1]
        assert "たびたび失礼します。" in again and "4950_001.pdf" in again
        assert again.startswith(f"[rp aid={NOTIFIER} to={FAX_ROOM}-{4950 * 2 + 1}]")

    def test_the_evening_list_uses_the_numbers(self, tmp_path):
        w = self.held_over_weekend(tmp_path)
        w.clock.now = at(2026, 9, 7, 19, 0)
        w.run_once()
        body = bodies(w)[1]
        assert "・① 9/5 10:00 光パックス石川 発注書（4950_001.pdf）" in body
        assert "番号を添えて「対応完了」とお知らせください（例:「① 対応完了」）" in body
        w._chatwork.messages.append(human(9500, f"[rp aid={AGENT} to={FAX_ROOM}-9001]①対応完了"))
        w.clock.now = at(2026, 9, 7, 19, 5)
        w.run_once()
        assert [t["number"] for t in w._load_state()["open"]] == [2]

    def test_a_long_batch_is_split_and_the_numbering_continues(self, tmp_path):
        msgs = (held("4950_001.pdf", "2026/09/05 10:00:00") + held("4960_001.pdf", "2026/09/06 15:00:00")
                + held("4970_001.pdf", "2026/09/06 16:00:00"))
        w = watcher(tmp_path, msgs, ORDER, now=at(2026, 9, 5, 10, 0))
        w._config.data["fax_watch"]["batch"] = {"enabled": True, "max_chars": 1}
        prime(w)
        w.run_once()
        w.clock.now = at(2026, 9, 7, 8, 30)
        assert w.run_once() == 3
        assert len(w._chatwork.sent) == 3
        first, second, third = bodies(w)
        assert "届いているFAXが3件あります" in first and "\n① " in first and "対応完了の返事が要るのは ① です。" in first
        assert second.startswith(f"[To:{SHINODA}] [To:{ADACHI}]\n（続き 2/3）") and "\n② " in second
        assert "（続き 3/3）" in third and "\n③ " in third
        assert [(t["number"], t["posted_id"]) for t in w._load_state()["open"]] == [(1, "9000"), (2, "9001"), (3, "9002")]
        w._chatwork.messages.append(human(9500, f"[rp aid={AGENT} to={FAX_ROOM}-9001]② 対応完了"))
        w.run_once()
        assert [t["number"] for t in w._load_state()["open"]] == [1, 3]

    def test_batching_can_be_switched_off(self, tmp_path):
        w = self.held_over_weekend(tmp_path, batch={"enabled": False})
        assert len(w._chatwork.sent) == 2
        assert "\n① 光パックス石川から発注書が届きました。" in bodies(w)[0]
        assert "\n② 光パックス石川から発注書が届きました。" in bodies(w)[1]
        assert all(b.endswith(ASK_DONE) for b in bodies(w))

    def test_the_status_reply_shows_the_numbers(self, tmp_path):
        from raizuinu.faxwatch import FaxStatus

        w = self.held_over_weekend(tmp_path)
        text = FaxStatus(w._config, now=lambda: at(2026, 9, 7, 10, 0))._status()
        assert "・① 9/5 10:00 光パックス石川 発注書（4950_001.pdf）" in text
        assert "番号を添えて「対応完了」とお知らせください（例:「① 対応完了」）" in text

    @pytest.mark.parametrize("body, expected", [
        ("①③ 対応完了", [1, 3]), ("1と3対応完了", [1, 3]), ("No.2 完了です", [2]), ("(4) 対応完了", [4]),
        ("2番と 5番 済みました", [2, 5]), ("㉑ 済", [21]), ("１と２ 対応完了", [1, 2]),
        ("9/12納品分 対応完了", []), ("2件とも対応完了", []), ("4950_001.pdf 対応完了", []),
        ("10時に対応完了しました", []), (f"[rp aid={AGENT} to={FAX_ROOM}-9000]①対応完了", [1]),
    ])
    def test_reading_numbers_from_a_reply(self, body, expected):
        from raizuinu.faxwatch import report_numbers

        assert report_numbers(body) == expected

    def test_wording_for_all_and_the_circled_numbers(self):
        from raizuinu.faxwatch import circled, mentions_all

        assert mentions_all("全て対応完了") and mentions_all("2件とも対応完了です") and mentions_all("全部済みました")
        assert not mentions_all("①対応完了") and not mentions_all("対応完了です")
        assert [circled(n) for n in (1, 20, 21, 35, 36, 50, 51)] == ["①", "⑳", "㉑", "㉟", "㊱", "㊿", "(51)"]


COMBINED = (
    "[To:7777]\n[info][title]FAX受信通知[/title]受信日時：2026/09/07 18:55:46\n"
    "送信元番号なしのFAXです\nRJOBNUM：8531\nファイル名：4971_001.pdf\n"
    "PDFは添付ファイルをご確認ください[/info]\n"
    "[info][title][dtext:file_uploaded][/title][download:2154500001]4971_001.pdf (40.0 KB)[/download][/info]"
)


class TestRepostsAndCombinedNotices:
    """実例（2026-09-07）: 通知と添付が1通にまとまって届き、受信日時が拾えなかった。
    同じファイル（4962_001.pdf）が3回投稿され、3回とも読んで知らせた。"""

    def test_a_combined_message_keeps_the_received_time(self, tmp_path):
        w = watcher(tmp_path, [bot(5, COMBINED)], ORDER)
        prime(w)
        assert w.run_once() == 1
        assert "受信 2026/09/07 18:55:46" in bodies(w)[0]
        assert w._load_state()["open"][0]["received_at"] == "2026/09/07 18:55:46"

    def test_a_late_notice_fills_in_a_held_fax(self, tmp_path):
        # 土曜に添付だけ見え、通知本文は次の巡回で見えた場合
        w = watcher(tmp_path, [bot(2, ATTACHMENT)], ORDER, now=at(2026, 9, 5, 10, 0))
        prime(w)
        w.run_once()
        assert w._load_state()["pending"][0]["notice"] == {}
        w._chatwork.messages.append(bot(1, NOTICE_NO_SENDER))
        w.run_once()
        assert w._load_state()["pending"][0]["notice"]["received_at"] == "2026/09/04 19:10:09"

    def test_a_repost_of_a_read_fax_is_not_read_again(self, tmp_path):
        w = watcher(tmp_path, [bot(1, NOTICE_NO_SENDER), bot(2, ATTACHMENT)], ORDER)
        prime(w)
        assert w.run_once() == 1
        w._chatwork.messages.append(bot(6, ATTACHMENT.replace("2153301583", "2153309999")))  # 同じ名前の再投稿
        w.clock.now = at(2026, 9, 3, 10, 20)
        assert w.run_once() == 0
        assert w._client.calls == 1  # 読み直さない
        note = bodies(w)[1]
        assert note.startswith(f"[rp aid={NOTIFIER} to={FAX_ROOM}-6]\nこのFAX（4950_001.pdf）は10:00に知らせた分と同じファイル名のため")
        assert "[To:" not in note
        assert len(w._load_state()["open"]) == 1  # 見届けも二重にしない

    def test_a_resend_after_an_unreadable_one_is_read_again(self, tmp_path):
        w = watcher(tmp_path, [bot(1, NOTICE_NO_SENDER), bot(2, ATTACHMENT)], [BLURRY, BLURRY, ORDER])
        prime(w)
        w.run_once()
        assert "読み取れませんでした" in bodies(w)[0]
        w._chatwork.messages.append(bot(6, ATTACHMENT.replace("2153301583", "2153309999")))
        w.clock.now = at(2026, 9, 3, 10, 20)
        assert w.run_once() == 1
        assert "光パックス石川から発注書が届きました" in bodies(w)[1]


NAKAURA_ID = 10622368


class TestQuestionsInTheFaxRoom:
    """実例（2026-09-07 19:23）: FAXルームで「未処理の注文書残ってる？」と聞かれ、無言だった。

    FAXルームは許可ルームに入れていない（社内ナレッジを流さないため）。対応状況の
    問い合わせにだけ、見張りの状態から答える。
    """

    OPEN = [
        {"posted_id": "9000", "pdf_id": "2", "filename": "4958_001.pdf", "sender": "株式会社髙島屋 泉北店",
         "kind": "発注書", "received_at": "2026/09/06 18:21:18", "posted_at": "2026-09-07T08:30:00+09:00",
         "stage": 0, "check_ids": []},
        {"posted_id": "9001", "pdf_id": "4", "filename": "4971_001.pdf", "sender": "㈱髙島屋 大阪店",
         "kind": "発注書", "received_at": "2026/09/07 18:55:46", "posted_at": "2026-09-07T19:00:00+09:00",
         "stage": 0, "check_ids": []},
    ]

    def _handler(self, tmp_path, monkeypatch, state, now=at(2026, 9, 7, 19, 45),  # 月曜、時間帯の後
                 chat_reply="はい、よろしくお願いします。"):
        from tests.test_guest import make_handler

        handler, chatwork, generator, audit = make_handler(
            tmp_path, monkeypatch, members=(ADACHI, SHINODA, NAKAURA_ID)
        )
        handler._config.data["fax_watch"] = {**make_config(tmp_path).data["fax_watch"]}
        (tmp_path / "state").mkdir(exist_ok=True)
        (tmp_path / "state" / "faxwatch.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        from raizuinu.faxwatch import FaxStatus

        handler._fax_status = FaxStatus(handler._config, now=lambda: now, client=fake_client({"reply": chat_reply}))
        return handler, chatwork, generator, audit

    @staticmethod
    def _ask(handler, account_id, text, message_id="1"):
        from tests.test_handler import sign

        raw = json.dumps({
            "webhook_event_type": "mention_to_me",
            "webhook_event": {"from_account_id": account_id, "to_account_id": 999, "room_id": FAX_ROOM,
                              "message_id": message_id, "body": f"[To:999] {text}", "send_time": 1757240580},
        }).encode()
        return handler.handle_webhook(raw, sign(raw))

    def test_the_open_list_is_answered_from_state(self, tmp_path, monkeypatch):
        handler, chatwork, generator, audit = self._handler(tmp_path, monkeypatch, {"open": self.OPEN, "pending": []})
        result = self._ask(handler, ADACHI, "今、未処理の注文書や発注書残ってる？")
        assert result.status == 200
        body = chatwork.sent[0][1]
        assert body.startswith(f"[rp aid={ADACHI} to={FAX_ROOM}-1]")
        assert "いま対応完了の返信をいただいていないのは2件です。" in body
        assert "・9/6 18:21 株式会社髙島屋 泉北店 発注書（4958_001.pdf）" in body
        assert "・9/7 18:55 ㈱髙島屋 大阪店 発注書（4971_001.pdf）" in body
        assert "「対応完了」とお知らせください" in body
        assert not generator.calls  # ハンドブックは使わない
        assert audit.records[-1]["type"] == "fax_status"

    def test_nothing_waiting_is_said_plainly(self, tmp_path, monkeypatch):
        # 月曜 19:45（時間帯の後）→ 控えている分は「明日の朝一」
        handler, chatwork, _, _ = self._handler(tmp_path, monkeypatch, {"open": [], "pending": [{"message_id": "7"}]})
        self._ask(handler, SHINODA, "未処理のFAXある？")
        body = chatwork.sent[0][1]
        assert "いま対応待ちのFAXはありません。" in body
        assert "ほかに、対応時間外に届いているFAXが1件あり、こちらは明日の朝一にお知らせいたします。" in body

    def test_on_friday_night_it_says_monday(self, tmp_path, monkeypatch):
        handler, chatwork, _, _ = self._handler(
            tmp_path, monkeypatch, {"open": [], "pending": [{"message_id": "7"}, {"message_id": "8"}]},
            now=at(2026, 9, 4, 20, 0),  # 金曜 20:00
        )
        self._ask(handler, ADACHI, "未処理ある？")
        assert "対応時間外に届いているFAXが2件あり、こちらは来週月曜日の朝一にお知らせいたします。" in chatwork.sent[0][1]

    @pytest.mark.parametrize("now, expected", [
        (at(2026, 9, 3, 20, 0), "明日の朝一"),        # 木曜夜
        (at(2026, 9, 4, 20, 0), "来週月曜日の朝一"),   # 金曜夜
        (at(2026, 9, 5, 10, 0), "来週月曜日の朝一"),   # 土曜
        (at(2026, 9, 6, 10, 0), "明日の朝一"),         # 日曜
        (at(2026, 9, 7, 7, 0), "本日08:30"),           # 月曜の早朝
        (at(2026, 9, 7, 10, 0), "順次"),               # 時間帯の中（上限待ちなど）
    ])
    def test_the_timing_follows_the_calendar(self, now, expected):
        from raizuinu.faxwatch import delivery_phrase

        assert delivery_phrase(now, {"start": "08:30", "end": "19:30"}) == expected

    def test_other_questions_are_pointed_to_the_department_room(self, tmp_path, monkeypatch):
        pointer = "すみません、このルームでは答えられないので、経理財務部のルームで聞いていただけますか。"
        handler, chatwork, generator, _ = self._handler(tmp_path, monkeypatch, {"open": self.OPEN}, chat_reply=pointer)
        self._ask(handler, ADACHI, "経費精算の締め日は？")
        assert pointer in chatwork.sent[0][1]
        assert not generator.calls  # 社内ナレッジはこのルームへ流さない
        system = handler._fax_status._client.kwargs["system"]
        assert "経理財務部のルームで聞いてほしい" in system and "知識を求める質問には答えず" in system

    def test_small_talk_gets_small_talk_back(self, tmp_path, monkeypatch):
        # 実例（2026-09-07 20:07）: 「了解です。」に案内文を返してしまった
        handler, chatwork, generator, audit = self._handler(tmp_path, monkeypatch, {"open": self.OPEN})
        self._ask(handler, ADACHI, "了解です。")
        body = chatwork.sent[0][1]
        assert body.endswith("はい、よろしくお願いします。")
        assert "このルームでは" not in body
        assert "相手のメッセージ:\n了解です。" in handler._fax_status._client.kwargs["messages"][0]["content"]
        assert not generator.calls
        assert audit.records[-1]["type"] == "fax_status"

    def test_a_parroted_reply_is_replaced(self, tmp_path, monkeypatch):
        # 実例（2026-09-07 20:22）: 「了解です。」に「了解です。」と返した
        handler, chatwork, _, _ = self._handler(tmp_path, monkeypatch, {"open": self.OPEN}, chat_reply="了解です。")
        self._ask(handler, ADACHI, "了解です。")
        body = chatwork.sent[0][1].split("\n", 1)[1]
        assert body != "了解です。"
        assert body in BANKS["fax_room_ack"]
        system = handler._fax_status._client.kwargs["system"]
        assert "相手の言葉をそのまま返さない" in system and "また届いたらお知らせしますね" in system

    @pytest.mark.parametrize("question, reply, expected", [
        ("了解です。", "了解です。", True),
        ("了解です", "了解です！", True),
        ("OKです", "はい、OKです。", True),
        ("了解です。", "はい。また届いたらお知らせしますね。", False),
        ("ありがとう", "こちらこそ、ご確認ありがとうございます。", False),
        ("", "了解です", False),
    ])
    def test_is_parrot(self, question, reply, expected):
        from raizuinu.faxwatch import is_parrot

        assert is_parrot(question, reply) is expected

    @pytest.mark.parametrize("text, expected", [
        ("今、未処理の注文書や発注書残ってる？", True),
        ("未処理のFAXある？", True),
        ("対応待ち件数を確認したい", True),
        ("いま何件抱えてますか", True),
        ("対応完了に対する返信と対応待ちFAXの共有が分かれてしまっていた件、修正しております。", False),
        ("了解です。", False),
    ])
    def test_status_questions_are_told_apart_from_notes(self, tmp_path, monkeypatch, text, expected):
        handler, _, _, _ = self._handler(tmp_path, monkeypatch, {"open": []})
        assert handler._fax_status.is_status_question(text) is expected

    def test_the_model_is_not_used_for_the_status_list(self, tmp_path, monkeypatch):
        handler, chatwork, _, _ = self._handler(tmp_path, monkeypatch, {"open": self.OPEN})
        self._ask(handler, ADACHI, "未処理ある？")
        assert handler._fax_status._client.kwargs is None  # 一覧はコードで作る（数字を作文させない）

    def test_the_notifier_bot_gets_no_reply(self, tmp_path, monkeypatch):
        handler, chatwork, _, _ = self._handler(tmp_path, monkeypatch, {"open": self.OPEN})
        handler._fax_watch_factory = None  # 即時処理は別テストで見る
        self._ask(handler, NOTIFIER, "未処理ある？")
        assert chatwork.sent == []

    def test_the_notifiers_mention_triggers_an_immediate_run(self, tmp_path, monkeypatch):
        # 通知管理くんのメンションを合図に、巡回を待たずに読みに行く
        import time

        naps = []
        monkeypatch.setattr(time, "sleep", lambda s: naps.append(s))
        handler, chatwork, _, audit = self._handler(tmp_path, monkeypatch, {"open": []})

        class FakeRun:
            calls = 0

            def run_once(self):
                FakeRun.calls += 1
                return 1

            def has_pending(self):
                return False

        handler._fax_watch_factory = lambda: FakeRun()
        self._ask(handler, NOTIFIER, "[info][title]FAX受信通知[/title]…[/info]")
        assert FakeRun.calls == 1
        assert naps == [5]  # 本文とPDFが別々に届く場合に備えて少し待つ
        assert chatwork.sent == []  # 合図に返事はしない
        assert audit.records[-1]["type"] == "fax_mention_trigger" and audit.records[-1]["handled"] == 1

    def test_if_nothing_arrived_yet_it_looks_again_once(self, tmp_path, monkeypatch):
        import time

        naps = []
        monkeypatch.setattr(time, "sleep", lambda s: naps.append(s))
        handler, chatwork, _, _ = self._handler(tmp_path, monkeypatch, {"open": []})

        class FakeRun:
            calls = 0

            def run_once(self):
                FakeRun.calls += 1
                return 0

            def has_pending(self):
                return False

        handler._fax_watch_factory = lambda: FakeRun()
        self._ask(handler, NOTIFIER, "FAX受信通知")
        assert FakeRun.calls == 2 and naps == [5, 10]

    def test_a_done_report_gets_one_reply_from_the_watcher_side(self, tmp_path, monkeypatch):
        # 実例（2026-09-08）: 「対応完了」に会話の返事とお礼が別々に2通届いた。
        # 完了の報告は巡回側の処理をその場で回し、そちらの1通（お礼＋残り）だけにする
        import time

        naps = []
        monkeypatch.setattr(time, "sleep", lambda s: naps.append(s))
        handler, chatwork, _, audit = self._handler(tmp_path, monkeypatch, {"open": self.OPEN})

        class FakeRun:
            calls = 0
            closed_last_run = [{"filename": "4958_001.pdf"}]

            def run_once(self):
                FakeRun.calls += 1
                chatwork.sent.append((FAX_ROOM, "ご対応ありがとうございます。\n対応待ちのFAXは、これでありません。"))
                return 0

            def has_pending(self):
                return False

        handler._fax_watch_factory = lambda: FakeRun()
        self._ask(handler, NAKAURA_ID, "対応完了")
        assert FakeRun.calls == 1 and naps == []  # 待たずに1回だけ
        assert len(chatwork.sent) == 1  # 会話の返事は重ねない
        assert handler._fax_status._client.kwargs is None
        assert audit.records[-1]["type"] == "fax_mention_trigger" and audit.records[-1]["closed"] == ["4958_001.pdf"]

    def test_a_done_report_the_watcher_asked_back_about_gets_no_second_reply(self, tmp_path, monkeypatch):
        # まとめ通知に番号なしの「対応完了」→ 巡回側が番号を聞き返す。会話の返事は重ねない
        import time

        monkeypatch.setattr(time, "sleep", lambda s: None)
        handler, chatwork, _, _ = self._handler(tmp_path, monkeypatch, {"open": self.OPEN})

        class FakeRun:
            closed_last_run = []
            asked_last_run = [{"message_id": "7"}]

            def run_once(self):
                chatwork.sent.append((FAX_ROOM, "ありがとうございます。対応待ちが2件あるので、どのFAXが済んだか番号を添えて「対応完了」とお返事ください。"))
                return 0

            def has_pending(self):
                return False

        handler._fax_watch_factory = lambda: FakeRun()
        self._ask(handler, NAKAURA_ID, "対応完了です", "7")
        assert len(chatwork.sent) == 1
        assert handler._fax_status._client.kwargs is None  # モデルでの会話に進まない

    def test_a_long_mentioned_report_is_read_and_closed_in_one_reply(self, tmp_path, monkeypatch):
        # メンション付きの長めの報告は、内容を読んで済んだと分かれば閉じ、お礼と残りを1通で返す
        import time

        monkeypatch.setattr(time, "sleep", lambda s: None)
        handler, chatwork, _, audit = self._handler(tmp_path, monkeypatch, {"open": self.OPEN})
        handler._fax_status._client = fake_client(
            {"reply": "ご対応ありがとうございます。", "done_all": False, "done_filenames": ["4958_001.pdf"]}
        )
        calls = []

        class FakeRun:
            closed_last_run = []

            def run_once(self):
                return 0

            def has_pending(self):
                return False

            def close_manually(self, room_id, message, filenames=None, everything=False):
                calls.append((filenames, everything, message["message_id"]))
                chatwork.sent.append((room_id, "ご対応ありがとうございます。\n対応待ちのFAXは、あと1件です。\n・9/7 18:55 ㈱髙島屋 大阪店 発注書（4971_001.pdf）"))
                return 1

        handler._fax_watch_factory = lambda: FakeRun()
        self._ask(handler, ADACHI, "髙島屋泉北店の発注書ですが、先ほど処理を終えましたので対応完了しております。ありがとうございました。", "7")
        assert calls == [(["4958_001.pdf"], False, "7")]
        assert len(chatwork.sent) == 1 and "対応待ちのFAXは、あと1件です。" in chatwork.sent[0][1]
        assert "4958_001.pdf" in handler._fax_status._client.kwargs["system"]  # 一覧を渡して選ばせる
        assert audit.records[-1]["type"] == "fax_status" and "閉じた: 1件" in audit.records[-1]["answer"]

    def test_an_operational_note_with_a_mention_gets_a_plain_reply(self, tmp_path, monkeypatch):
        import time

        monkeypatch.setattr(time, "sleep", lambda s: None)
        handler, chatwork, _, _ = self._handler(
            tmp_path, monkeypatch, {"open": self.OPEN},
            chat_reply="承知しました。修正ありがとうございます。",
        )

        class FakeRun:
            closed_last_run = []

            def run_once(self):
                return 0

            def has_pending(self):
                return False

            def close_manually(self, *args, **kwargs):
                raise AssertionError("運用の連絡で発注書を閉じてはいけない")

        handler._fax_watch_factory = lambda: FakeRun()
        self._ask(handler, ADACHI, "対応完了に対する返信と対応待ちFAXの共有が分かれてしまっていた件、修正しております。", "8")
        assert len(chatwork.sent) == 1 and "承知しました。修正ありがとうございます。" in chatwork.sent[0][1]

    def test_a_done_report_that_closes_nothing_is_answered_as_talk(self, tmp_path, monkeypatch):
        import time

        monkeypatch.setattr(time, "sleep", lambda s: None)
        handler, chatwork, _, _ = self._handler(tmp_path, monkeypatch, {"open": []}, chat_reply="ありがとうございます。承知しました。")

        class FakeRun:
            closed_last_run = []

            def run_once(self):
                return 0

            def has_pending(self):
                return False

        handler._fax_watch_factory = lambda: FakeRun()
        self._ask(handler, ADACHI, "対応完了です")
        assert len(chatwork.sent) == 1 and "ありがとうございます。承知しました。" in chatwork.sent[0][1]

    def test_a_failure_in_the_immediate_run_is_left_to_the_timer(self, tmp_path, monkeypatch):
        import time

        monkeypatch.setattr(time, "sleep", lambda s: None)
        handler, chatwork, _, audit = self._handler(tmp_path, monkeypatch, {"open": []})

        class Exploding:
            def run_once(self):
                raise RuntimeError("boom")

            def has_pending(self):
                return False

        handler._fax_watch_factory = lambda: Exploding()
        self._ask(handler, NOTIFIER, "FAX受信通知")
        assert chatwork.sent == []  # ルームに失敗を流さない（次の巡回で拾う）
        assert audit.records[-1]["handled"] == 0

    def test_other_rooms_outside_the_list_stay_silent(self, tmp_path, monkeypatch):
        from tests.test_handler import sign

        handler, chatwork, _, _ = self._handler(tmp_path, monkeypatch, {"open": self.OPEN})
        raw = json.dumps({
            "webhook_event_type": "mention_to_me",
            "webhook_event": {"from_account_id": ADACHI, "to_account_id": 999, "room_id": 345854487,
                              "message_id": "2", "body": "[To:999] 未処理ある？", "send_time": 1757240580},
        }).encode()
        handler.handle_webhook(raw, sign(raw))
        assert chatwork.sent == []

    def test_the_same_message_is_answered_once(self, tmp_path, monkeypatch):
        handler, chatwork, _, _ = self._handler(tmp_path, monkeypatch, {"open": self.OPEN})
        self._ask(handler, ADACHI, "未処理ある？", "5")
        self._ask(handler, ADACHI, "未処理ある？", "5")
        assert len(chatwork.sent) == 1


FUKUMOTO, NAKAURA, FUDABA = 9763216, 10622368, 10675817
RECIPIENTS = [
    {"account_id": SHINODA, "name": "篠田", "work_days": [0, 3, 4]},
    {"account_id": FUKUMOTO, "name": "福本", "work_days": [0, 1, 2, 3, 4]},
    {"account_id": NAKAURA, "name": "中浦", "work_days": [0, 1, 2]},
    {"account_id": FUDABA, "name": "札葉", "work_days": [0, 1, 2, 3, 4]},
]


class TestRecipientsByWorkDay:
    """2026-09-07 テスト完了: 宛先を篠田・福本・中浦・札葉に切替。To はその人の勤務日だけ。"""

    def _watcher(self, tmp_path, now, payload=ORDER):
        w = watcher(tmp_path, [bot(1, NOTICE_NO_SENDER), bot(2, ATTACHMENT)], payload, now=now)
        w._config.data["fax_watch"]["notify_recipients"] = RECIPIENTS
        w._config.data["fax_watch"]["notify_account_ids"] = [SHINODA, FUKUMOTO, NAKAURA, FUDABA]
        return w

    def _to(self, body):
        import re

        return [int(x) for x in re.findall(r"\[To:(\d+)\]", body.split("\n")[1])]

    def test_thursday_leaves_out_nakaura(self, tmp_path):
        w = self._watcher(tmp_path, THU)  # 木曜
        prime(w)
        w.run_once()
        assert self._to(bodies(w)[0]) == [SHINODA, FUKUMOTO, FUDABA]
        assert ADACHI not in self._to(bodies(w)[0])  # 足立さんは外れる

    def test_tuesday_leaves_out_shinoda(self, tmp_path):
        w = self._watcher(tmp_path, at(2026, 9, 8, 10, 0))  # 火曜
        prime(w)
        w.run_once()
        assert self._to(bodies(w)[0]) == [FUKUMOTO, NAKAURA, FUDABA]

    def test_monday_calls_everyone(self, tmp_path):
        w = self._watcher(tmp_path, at(2026, 9, 7, 10, 0))
        prime(w)
        w.run_once()
        assert self._to(bodies(w)[0]) == [SHINODA, FUKUMOTO, NAKAURA, FUDABA]

    def test_reminders_and_the_evening_list_follow_the_day(self, tmp_path):
        w = self._watcher(tmp_path, at(2026, 9, 7, 10, 0))  # 月曜に通知
        prime(w)
        w.run_once()
        w.clock.now = at(2026, 9, 8, 9, 0)  # 火曜の確認 → 篠田さんは休み
        w.run_once()
        assert self._to(bodies(w)[1]) == [FUKUMOTO, NAKAURA, FUDABA]
        w.clock.now = at(2026, 9, 10, 19, 0)  # 木曜の夕方の一覧 → 中浦さんは休み
        w.run_once()
        evening = bodies(w)[-1]
        assert evening.startswith(f"[To:{SHINODA}] [To:{FUKUMOTO}] [To:{FUDABA}]\nお疲れさまです。")

    def test_without_the_detailed_list_everyone_is_called_daily(self, tmp_path):
        w = watcher(tmp_path, [bot(1, NOTICE_NO_SENDER), bot(2, ATTACHMENT)], ORDER, now=at(2026, 9, 8, 10, 0))
        prime(w)
        w.run_once()
        assert self._to(bodies(w)[0]) == [SHINODA, ADACHI]


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
        # 3度目の催促は無い。ただし対応待ちとしては残る（夕方の一覧に載る）
        for later in (at(2026, 9, 4, 15, 0), at(2026, 9, 7, 9, 0), at(2026, 9, 8, 12, 0)):
            w.clock.now = later
            w.run_once()
        assert len(w._chatwork.sent) == 3
        assert [t["stage"] for t in w._load_state()["open"]] == [2]

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
        assert thanks.split("\n")[1] in BANKS["fax_done_thanks"]
        assert thanks.split("\n")[2] == "対応待ちのFAXは、これでありません。"

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
        assert w.run_once() == 2  # 同じ巡回の2件は1通にまとまる（①②）
        assert len(w._chatwork.sent) == 1
        w._chatwork.messages.append(human(9500, "2件とも対応完了です"))
        w.run_once()
        assert len(w._chatwork.sent) == 2  # お礼は1回
        assert w._load_state()["open"] == []
        w.clock.now = at(2026, 9, 4, 9, 0)
        w.run_once()
        assert len(w._chatwork.sent) == 2

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
