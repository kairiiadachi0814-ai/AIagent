"""支払通知書の月次発行。

指示（2026-11-20）: 宮﨑さんの業務委託契約について、領収書の画像を受け付け、支払通知書を作ってPDFにし、
経理財務部のメンバー（足立・坂田・岩永のうち1名）の承認を得てから、締日の翌日から5営業日以内に
宮﨑さんのルームで交付する。定例の業務委託費（月10万円・税込）の下に、領収書ごとに1行ずつ載せる。
"""

import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from raizuinu.config import Config
from raizuinu.gsheets import column_index, date_serial
from raizuinu.paynotice import (
    AWAITING,
    EXCLUDED,
    ISSUED,
    JST,
    OK,
    REVIEW,
    Calendar,
    NoticeError,
    PaymentNoticeRunner,
    attachments_of,
    clean_registration,
    registration_verdict,
    repair_registration,
    build_lines,
    check_receipt,
    is_plain_approval,
    period_bounds,
    period_of,
    previous_period,
    receipt_label,
    total_of,
)

PAYEE_ROOM, APPROVAL_ROOM, ADMIN_ROOM, PAYMENT_ROOM = 453639844, 384793683, 444945031, 64547366
ME, MIYAZAKI, ADACHI, SAKATA, IWANAGA, SHINODA, ITO = 11574026, 8923331, 6945415, 8681926, 11481620, 9228914, 9129422
TEMPLATE = "2026年10月(源泉なし)"
HOLIDAYS = {"2026-11-03", "2026-11-23", "2027-01-01", "2027-01-11"}


class FakeChatwork:
    def __init__(self):
        self.rooms, self.sent, self.uploads, self.files, self._next = {}, [], [], {}, 1000
        self.fail_upload = 0

    def _add(self, room, account, body, at=None):
        self._next += 1
        self.rooms.setdefault(room, []).append({
            "message_id": str(self._next), "account": {"account_id": account}, "body": body,
            "send_time": int((at or datetime(2026, 11, 20, 10, 0, tzinfo=JST)).timestamp()),
        })
        return str(self._next)

    def say(self, room, account, body, at=None):
        return self._add(room, account, body, at)

    def attach(self, room, account, names, text="", at=None):
        tags = ""
        for name in names:
            self._next += 1
            self.files[self._next] = name
            tags += f"[info][title][dtext:file_uploaded][/title][download:{self._next}]{name} (1.2 MB)[/download][/info]"
        return self._add(room, account, text + tags, at)

    def reply(self, room, account, target, text):
        return self._add(room, account, f"[rp aid={ME} to={room}-{target}]経理財務アシスタントさん\n{text}")

    def get_me(self):
        return ME

    def get_recent_messages(self, room_id, limit=100):
        return list(self.rooms.get(room_id, []))[-limit:]

    def get_file_info(self, room_id, file_id):
        return {"filename": self.files[file_id], "download_url": f"https://files.example/{file_id}", "filesize": 1000}

    def send_message(self, room_id, body):
        mid = self._add(room_id, ME, body)
        self.sent.append((room_id, body))
        return mid

    def upload_file(self, room_id, filename, data, message=""):
        if self.fail_upload:
            self.fail_upload -= 1
            raise RuntimeError("upload failed")
        self._next += 1
        file_id = self._next
        self._add(room_id, ME, f"{message}[download:{file_id}]{filename}[/download]")
        self.uploads.append((room_id, filename, data, message))
        return str(file_id)

    def posts(self, room):
        return [body for r, body in self.sent if r == room]


class FakeSheets:
    """スプレッドシートの差し替え。合計は「数量×単価」の和で出す（ひな形の数式の代わり）。"""

    def __init__(self):
        self.tabs = {TEMPLATE: {"id": 1, "cells": {"L20": 1, "M20": "式", "N20": 100000}, "rows": 0}}
        self._next = 100
        self.deleted, self.wrong_total, self.formatted = [], None, []

    def sheets(self):
        return [{"id": t["id"], "title": name, "index": i, "hidden": False, "rows": 45}
                for i, (name, t) in enumerate(self.tabs.items())]

    def find(self, title):
        return next((s for s in self.sheets() if s["title"] == title), None)

    def duplicate(self, source, new_title):
        assert new_title not in self.tabs
        self._next += 1
        self.tabs[new_title] = {"id": self._next, "cells": dict(self.tabs[source]["cells"]), "rows": 0}
        return self._next

    def add_row_like(self, sheet_id, row, last_column):
        next(t for t in self.tabs.values() if t["id"] == sheet_id)["rows"] += 1

    def copy_row_format(self, sheet_id, source_row, first_row, last_row, last_column):
        if last_row >= first_row:
            self.formatted.append((source_row, first_row, last_row))

    def write(self, title, values):
        self.tabs[title]["cells"].update(values)

    def read_cell(self, title, cell):
        if self.wrong_total is not None:
            return self.wrong_total
        cells = self.tabs[title]["cells"]
        rows = {int(k[1:]) for k in cells if k[0] == "N"}
        return sum(int(cells.get(f"L{r}") or 0) * int(cells.get(f"N{r}") or 0) for r in rows)

    def delete_sheet(self, sheet_id):
        name = next(n for n, t in self.tabs.items() if t["id"] == sheet_id)
        self.deleted.append(name)
        del self.tabs[name]

    def export_pdf(self, sheet_id):
        name, tab = next((n, t) for n, t in self.tabs.items() if t["id"] == sheet_id)
        payload = json.dumps({k: (v.isoformat() if isinstance(v, date) else v) for k, v in sorted(tab["cells"].items())},
                             ensure_ascii=False)
        return b"%PDF-" + f"{name}|{payload}".encode("utf-8")


class FakeModel:
    def __init__(self):
        self.receipts, self.replies, self.calls, self.fail = {}, [], [], 0
        self.rereads = {}   # 登録番号の読み直し: ファイル名 → {"numbers": [...]}
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, messages, **kwargs):
        if self.fail:
            self.fail -= 1
            raise RuntimeError("overloaded")
        content = messages[0]["content"]
        if kwargs["system"].startswith("登録番号だけ"):
            text = content[-1]["text"]
            found = next((v for k, v in self.rereads.items() if k in text), {"numbers": []})
            self.calls.append(("reread", text))
        elif "領収書・レシート" in kwargs["system"]:
            text = content[-1]["text"]
            found = next((v for k, v in self.receipts.items() if k in text), {"receipts": [], "note": "証憑ではありません"})
            self.calls.append(("receipt", text))
        else:
            found = self.replies.pop(0)
            self.calls.append(("reply", content))
        return SimpleNamespace(stop_reason="end_turn", usage=SimpleNamespace(input_tokens=1500, output_tokens=200),
                               content=[SimpleNamespace(type="text", text=json.dumps(found, ensure_ascii=False))])


def receipt(day="2026-11-12", payee="近畿日本鉄道", amount=1240, purpose="交通費"):
    return {"date": day, "payee": payee, "amount": amount, "purpose": purpose, "kind": "領収書",
            "tax_note": "10%", "registration": ""}


def op(name, number=0, amount=0, day="", label="", exempt=False):
    return {"op": name, "number": number, "amount": amount, "date": day, "label": label, "exempt": exempt}


@pytest.fixture
def env(tmp_path):
    config = Config.load()  # 本物の設定（契約の中身ごと確かめる）
    config.base_dir = tmp_path
    config.data = json.loads(json.dumps(config.data))
    config.data["state_dir"] = str(tmp_path / "state")
    config.data["audit_log_dir"] = str(tmp_path / "audit")
    config.data["payment_notice"]["live"] = True
    # 振込の依頼は、それを確かめるテストでだけ有効にする（`with_payment_request`）
    config.data["payment_notice"]["saved_payment_request"] = config.data["payment_notice"]["payment_request"]
    config.data["payment_notice"]["payment_request"] = {}
    cw, sheets, model = FakeChatwork(), FakeSheets(), FakeModel()
    clock = SimpleNamespace(now=datetime(2026, 11, 20, 10, 0, tzinfo=JST))
    naps = []
    runner = PaymentNoticeRunner(
        config, cw, client=model, now=lambda: clock.now, sheets=lambda spreadsheet_id: sheets,
        download=lambda url: (200, b"\xff\xd8image-bytes"), calendar=Calendar(HOLIDAYS), sleep=naps.append,
    )
    return SimpleNamespace(config=config, cw=cw, sheets=sheets, model=model, clock=clock, runner=runner, tmp=tmp_path,
                           naps=naps)


def state_of(env, name="paynotice.json"):
    return json.loads((env.tmp / "state" / name).read_text(encoding="utf-8"))["contracts"]["miyazaki"]


def at(month, day, hour=9, minute=0, year=2026):
    return datetime(year, month, day, hour, minute, tzinfo=JST)


def submit(env, names=("IMG_0001.jpg",), when=None, **found):
    for name in names:
        env.model.receipts.setdefault(name, {"receipts": [receipt(**found)], "note": ""})
    return env.cw.attach(PAYEE_ROOM, MIYAZAKI, list(names), at=when or at(11, 20, 10))


def december(env):
    """11月分の証憑を1件受け付け、12/1 の朝に支払通知書を作らせる。→ 承認依頼の投稿ID"""
    submit(env)
    env.runner.run_once()
    env.clock.now = at(12, 1, 9, 0)
    env.runner.run_once()
    return state_of(env)["notices"]["2026-11"]["request_ids"][0]


# --- 月と営業日 ---


def test_the_period_is_the_month_the_evidence_was_handed_in():
    assert period_of(date(2026, 11, 30)) == "2026-11" and period_of(date(2026, 12, 1)) == "2026-12"
    assert previous_period(date(2026, 12, 1)) == "2026-11" and previous_period(date(2027, 1, 4)) == "2026-12"
    assert period_bounds("2026-11") == (date(2026, 11, 1), date(2026, 11, 30))
    assert period_bounds("2027-02")[1] == date(2027, 2, 28)


def test_the_fifth_business_day_after_closing_is_the_deadline():
    calendar = Calendar(HOLIDAYS)
    assert calendar.nth_after(date(2026, 11, 30), 5) == date(2026, 12, 7)     # 12/1・2・3・4・7
    assert calendar.nth_after(date(2026, 10, 31), 5) == date(2026, 11, 9)     # 11/3（祝）を飛ばす
    assert calendar.nth_after(date(2026, 12, 31), 5) == date(2027, 1, 8)      # 1/1（祝）・土日を飛ばす
    closed = Calendar(HOLIDAYS, ["2026-12-29/2027-01-05"])
    assert closed.nth_after(date(2026, 12, 31), 5) == date(2027, 1, 13)       # 1/6・7・8・12・13（1/11 は祝日）
    assert calendar.days_left(date(2026, 12, 3), date(2026, 12, 7)) == 3
    assert calendar.days_left(date(2026, 12, 8), date(2026, 12, 7)) == -1


def test_the_payment_day_moves_back_when_the_bank_is_closed():
    calendar = Calendar(HOLIDAYS)
    assert calendar.on_or_before(date(2026, 12, 15)) == date(2026, 12, 15)
    assert calendar.on_or_before(date(2026, 11, 15)) == date(2026, 11, 13)    # 日曜 → 金曜
    assert calendar.on_or_before(date(2027, 8, 15)) == date(2027, 8, 13)


def test_dates_go_into_the_sheet_as_serial_numbers():
    assert date_serial(date(2026, 11, 19)) == 46345
    assert column_index("A") == 0 and column_index("R") == 17 and column_index("AA") == 26


# --- 行・点検・承認の読み取り ---


def test_the_first_line_is_the_monthly_fee_then_one_line_per_receipt(env):
    contract = env.runner.contract()
    receipts = [
        {"id": "b-1", "period": "2026-11", "status": OK, "date": "2026-11-20", "amount": 3300, "payee": "コクヨ", "purpose": "文具"},
        {"id": "a-1", "period": "2026-11", "status": OK, "date": "2026-11-12", "amount": 1240, "payee": "近鉄", "purpose": "交通費"},
        {"id": "c-1", "period": "2026-11", "status": REVIEW, "date": "", "amount": 0},
        {"id": "d-1", "period": "2026-12", "status": OK, "date": "2026-12-01", "amount": 500},
    ]
    lines = build_lines(contract, "2026-11", receipts)
    assert [(l["kind"], l["date"], l["label"], l["price"], l["exempt"]) for l in lines] == [
        ("fee", "2026-11-30", "ヤマトライジング業務委託費", 100000, False),
        ("receipt", "2026-11-12", "立替：交通費（近鉄）", 1240, True),
        ("receipt", "2026-11-20", "立替：文具（コクヨ）", 3300, True),
    ]
    assert lines[0]["quantity"] == 1 and lines[0]["unit"] == "式"
    assert total_of(lines) == 104540
    special = build_lines(contract, "2026-11", [], {"fee_amount": 50000})
    assert special[0]["price"] == 50000


def test_the_content_column_stays_short_enough_to_fit():
    label = receipt_label
    assert label({"purpose": "交通費", "payee": "近鉄"}) == "立替：交通費（近鉄）"
    assert label({"purpose": "交通費（近鉄奈良〜大阪難波）", "payee": "近畿日本鉄道株式会社"}) == "立替：交通費（近鉄奈良〜大阪難波） 近畿日本鉄道"
    assert label({"purpose": "駐車料金", "payee": "タイムズ24株式会社（タイムズ奈良東向南町）"}) == "立替：駐車料金（タイムズ24）"
    assert label({"purpose": "", "payee": ""}) == "立替：経費"
    long = label({"purpose": "チーム運営に関する打合せの会議飲食代および資料印刷代", "payee": "奈良ホテル"})
    assert len(long) <= 30 and long.startswith("立替：チーム運営")


def test_a_receipt_is_checked_before_it_is_listed():
    submitted, start = date(2026, 11, 20), date(2026, 10, 1)
    good = receipt()
    assert check_receipt(good, submitted, start, []) == []
    assert "金額を読み取れませんでした" in check_receipt(receipt(amount=0), submitted, start, [])
    assert "日付を読み取れませんでした" in check_receipt(receipt(day=""), submitted, start, [])
    assert any("提出日より後" in i for i in check_receipt(receipt(day="2026-11-25"), submitted, start, []))
    assert any("契約の開始日より前" in i for i in check_receipt(receipt(day="2026-09-30"), submitted, start, []))
    assert any("重複" in i for i in check_receipt(good, submitted, start, [{**good, "status": OK}]))
    assert check_receipt(good, submitted, start, [{**good, "status": EXCLUDED}]) == []
    assert any("金額が大きい" in i for i in check_receipt(receipt(amount=20_000_000), submitted, start, []))


def test_only_a_bare_approval_is_read_without_the_model():
    for text in ("承認", "承認します。", "承認いたします", "承認です！", "OK"):
        assert is_plain_approval(text)
    for text in ("②を外して承認", "承認できません", "未承認です", "承認は少し待ってください", "確認しました"):
        assert not is_plain_approval(text)


def test_attachments_are_found_in_a_post():
    body = "領収書です[info][title][dtext:file_uploaded][/title][preview id=9 ht=100][download:9]IMG_1.jpg (2.3 MB)[/download][/info]"
    assert attachments_of(body) == [(9, "IMG_1.jpg")]
    assert attachments_of("よろしくお願いします") == []


# --- 1. 証憑の受付 ---


def test_a_receipt_image_is_read_and_shown_back(env):
    mid = submit(env)
    env.runner.run_once()
    reply = env.cw.posts(PAYEE_ROOM)[0]
    assert reply.startswith(f"[rp aid={MIYAZAKI} to={PAYEE_ROOM}-{mid}]\n領収書を受け付けました（2026年11月分）。")
    assert "・11/12 近畿日本鉄道 交通費 1,240円" in reply
    assert "12月初めにお渡しする支払通知書に載せます。" in reply
    saved = state_of(env)["receipts"][0]
    assert saved["period"] == "2026-11" and saved["amount"] == 1240 and saved["status"] == OK
    env.runner.run_once()
    assert len(env.cw.posts(PAYEE_ROOM)) == 1  # 同じ投稿を二度読まない


def test_several_images_and_several_receipts_in_one_image(env):
    env.model.receipts["two.jpg"] = {"receipts": [receipt(), receipt(day="2026-11-13", amount=880)], "note": ""}
    submit(env, names=("two.jpg", "IMG_0002.jpg"), day="2026-11-15", payee="タイムズ", amount=600, purpose="駐車料金")
    env.runner.run_once()
    assert [r["amount"] for r in state_of(env)["receipts"]] == [1240, 880, 600]
    assert env.cw.posts(PAYEE_ROOM)[0].count("\n・") == 3


def test_evidence_handed_in_after_the_closing_day_goes_to_the_next_month(env):
    submit(env, when=at(11, 30, 23, 50))
    submit(env, names=("late.jpg",), when=at(12, 1, 0, 10), day="2026-11-29", amount=500)
    env.clock.now = at(12, 1, 0, 20)
    env.runner.run_once()
    assert [r["period"] for r in state_of(env)["receipts"]] == ["2026-11", "2026-12"]
    assert "（2026年12月分）" in env.cw.posts(PAYEE_ROOM)[1] and "1月初め" in env.cw.posts(PAYEE_ROOM)[1]


def test_an_amount_that_cannot_be_read_is_not_guessed(env):
    submit(env, amount=0)
    env.runner.run_once()
    reply = env.cw.posts(PAYEE_ROOM)[0]
    assert "金額を読み取れませんでした" in reply and "経理担当が確認します" in reply and "受け付けました" not in reply
    assert state_of(env)["receipts"][0]["status"] == REVIEW


def test_a_picture_that_is_not_a_receipt_is_said_so(env):
    env.cw.attach(PAYEE_ROOM, MIYAZAKI, ["photo.jpg", "memo.docx"])
    env.runner.run_once()
    reply = env.cw.posts(PAYEE_ROOM)[0]
    assert "「photo.jpg」は、領収書として読み取れませんでした。" in reply
    assert "「memo.docx」は画像・PDFではないため、読み取っていません。" in reply
    assert state_of(env)["receipts"] == []


def test_files_from_other_members_are_not_taken_as_evidence(env):
    env.cw.attach(PAYEE_ROOM, IWANAGA, ["業務委託契約書.pdf"])   # テスト役でも委託先でもない
    env.runner.run_once()
    assert env.cw.sent == [] and env.model.calls == []


def test_a_message_to_the_assistant_is_passed_on_not_answered(env):
    mid = env.cw.say(PAYEE_ROOM, MIYAZAKI, f"[To:{ME}]経理財務アシスタントさん\n先日の領収書の金額が1,420円の誤りでした。")
    env.cw.say(PAYEE_ROOM, MIYAZAKI, f"[To:{IWANAGA}]岩永さん\n来週の打合せの件です。")   # 人あての発言には関わらない
    env.cw.say(PAYEE_ROOM, MIYAZAKI, f"[To:{ME}]経理財務アシスタントさん\nありがとうございます！")
    env.runner.run_once()
    relayed = env.cw.posts(APPROVAL_ROOM)
    assert len(relayed) == 1
    assert relayed[0].startswith(f"[To:{ADACHI}]足立さん [To:{SAKATA}]坂田さん [To:{IWANAGA}]岩永さん\n宮﨑 大樹様から、次のご連絡がありました。")
    assert "「先日の領収書の金額が1,420円の誤りでした。」" in relayed[0] and f"#!rid{PAYEE_ROOM}-{mid}" in relayed[0]
    assert env.cw.posts(PAYEE_ROOM) == [f"[rp aid={MIYAZAKI} to={PAYEE_ROOM}-{mid}]\nご連絡ありがとうございます。経理担当へお伝えしました。担当から折り返しご連絡します。"]


def test_a_read_failure_is_retried_then_handed_to_people(env):
    submit(env)
    env.model.fail = 2
    env.runner.run_once()
    env.runner.run_once()
    assert env.cw.sent == []
    env.runner.run_once()
    assert "領収書を受け付けました" in env.cw.posts(PAYEE_ROOM)[0]

    submit(env, names=("IMG_0009.jpg",))
    env.model.fail = 3
    for _ in range(3):
        env.runner.run_once()
    assert "読み取れませんでした。経理担当が確認します。" in env.cw.posts(PAYEE_ROOM)[1]
    assert "届いた証憑を読み取れませんでした" in env.cw.posts(APPROVAL_ROOM)[0]


# --- 読み取りテスト（本番前に、実物の領収書で確かめる） ---


def test_a_testers_receipt_is_read_and_reported_but_not_kept(env):
    # 指示（2026-11-20）: 宮﨑さんではなく足立さんが、宮﨑さんのルームに領収書の写真を上げて読み取りを試す
    env.config.data["payment_notice"]["live"] = False   # 本番前でも動く
    env.model.receipts["real.jpg"] = {"receipts": [{**receipt(payee="近畿日本鉄道株式会社", purpose="交通費（近鉄奈良〜大阪難波）", amount=680),
                                                    "registration": "T2810826779764"}], "note": ""}
    mid = env.cw.attach(PAYEE_ROOM, ADACHI, ["real.jpg"])
    assert env.runner.run_once() == 1
    reply = env.cw.posts(PAYEE_ROOM)[0]
    assert reply.startswith(f"[rp aid={ADACHI} to={PAYEE_ROOM}-{mid}]\n【読み取りテスト】次のように読み取りました。"
                            "テストのため控えには残さず、支払通知書にも載せません。")
    assert "■ real.jpg" in reply
    assert "① 日付: 11月12日（木）／支払先: 近畿日本鉄道株式会社／金額: 680円" in reply
    assert "　 用途: 交通費（近鉄奈良〜大阪難波）／種類: 領収書／税率の記載: 10%\n　 登録番号: T2810826779764\n" in reply
    env.model.receipts["odd.jpg"] = {"receipts": [{**receipt(), "registration": "T12345678901234"}], "note": ""}
    env.cw.attach(PAYEE_ROOM, ADACHI, ["odd.jpg"])
    env.runner.run_once()
    assert "登録番号: T12345678901234（桁数が合わず、読み違いの疑いがあります）" in env.cw.posts(PAYEE_ROOM)[1]
    env.cw.sent.pop()
    assert "支払通知書の内容欄: 立替：交通費（近鉄奈良〜大阪難波） 近畿日本鉄道" in reply
    assert not (env.tmp / "state" / "paynotice.json").exists()   # 本番の控えには何も残らない
    env.runner.run_once()
    assert len(env.cw.posts(PAYEE_ROOM)) == 1                   # 同じ投稿を二度読まない


def test_each_receipt_in_one_photo_gets_its_own_number(env):
    # 指摘（2026-11-23）: レシート3枚を1枚の写真で上げたら、読み取りの結果が羅列になり確かめにくかった。
    # 証憑ごとに①②…を振り、1枚ずつ間を空ける。番号は返信の中の通し番号（2つ目の写真は続き）
    env.model.receipts["three.jpg"] = {"receipts": [
        {**receipt(day="2026-11-03", payee="MY NEIGHBOR", amount=6917, purpose="飲食代"), "registration": "T4120001256055"},
        {**receipt(day="2026-11-03", payee="セブン-イレブン 大阪靱本町2丁目店", amount=500, purpose="飲食料品"), "registration": "T3810281025762"},
        {**receipt(day="2026-11-03", payee="LAWSON 近鉄奈良駅前店", amount=873, purpose="飲食料品"), "registration": "T2810826779764"},
    ], "note": ""}
    env.model.receipts["one.jpg"] = {"receipts": [receipt()], "note": ""}
    env.cw.attach(PAYEE_ROOM, ADACHI, ["three.jpg", "one.jpg"])
    env.runner.run_once()
    reply = env.cw.posts(PAYEE_ROOM)[0]
    assert ("■ three.jpg（3件）\n"
            "① 日付: 11月3日（火）／支払先: MY NEIGHBOR／金額: 6,917円\n"
            "　 用途: 飲食代／種類: 領収書／税率の記載: 10%\n"
            "　 登録番号: T4120001256055\n"
            "　 支払通知書の内容欄: 立替：飲食代（MY NEIGHBOR）\n"
            "\n"
            "② 日付: 11月3日（火）／支払先: セブン-イレブン 大阪靱本町2丁目店／金額: 500円\n") in reply
    assert "\n\n③ 日付: 11月3日（火）／支払先: LAWSON 近鉄奈良駅前店／金額: 873円\n" in reply
    assert "■ one.jpg\n④ 日付: 11月12日（木）" in reply
    assert [c[0] for c in env.model.calls] == ["receipt", "receipt"]   # 番号が合っていれば読み直さない


def test_a_registration_number_that_does_not_add_up_is_read_again(env):
    # 実例（2026-11-23）: 「T4120001256055」を「T41200001256055」と読んだ（0 が3つ続く所を4つに）
    wrong = {**receipt(payee="MY NEIGHBOR", amount=6917, purpose="飲食代"), "registration": "T41200001256055"}
    env.model.receipts["a.jpg"] = {"receipts": [receipt(amount=300), wrong], "note": ""}
    env.model.rereads["a.jpg"] = {"numbers": [{"index": 2, "registration": "T4120001256055"}]}
    env.cw.attach(PAYEE_ROOM, ADACHI, ["a.jpg"])
    env.runner.run_once()
    reply = env.cw.posts(PAYEE_ROOM)[0]
    assert "登録番号: T4120001256055\n" in reply and "T41200001256055" not in reply   # 読み直して、検査用の数字まで合った
    kind, asked = env.model.calls[-1]
    assert kind == "reread" and "2: 支払先「MY NEIGHBOR」／金額 6917円／先の読み「T41200001256055」（数字が14桁で、13桁と合わない）" in asked
    assert "1: " not in asked                                                       # 番号の無い証憑は聞かない


def test_a_miscounted_run_is_repaired_and_shown_as_such(env):
    # 読み直しても同じ読み違いなら、続いている数字の数え違いとして直し、その旨を添えて人に確かめてもらう
    env.model.receipts["b.jpg"] = {"receipts": [{**receipt(), "registration": "T41200001256055"}], "note": ""}
    env.model.rereads["b.jpg"] = {"numbers": [{"index": 1, "registration": "T41200001256055"}]}
    env.cw.attach(PAYEE_ROOM, ADACHI, ["b.jpg"])
    env.runner.run_once()
    assert ("登録番号: T4120001256055（「T41200001256055」と読みましたが桁数が合わないため、"
            "検査用の数字が合う形に直しています。画像でご確認ください）") in env.cw.posts(PAYEE_ROOM)[0]


def test_a_number_that_fails_the_check_digit_is_flagged_not_dropped(env):
    # 13桁でも、1文字の読み違いは検査用の数字で分かる。個人の事業者の番号かもしれないので、決めつけない
    env.model.receipts["c.jpg"] = {"receipts": [{**receipt(), "registration": "T4120001256065"}], "note": ""}
    env.cw.attach(PAYEE_ROOM, ADACHI, ["c.jpg"])
    env.runner.run_once()
    assert ("登録番号: T4120001256065（法人番号の検査用の数字と合いません。"
            "個人の事業者の番号でなければ、読み違いの疑いがあります）") in env.cw.posts(PAYEE_ROOM)[0]


def test_the_kept_registration_number_is_the_checked_one(env):
    env.model.receipts["IMG_0001.jpg"] = {"receipts": [{**receipt(), "registration": "T41200001256055"}], "note": ""}
    env.model.receipts["IMG_0002.jpg"] = {"receipts": [{**receipt(amount=500), "registration": "T12345678901234"}], "note": ""}
    submit(env, names=("IMG_0001.jpg", "IMG_0002.jpg"))
    env.runner.run_once()
    assert [r["registration"] for r in state_of(env)["receipts"]] == ["T4120001256055", ""]   # 直せない番号は控えない


def test_a_test_reading_shows_what_could_not_be_read(env):
    env.model.receipts["two.jpg"] = {"receipts": [receipt(amount=0), receipt(day="2026-09-01", amount=500)], "note": ""}
    env.cw.attach(PAYEE_ROOM, ADACHI, ["two.jpg", "blur.jpg", "memo.docx"])
    env.runner.run_once()
    reply = env.cw.posts(PAYEE_ROOM)[0]
    assert "■ two.jpg（2件）" in reply and "金額: 読み取れず" in reply
    assert "【要確認】金額を読み取れませんでした" in reply and "【要確認】日付（2026-09-01）が契約の開始日より前です" in reply
    assert "■ blur.jpg\n　領収書として読み取れませんでした。（証憑ではありません）" in reply
    assert "■ memo.docx\n　画像（JPEG・PNG・GIF・WebP）とPDF以外は読み取れません。" in reply
    assert state_of(env)["receipts"] == [] if (env.tmp / "state" / "paynotice.json").exists() else True


def test_a_testers_receipt_never_reaches_the_notice(env):
    env.cw.attach(PAYEE_ROOM, ADACHI, ["IMG_0001.jpg"])
    env.model.receipts["IMG_0001.jpg"] = {"receipts": [receipt()], "note": ""}
    env.runner.run_once()
    env.clock.now = at(12, 1, 9, 0)
    env.runner.run_once()
    notice = state_of(env)["notices"]["2026-11"]
    assert notice["total"] == 100000 and len(notice["lines"]) == 1   # 定例の業務委託費だけ


def test_without_a_tester_the_admins_files_are_left_alone(env):
    env.config.data["payment_notice"]["contracts"][0]["test_submitter_account_ids"] = []
    env.cw.attach(PAYEE_ROOM, ADACHI, ["IMG_0001.jpg"])
    env.runner.run_once()
    assert env.cw.sent == [] and env.model.calls == []


# --- 2. 作成 ---


def test_the_notice_is_made_on_the_first_business_morning(env):
    submit(env)
    env.runner.run_once()
    env.clock.now = at(11, 30, 9, 0)
    env.runner.run_once()
    env.clock.now = at(12, 1, 8, 59)
    env.runner.run_once()
    assert env.cw.posts(APPROVAL_ROOM) == []   # 月内・9時前には作らない
    env.clock.now = at(12, 1, 9, 0)
    env.runner.run_once()
    notice = state_of(env)["notices"]["2026-11"]
    assert notice["status"] == AWAITING and notice["version"] == 1
    assert notice["issue_date"] == "2026-12-01" and notice["deadline"] == "2026-12-07" and notice["payment_date"] == "2026-12-15"
    assert notice["total"] == 101240
    cells = env.sheets.tabs["2026年11月(源泉なし)"]["cells"]
    assert cells["Q4"] == date(2026, 12, 1)
    assert (cells["B20"], cells["D20"], cells["L20"], cells["M20"], cells["N20"], cells["K20"]) == (
        date(2026, 11, 30), "ヤマトライジング業務委託費", 1, "式", 100000, None)
    assert (cells["B21"], cells["D21"], cells["K21"], cells["L21"], cells["M21"], cells["N21"]) == (
        date(2026, 11, 12), "立替：交通費（近畿日本鉄道）", "※", 1, "式", 1240)
    assert TEMPLATE in env.sheets.tabs  # ひな形は残る


def test_the_request_shows_every_line_and_the_way_to_answer(env):
    december(env)
    request = env.cw.posts(APPROVAL_ROOM)[0]
    assert request.startswith(f"[To:{ADACHI}]足立さん [To:{SAKATA}]坂田さん [To:{IWANAGA}]岩永さん\n"
                              "【承認のお願い】宮﨑 大樹様 2026年11月分 支払通知書（第1版）")
    assert "発行日: 12月1日（火）／交付期限: 12月7日（月）／支払予定日: 12月15日（火）" in request
    assert "合計: 101,240円" in request
    assert "① 11/30 ヤマトライジング業務委託費 100,000円" in request
    assert "② 11/12 立替：交通費（近畿日本鉄道） 1,240円 ※課税対象外　証憑: https://www.chatwork.com/#!rid453639844-" in request
    assert "経費は、事前に承認されたものかどうかも合わせてご確認ください。" in request
    assert "この投稿への返信で「承認」" in request and "どなたか1名で結構です" in request
    room, filename, data, _ = env.cw.uploads[0]
    # 名前の先頭は発行日（月.日・半角）。12/1 に作ったので「12.1-」
    assert room == APPROVAL_ROOM and filename == "12.1-宮﨑大樹様-ヤマトライジング業務委託費支払通知書2026年11月分.pdf"
    assert data.startswith(b"%PDF-") and env.cw.posts(PAYEE_ROOM)[1:] == []  # 委託先にはまだ何も出さない


def test_a_month_without_receipts_has_only_the_fee(env):
    env.clock.now = at(12, 1, 9, 0)
    env.runner.run_once()
    assert env.sheets.formatted == []   # 2行目までは、ひな形の書式のまま
    notice = state_of(env)["notices"]["2026-11"]
    assert notice["total"] == 100000 and len(notice["lines"]) == 1
    assert "事前に承認されたもの" not in env.cw.posts(APPROVAL_ROOM)[0]


def test_nothing_is_made_before_the_first_period_or_after_the_term(env):
    env.clock.now = at(11, 20, 9, 30)   # 10月分は手で発行済み（first_period が 2026-11）
    env.runner.run_once()
    assert state_of(env)["notices"] == {}
    env.clock.now = at(11, 2, 9, 30, year=2027)   # 契約は 2027/9/30 まで
    env.runner.run_once()
    assert "2027-10" not in state_of(env)["notices"]


def test_more_receipts_than_rows_add_rows(env):
    env.model.receipts["many.jpg"] = {"receipts": [receipt(day=f"2026-11-{d:02d}", amount=100 + d) for d in range(1, 17)], "note": ""}
    submit(env, names=("many.jpg",))
    env.runner.run_once()
    env.clock.now = at(12, 1, 9, 0)
    env.runner.run_once()
    tab = env.sheets.tabs["2026年11月(源泉なし)"]
    assert tab["rows"] == 2 and tab["cells"]["N36"] == 116   # 17行（定例1＋16件）で、ひな形の15行に2行足す
    assert env.sheets.formatted == [(21, 22, 36)]            # 3行目からは、2行目の書式にそろえる
    assert state_of(env)["notices"]["2026-11"]["total"] == 100000 + sum(100 + d for d in range(1, 17))


def test_a_tab_made_by_hand_is_never_overwritten(env):
    env.sheets.tabs["2026年11月(源泉なし)"] = {"id": 55, "cells": {}, "rows": 0}
    env.clock.now = at(12, 1, 9, 0)
    env.runner.run_once()
    assert "2026-11" not in state_of(env)["notices"] and env.sheets.deleted == []
    alert = env.cw.posts(APPROVAL_ROOM)[0]
    assert "作成できませんでした" in alert and "タブが既にあります" in alert
    env.runner.run_once()
    assert len(env.cw.posts(APPROVAL_ROOM)) == 1   # 同じ日に何度も知らせない


def test_a_total_that_differs_from_the_sheet_stops_the_notice(env):
    env.sheets.wrong_total = 99999
    env.clock.now = at(12, 1, 9, 0)
    env.runner.run_once()
    assert "2026-11" not in state_of(env)["notices"] and env.cw.uploads == []
    assert "合いません" in env.cw.posts(APPROVAL_ROOM)[0]
    env.sheets.wrong_total = None
    env.clock.now = at(12, 1, 9, 5)
    env.runner.run_once()   # 自分で作ったタブは作り直せる
    assert state_of(env)["notices"]["2026-11"]["status"] == AWAITING


# --- 3. 承認と 4. 交付 ---


def test_one_approval_delivers_the_approved_pdf_as_it_is(env):
    request = december(env)
    env.cw.reply(APPROVAL_ROOM, SAKATA, request, "承認します")
    env.clock.now = at(12, 1, 10, 0)
    env.runner.run_once()
    notice = state_of(env)["notices"]["2026-11"]
    assert notice["status"] == ISSUED and notice["approved_by"] == SAKATA
    shown, delivered = env.cw.uploads
    assert delivered[0] == PAYEE_ROOM and delivered[2] == shown[2]            # 見せたPDFと同じバイト列
    assert hashlib.sha256(delivered[2]).hexdigest() == notice["pdf_sha256"]
    text = delivered[3]
    assert text.startswith(f"[To:{MIYAZAKI}]宮﨑さん\n2026年11月分の支払通知書をお送りします。")
    assert "お支払金額の合計は101,240円（業務委託費100,000円、立替経費など1件 1,240円）、お支払予定日は12月15日（火）です。" in text
    assert "受領後7日以内にこのチャットでお知らせください" in text
    done = env.cw.posts(APPROVAL_ROOM)[-1]
    assert "承認ありがとうございます。" in done and "交付しました" in done and "支払予定日は12月15日（火）です。振込の手配をお願いします。" in done
    env.clock.now = at(12, 2, 9, 0)
    env.runner.run_once()
    assert len(env.cw.uploads) == 2   # 交付は一度だけ。催促もしない


def test_nothing_is_delivered_without_an_approval(env):
    request = december(env)
    env.cw.reply(APPROVAL_ROOM, SHINODA, request, "承認")                      # 承認者ではない
    env.cw.say(APPROVAL_ROOM, SAKATA, "承認")                                  # 承認依頼への返信ではない
    env.cw.reply(APPROVAL_ROOM, ADACHI, request, "承認は少し待ってください")
    env.model.replies.append({"intent": "other", "ops": [], "question": ""})
    env.clock.now = at(12, 1, 10, 0)
    env.runner.run_once()
    assert state_of(env)["notices"]["2026-11"]["status"] == AWAITING and len(env.cw.uploads) == 1
    assert "承認か直しのご指示かを読み取れませんでした" in env.cw.posts(APPROVAL_ROOM)[-1]


def test_an_approval_on_the_pdf_right_after_it_was_posted_is_ours(env):
    # 実例（2026-11-20 21:32 のリハーサル）: 添付の1分後に、PDFの投稿へ「承認」と返信された。添付の投稿IDを
    # まだ控えていなかったので webhook 側が見分けられず、マニュアル更新の処理が「どの件のお返事か分からなかった」と返した
    december(env)
    pdf_post = next(m["message_id"] for m in env.cw.rooms[APPROVAL_ROOM] if "[download:" in m["body"])
    path = env.tmp / "state" / "paynotice.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    notice = state["contracts"]["miyazaki"]["notices"]["2026-11"]
    notice["request_ids"] = [i for i in notice["request_ids"] if i != pdf_post]   # 添付の直後（まだ結び付けていない）
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    assert env.runner.owns_reply(APPROVAL_ROOM, pdf_post)        # ファイルIDで見分ける
    other = env.cw.attach(APPROVAL_ROOM, SHINODA, ["別の資料.pdf"])
    assert not env.runner.owns_reply(APPROVAL_ROOM, other)       # 人が上げた別のファイルは、こちらのものではない
    env.cw.reply(APPROVAL_ROOM, ADACHI, pdf_post, "承認")
    env.runner.run_once()                                         # webhook 側が、その場で回す
    assert state_of(env)["notices"]["2026-11"]["status"] == ISSUED


def test_the_pdf_of_an_old_version_is_treated_as_the_old_request(env):
    request = december(env)
    old_pdf = next(m["message_id"] for m in env.cw.rooms[APPROVAL_ROOM] if "[download:" in m["body"])
    env.cw.reply(APPROVAL_ROOM, ADACHI, request, "②を外して")
    env.model.replies.append({"intent": "revise", "question": "", "ops": [op("exclude_receipt", number=2)]})
    env.clock.now = at(12, 1, 10, 0)
    env.runner.run_once()
    assert env.runner.owns_reply(APPROVAL_ROOM, old_pdf)
    env.cw.reply(APPROVAL_ROOM, SAKATA, old_pdf, "承認")          # 古い版のPDFへの承認では交付しない
    env.runner.run_once()
    notice = state_of(env)["notices"]["2026-11"]
    assert notice["status"] == AWAITING and "最新は第2版です" in env.cw.posts(APPROVAL_ROOM)[-1]
    assert all(u[0] == APPROVAL_ROOM for u in env.cw.uploads)


def test_a_reply_to_the_attached_pdf_counts_too(env):
    # 添付の投稿IDは作った時点では分からない（APIはファイルIDしか返さない）。次に一覧を見たときに結び付ける
    december(env)
    pdf_post = next(m["message_id"] for m in env.cw.rooms[APPROVAL_ROOM] if "[download:" in m["body"])
    env.cw.reply(APPROVAL_ROOM, IWANAGA, pdf_post, "承認")
    env.runner.run_once()
    notice = state_of(env)["notices"]["2026-11"]
    assert notice["status"] == ISSUED and pdf_post in notice["request_ids"]


def test_a_correction_rebuilds_the_notice_and_asks_again(env):
    request = december(env)
    env.cw.reply(APPROVAL_ROOM, ADACHI, request, "業務委託費を50,000円にして、②は外してください")
    env.model.replies.append({"intent": "revise", "question": "",
                              "ops": [op("set_fee_amount", amount=50000), op("exclude_receipt", number=2)]})
    env.clock.now = at(12, 1, 10, 0)
    env.runner.run_once()
    notice = state_of(env)["notices"]["2026-11"]
    assert notice["version"] == 2 and notice["status"] == AWAITING and notice["total"] == 50000
    assert env.sheets.deleted == ["2026年11月(源泉なし)"] and env.sheets.tabs["2026年11月(源泉なし)"]["cells"]["N20"] == 50000
    posts = env.cw.posts(APPROVAL_ROOM)
    assert "支払通知書（第2版）" in posts[-2] and "① 11/30 ヤマトライジング業務委託費 50,000円" in posts[-2]
    assert "載せていない証憑（番号はそのままです）:" in posts[-2] and "（外す指示あり）" in posts[-2]
    assert "直しました（業務委託費を50,000円に、②を外す）。第2版を投稿しています。" in posts[-1]
    assert len(env.cw.uploads) == 2 and all(u[0] == APPROVAL_ROOM for u in env.cw.uploads)
    # 行の一覧をモデルへ渡している
    assert "2（②）: 証憑「立替：交通費（近畿日本鉄道）」" in env.model.calls[-1][1]


def two_receipts(env):
    """近鉄（11/12）と駐車料金（11/15）の2件で、12/1 の朝に第1版を作らせる。→ 承認依頼の投稿ID"""
    submit(env)
    submit(env, names=("IMG_0002.jpg",), day="2026-11-15", payee="タイムズ24", amount=1200, purpose="駐車料金")
    env.runner.run_once()
    env.clock.now = at(12, 1, 9, 0)
    env.runner.run_once()
    return state_of(env)["notices"]["2026-11"]["request_ids"][0]


def test_numbers_stay_with_their_lines_across_versions(env):
    # 実例（2026-11-20 のリハーサル）: 第1版「①業務委託費 ②近鉄 ③駐車料金」から②を外すと、第2版で
    # 駐車料金が②、外した近鉄が③に振り直された。番号は一度振ったら変えない
    request = two_receipts(env)
    first = env.cw.posts(APPROVAL_ROOM)[0]
    assert "② 11/12 立替：交通費（近畿日本鉄道） 1,240円" in first and "③ 11/15 立替：駐車料金（タイムズ24） 1,200円" in first
    env.cw.reply(APPROVAL_ROOM, ADACHI, request, "②を外して")
    env.model.replies.append({"intent": "revise", "question": "", "ops": [op("exclude_receipt", number=2)]})
    env.clock.now = at(12, 1, 10, 0)
    env.runner.run_once()
    second = env.cw.posts(APPROVAL_ROOM)[-2]
    assert "支払通知書（第2版）" in second
    assert "① 11/30 ヤマトライジング業務委託費 100,000円" in second
    assert "③ 11/15 立替：駐車料金（タイムズ24） 1,200円" in second          # ③のまま
    # 外した行も、載っていたときと同じ書き方（先頭にファイル名を足さない。指摘 2026-11-20）
    assert "載せていない証憑（番号はそのままです）:\n② 11/12 立替：交通費（近畿日本鉄道） 1,240円（外す指示あり）　証憑: https://" in second
    assert "② 11/15" not in second and "IMG_0001.jpg" not in second
    # 次の指示も、最初に見た番号のまま通じる
    env.cw.reply(APPROVAL_ROOM, ADACHI, last_bot_reply(env), "③の金額を1,300円に。②はやはり載せて")
    env.model.replies.append({"intent": "revise", "question": "", "ops": [
        op("set_receipt_amount", number=3, amount=1300), op("include_receipt", number=2)]})
    env.runner.run_once()
    third = env.cw.posts(APPROVAL_ROOM)[-2]
    assert "② 11/12 立替：交通費（近畿日本鉄道） 1,240円" in third and "③ 11/15 立替：駐車料金（タイムズ24） 1,300円" in third
    assert state_of(env)["notices"]["2026-11"]["total"] == 102540
    # モデルへ渡す一覧も同じ番号
    assert "2（②）: 証憑「立替：交通費（近畿日本鉄道）」" in env.model.calls[-1][1]
    assert "3（③）: 証憑「立替：駐車料金（タイムズ24）」" in env.model.calls[-1][1]


def test_an_added_line_gets_the_next_number_and_keeps_it(env):
    request = two_receipts(env)
    env.cw.reply(APPROVAL_ROOM, ADACHI, request, "10月分の交通費精算3,000円を足して、②は外して")
    env.model.replies.append({"intent": "revise", "question": "", "ops": [
        op("add_line", amount=3000, day="2026-11-30", label="交通費精算（10月分）", exempt=True),
        op("exclude_receipt", number=2)]})
    env.clock.now = at(12, 1, 10, 0)
    env.runner.run_once()
    second = env.cw.posts(APPROVAL_ROOM)[-2]
    assert "④ 11/30 交通費精算（10月分） 3,000円 ※課税対象外" in second and "③ 11/15 立替：駐車料金" in second
    env.cw.reply(APPROVAL_ROOM, ADACHI, last_bot_reply(env), "④を消して")
    env.model.replies.append({"intent": "revise", "question": "", "ops": [op("remove_line", number=4)]})
    env.runner.run_once()
    notice = state_of(env)["notices"]["2026-11"]
    assert notice["version"] == 3 and notice["total"] == 101200 and "④" not in env.cw.posts(APPROVAL_ROOM)[-2]


def last_bot_reply(env, room=APPROVAL_ROOM):
    return next(m["message_id"] for m in reversed(env.cw.rooms[room]) if m["account"]["account_id"] == ME
                and m["body"].startswith("[rp aid="))


def test_a_second_correction_as_a_reply_to_the_fixed_it_message_is_read(env):
    # 実例（2026-11-20 19:35 のリハーサル）: 「②を外して」で第2版ができた後、「直しました」の返事への返信で
    # 「①を15万円に変更して」と頼まれた。承認依頼そのものへの返信でなかったため読めず、作り直さなかった
    request = december(env)
    env.cw.reply(APPROVAL_ROOM, ADACHI, request, "②を外して")
    env.model.replies.append({"intent": "revise", "question": "", "ops": [op("exclude_receipt", number=2)]})
    env.clock.now = at(12, 1, 10, 0)
    env.runner.run_once()
    fixed = last_bot_reply(env)
    assert "直しました（②を外す）" in env.cw.posts(APPROVAL_ROOM)[-1]
    env.cw.reply(APPROVAL_ROOM, ADACHI, fixed, "①を15万円に変更して")
    env.model.replies.append({"intent": "revise", "question": "", "ops": [op("set_fee_amount", amount=150000)]})
    env.runner.run_once()
    notice = state_of(env)["notices"]["2026-11"]
    assert notice["version"] == 3 and notice["total"] == 150000
    assert "直しました（業務委託費を150,000円に）。第3版を投稿しています。" in env.cw.posts(APPROVAL_ROOM)[-1]
    # 続けて、その返事への返信で承認できる
    env.cw.reply(APPROVAL_ROOM, ADACHI, last_bot_reply(env), "承認")
    env.runner.run_once()
    assert state_of(env)["notices"]["2026-11"]["status"] == ISSUED
    assert env.cw.uploads[-1][0] == PAYEE_ROOM and env.cw.uploads[-1][2] == env.cw.uploads[-2][2]


def test_the_fixed_it_message_of_an_old_version_does_not_approve_the_new_one(env):
    request = december(env)
    env.cw.reply(APPROVAL_ROOM, ADACHI, request, "②を外して")
    env.model.replies.append({"intent": "revise", "question": "", "ops": [op("exclude_receipt", number=2)]})
    env.clock.now = at(12, 1, 10, 0)
    env.runner.run_once()
    second = last_bot_reply(env)   # 第2版の「直しました」
    env.cw.reply(APPROVAL_ROOM, ADACHI, second, "業務委託費を5万円に")
    env.model.replies.append({"intent": "revise", "question": "", "ops": [op("set_fee_amount", amount=50000)]})
    env.runner.run_once()
    env.cw.reply(APPROVAL_ROOM, SAKATA, second, "承認")   # 第3版ができた後に、第2版の返事へ承認
    env.runner.run_once()
    notice = state_of(env)["notices"]["2026-11"]
    assert notice["status"] == AWAITING and notice["version"] == 3
    assert "最新は第3版です" in env.cw.posts(APPROVAL_ROOM)[-1]


def test_replies_to_our_posts_are_ours_not_questions(env):
    request = december(env)
    runner = env.runner
    assert runner.owns_reply(APPROVAL_ROOM, request)
    assert not runner.owns_reply(APPROVAL_ROOM, "999999") and not runner.owns_reply(APPROVAL_ROOM, "")
    assert not runner.owns_reply(PAYEE_ROOM, request)          # 別のルームの同じ番号は関係ない
    env.cw.reply(APPROVAL_ROOM, ADACHI, request, "②を外して")
    env.model.replies.append({"intent": "revise", "question": "", "ops": [op("exclude_receipt", number=2)]})
    env.clock.now = at(12, 1, 10, 0)
    runner.run_once()
    assert runner.owns_reply(APPROVAL_ROOM, request)           # 古い版の承認依頼
    assert runner.owns_reply(APPROVAL_ROOM, last_bot_reply(env))   # 「直しました」の返事
    pdf_post = next(m["message_id"] for m in reversed(env.cw.rooms[APPROVAL_ROOM]) if "[download:" in m["body"])
    runner.run_once()
    assert runner.owns_reply(APPROVAL_ROOM, pdf_post)          # 添付したPDFの投稿


def test_a_reply_to_the_delivery_note_is_pointed_to_the_request(env):
    # 交付の知らせなど、どの版の話か決められない投稿への返信は、黙らずに返信先を案内する
    request = december(env)
    env.cw.reply(APPROVAL_ROOM, ADACHI, request, "⑨を外して")
    env.model.replies.append({"intent": "revise", "question": "", "ops": [op("exclude_receipt", number=9)]})
    env.clock.now = at(12, 1, 10, 0)
    env.runner.run_once()
    asked = last_bot_reply(env)   # 「ご指示を反映できませんでした」— これは同じ版への返事として読む
    env.cw.reply(APPROVAL_ROOM, ADACHI, asked, "承認")
    env.runner.run_once()
    assert state_of(env)["notices"]["2026-11"]["status"] == ISSUED
    delivered = last_bot_reply(env)   # 「承認ありがとうございます。…交付しました」
    env.cw.reply(APPROVAL_ROOM, SAKATA, delivered, "ありがとうございます")
    env.runner.run_once()
    assert env.runner.owns_reply(APPROVAL_ROOM, delivered)
    assert "交付しました" in env.cw.posts(APPROVAL_ROOM)[-1]   # 承認待ちが無ければ、何も足さない


def test_the_file_name_follows_the_issue_date(env):
    request = december(env)
    env.cw.reply(APPROVAL_ROOM, ADACHI, request, "発行日を12月3日にしてください")
    env.model.replies.append({"intent": "revise", "question": "", "ops": [op("set_issue_date", day="2026-12-03")]})
    env.clock.now = at(12, 1, 10, 0)
    env.runner.run_once()
    notice = state_of(env)["notices"]["2026-11"]
    assert notice["issue_date"] == "2026-12-03" and env.sheets.tabs["2026年11月(源泉なし)"]["cells"]["Q4"] == date(2026, 12, 3)
    assert env.cw.uploads[-1][1] == "12.3-宮﨑大樹様-ヤマトライジング業務委託費支払通知書2026年11月分.pdf"
    env.cw.reply(APPROVAL_ROOM, ADACHI, notice["request_ids"][0], "承認")
    env.runner.run_once()
    assert env.cw.uploads[-1][0] == PAYEE_ROOM and env.cw.uploads[-1][1].startswith("12.3-宮﨑大樹様-")


def test_an_approval_of_the_old_version_does_not_deliver_it(env):
    # 直しの指示と、古い版への「承認」が同じ巡回の間に届いたとき、直す前の版を渡さない
    request = december(env)
    env.cw.reply(APPROVAL_ROOM, ADACHI, request, "②は外してください")
    env.cw.reply(APPROVAL_ROOM, SAKATA, request, "承認")
    env.model.replies.append({"intent": "revise", "question": "", "ops": [op("exclude_receipt", number=2)]})
    env.clock.now = at(12, 1, 10, 0)
    env.runner.run_once()
    notice = state_of(env)["notices"]["2026-11"]
    assert notice["status"] == AWAITING and notice["version"] == 2
    assert all(u[0] == APPROVAL_ROOM for u in env.cw.uploads)   # 委託先には出していない
    assert "最新は第2版です" in env.cw.posts(APPROVAL_ROOM)[-1]
    env.cw.reply(APPROVAL_ROOM, SAKATA, notice["request_ids"][0], "承認")
    env.runner.run_once()
    assert state_of(env)["notices"]["2026-11"]["status"] == ISSUED
    assert env.cw.uploads[-1][0] == PAYEE_ROOM and env.cw.uploads[-1][2] == env.cw.uploads[-2][2]


def test_an_unreadable_receipt_is_listed_once_a_person_gives_the_amount(env):
    submit(env, amount=0, day="")
    env.runner.run_once()
    env.clock.now = at(12, 1, 9, 0)
    env.runner.run_once()
    notice = state_of(env)["notices"]["2026-11"]
    request = env.cw.posts(APPROVAL_ROOM)[0]
    assert notice["total"] == 100000 and "載せていない証憑（番号はそのままです）:" in request
    assert "② 日付不明 立替：交通費（近畿日本鉄道） 金額不明（金額を読み取れませんでした、日付を読み取れませんでした）　証憑: https://" in request
    assert request.count("（IMG_0001.jpg）") == 1   # 読めなかった証憑は、どの画像かをリンクの後ろに添える
    env.cw.reply(APPROVAL_ROOM, IWANAGA, notice["request_ids"][0], "②は11/12の交通費で1,240円です")
    env.model.replies.append({"intent": "revise", "question": "", "ops": [
        op("set_receipt_amount", number=2, amount=1240), op("set_receipt_date", number=2, day="2026-11-12")]})
    env.clock.now = at(12, 1, 10, 0)
    env.runner.run_once()
    notice = state_of(env)["notices"]["2026-11"]
    assert notice["version"] == 2 and notice["total"] == 101240 and state_of(env)["receipts"][0]["status"] == OK


def test_an_instruction_that_does_not_fit_changes_nothing(env):
    request = december(env)
    env.cw.reply(APPROVAL_ROOM, ADACHI, request, "⑨を外して")
    env.model.replies.append({"intent": "revise", "question": "", "ops": [op("exclude_receipt", number=9)]})
    env.clock.now = at(12, 1, 10, 0)
    env.runner.run_once()
    notice = state_of(env)["notices"]["2026-11"]
    assert notice["version"] == 1 and "ご指示を反映できませんでした（⑨に当たる行が見当たりません）" in env.cw.posts(APPROVAL_ROOM)[-1]


def test_a_line_without_a_receipt_can_be_added_and_removed(env):
    runner = env.runner
    cstate = {"receipts": []}
    notice = {"overrides": {}, "display": [{"kind": "fee", "receipt_id": None, "label": "業務委託費"}]}
    done = runner._apply(cstate, notice, [op("add_line", amount=3000, day="2026-11-30", label="交通費精算（10月分）", exempt=True)])
    assert done == ["「交通費精算（10月分）」3,000円の行を追加"]
    lines = build_lines(runner.contract(), "2026-11", [], notice["overrides"])
    assert (lines[1]["kind"], lines[1]["price"], lines[1]["exempt"], lines[1]["unit"]) == ("added", 3000, True, "式")
    with pytest.raises(NoticeError):
        runner._apply(cstate, notice, [op("set_fee_amount", amount=0)])
    with pytest.raises(NoticeError):
        runner._apply(cstate, notice, [op("set_issue_date", day="12月2日")])


def test_a_failed_delivery_is_tried_again(env):
    request = december(env)
    env.cw.reply(APPROVAL_ROOM, SAKATA, request, "承認")
    env.cw.fail_upload = 2   # 承認の直後と、同じ巡回の中のやり直しの両方が失敗する
    env.clock.now = at(12, 1, 10, 0)
    env.runner.run_once()
    notice = state_of(env)["notices"]["2026-11"]
    assert notice["status"] == AWAITING and notice["approved_by"] == SAKATA
    assert "交付できませんでした" in env.cw.posts(APPROVAL_ROOM)[-1]
    env.clock.now = at(12, 2, 9, 0)   # 承認済みなので、翌朝も催促はしない
    env.runner.run_once()
    assert "【承認待ち】" not in env.cw.posts(APPROVAL_ROOM)[-1]
    assert state_of(env)["notices"]["2026-11"]["status"] == ISSUED and env.cw.uploads[-1][0] == PAYEE_ROOM


def test_a_changed_pdf_is_not_delivered(env):
    request = december(env)
    Path(state_of(env)["notices"]["2026-11"]["pdf"]).write_bytes(b"%PDF-tampered")
    env.cw.reply(APPROVAL_ROOM, SAKATA, request, "承認")
    env.runner.run_once()
    assert state_of(env)["notices"]["2026-11"]["status"] == AWAITING
    assert all(u[0] == APPROVAL_ROOM for u in env.cw.uploads)


# --- 5. 振込の依頼 ---


def with_payment_request(env):
    settings = env.config.data["payment_notice"]
    settings["payment_request"] = settings["saved_payment_request"]


def test_on_approval_the_payment_is_requested_with_the_pdf_and_the_receipts(env):
    # 指示（2026-11-20）: 承認後、宮﨑さんへの交付と同時に「支払依頼・入金確認チャット」で坂田さん・伊藤さん・
    # 岩永さんへ振込を依頼し、支払通知書のPDFと、承認済みの証憑（レシート）のデータを添える
    with_payment_request(env)
    request = two_receipts(env)
    env.cw.reply(APPROVAL_ROOM, SAKATA, request, "承認")
    env.clock.now = at(12, 1, 10, 14)
    env.runner.run_once()
    notice = state_of(env)["notices"]["2026-11"]
    assert notice["status"] == ISSUED and notice["payment_request"]["status"] == "done"
    shown, delivered, pay_pdf, first, second = env.cw.uploads
    assert delivered[0] == PAYEE_ROOM                                  # 先に委託先へ交付
    assert pay_pdf[0] == PAYMENT_ROOM and pay_pdf[2] == delivered[2]   # 同じPDFを振込の依頼にも添える
    assert pay_pdf[1] == "12.1-宮﨑大樹様-ヤマトライジング業務委託費支払通知書2026年11月分.pdf"
    text = pay_pdf[3]
    assert text.startswith(f"[To:{SAKATA}]坂田さん [To:{ITO}]伊藤さん [To:{IWANAGA}]岩永さん\n"
                           "【振込のお願い】宮﨑 大樹様 2026年11月分（ヤマトライジング業務委託費）")
    assert "支払通知書を交付しました（承認: 坂田さん・12/1 10:14）。次のとおり、振込の手配をお願いします。" in text
    assert "支払予定日: 12月15日（火）" in text and "支払金額: 102,440円" in text
    assert "① 11/30 ヤマトライジング業務委託費 100,000円" in text
    assert "② 11/12 立替：交通費（近畿日本鉄道） 1,240円" in text and "③ 11/15 立替：駐車料金（タイムズ24） 1,200円" in text
    assert "振込先は、添付の支払通知書の備考欄をご確認ください。" in text
    assert "添付: 支払通知書のPDF（この投稿）と、承認済みの証憑2点（続けて投稿します）" in text
    assert "口座" not in text                                          # 振込先そのものは文に書かない
    # 証憑は、受付のときに読んだファイルそのもの
    assert (first[0], first[1], first[2], first[3]) == (PAYMENT_ROOM, "IMG_0001.jpg", b"\xff\xd8image-bytes", "承認済みの証憑（②）です。")
    assert (second[0], second[1], second[3]) == (PAYMENT_ROOM, "IMG_0002.jpg", "承認済みの証憑（③）です。")
    done = env.cw.posts(APPROVAL_ROOM)[-1]
    assert "交付しました" in done and "あわせて「支払依頼・入金確認チャット」へ、振込の依頼を出しました（支払予定日は12月15日（火））。" in done
    env.clock.now = at(12, 2, 9, 0)
    env.runner.run_once()
    assert len(env.cw.uploads) == 5                                    # 一度だけ


def test_the_request_and_the_receipts_appear_in_the_order_they_were_sent(env):
    # 実例（2026-11-20 21:51 のリハーサル）: Chatworkは添付を受け取ってから投稿として並べるまでに間があり、
    # その長さがファイルごとに違う。続けて送ったら「証憑② → 依頼文とPDF → 証憑③」の順に並んだ。
    # 1つ前の添付が投稿として並んだのを確かめてから、次を送る
    with_payment_request(env)
    request = two_receipts(env)
    cw = env.cw
    slow = []   # [残りの確認回数, 並べる投稿]

    def upload(room_id, filename, data, message=""):
        cw._next += 1
        file_id = cw._next
        cw.uploads.append((room_id, filename, data, message))
        delay = 3 if filename.endswith(".pdf") and room_id == PAYMENT_ROOM else 0   # PDFだけ並ぶのが遅い
        slow.append([delay, room_id, f"{message}[download:{file_id}]{filename}[/download]"])
        release()
        return str(file_id)

    def release():
        for entry in list(slow):
            if entry[0] <= 0:
                cw._add(entry[1], ME, entry[2])
                slow.remove(entry)

    def recent(room_id, limit=100):
        for entry in slow:
            entry[0] -= 1
        release()
        return list(cw.rooms.get(room_id, []))[-limit:]

    cw.upload_file, cw.get_recent_messages = upload, recent
    cw.reply(APPROVAL_ROOM, SAKATA, request, "承認")
    env.clock.now = at(12, 1, 10, 14)
    env.runner.run_once()
    order = [m["body"].split("[download:")[0][:14] for m in cw.rooms[PAYMENT_ROOM]]
    assert order == ["[To:8681926]坂田", "承認済みの証憑（②）です。", "承認済みの証憑（③）です。"]   # 依頼文とPDF → ② → ③
    assert len(env.naps) >= 2                                              # 並ぶのを待った
    assert state_of(env)["notices"]["2026-11"]["payment_request"]["status"] == "done"


def test_a_post_that_never_shows_up_does_not_block_the_rest(env):
    with_payment_request(env)
    env.config.data["payment_notice"]["payment_request"]["order_wait_seconds"] = 3
    request = december(env)
    cw = env.cw
    real = cw.upload_file

    def upload(room_id, filename, data, message=""):
        if room_id != PAYMENT_ROOM:
            return real(room_id, filename, data, message)
        cw.uploads.append((room_id, filename, data, message))   # 一覧には出てこない
        cw._next += 1
        return str(cw._next)

    cw.upload_file = upload
    cw.reply(APPROVAL_ROOM, SAKATA, request, "承認")
    env.runner.run_once()
    assert state_of(env)["notices"]["2026-11"]["payment_request"]["status"] == "done"
    assert [u[1] for u in cw.uploads if u[0] == PAYMENT_ROOM][1:] == ["IMG_0001.jpg"] and len(env.naps) == 6   # 3秒ずつ待って先へ


def test_only_the_receipts_on_the_approved_version_are_sent(env):
    with_payment_request(env)
    request = two_receipts(env)
    env.cw.reply(APPROVAL_ROOM, ADACHI, request, "②を外して")
    env.model.replies.append({"intent": "revise", "question": "", "ops": [op("exclude_receipt", number=2)]})
    env.clock.now = at(12, 1, 10, 0)
    env.runner.run_once()
    env.cw.reply(APPROVAL_ROOM, ADACHI, state_of(env)["notices"]["2026-11"]["request_ids"][0], "承認")
    env.runner.run_once()
    to_payment = [u for u in env.cw.uploads if u[0] == PAYMENT_ROOM]
    assert [u[1] for u in to_payment] == ["12.1-宮﨑大樹様-ヤマトライジング業務委託費支払通知書2026年11月分.pdf", "IMG_0002.jpg"]
    assert "承認済みの証憑1点" in to_payment[0][3] and "② 11/12" not in to_payment[0][3]
    assert to_payment[1][3] == "承認済みの証憑（③）です。"            # 外した②の領収書は送らない


def test_one_image_with_two_receipts_is_sent_once(env):
    with_payment_request(env)
    env.model.receipts["two.jpg"] = {"receipts": [receipt(), receipt(day="2026-11-13", amount=880)], "note": ""}
    submit(env, names=("two.jpg",))
    env.runner.run_once()
    env.clock.now = at(12, 1, 9, 0)
    env.runner.run_once()
    env.cw.reply(APPROVAL_ROOM, IWANAGA, state_of(env)["notices"]["2026-11"]["request_ids"][0], "承認")
    env.runner.run_once()
    to_payment = [u for u in env.cw.uploads if u[0] == PAYMENT_ROOM]
    assert len(to_payment) == 2 and to_payment[1][3] == "承認済みの証憑（②③）です。"


def test_a_month_without_receipts_asks_for_the_fee_only(env):
    with_payment_request(env)
    env.clock.now = at(12, 1, 9, 0)
    env.runner.run_once()
    env.cw.reply(APPROVAL_ROOM, SAKATA, state_of(env)["notices"]["2026-11"]["request_ids"][0], "承認")
    env.runner.run_once()
    to_payment = [u for u in env.cw.uploads if u[0] == PAYMENT_ROOM]
    assert len(to_payment) == 1 and to_payment[0][3].endswith("添付: 支払通知書のPDF")
    assert "支払金額: 100,000円" in to_payment[0][3]


def test_no_payment_is_requested_before_the_notice_is_delivered(env):
    with_payment_request(env)
    request = two_receipts(env)
    env.runner.run_once()
    assert [u for u in env.cw.uploads if u[0] == PAYMENT_ROOM] == []   # 承認前
    env.cw.reply(APPROVAL_ROOM, SAKATA, request, "承認")
    env.cw.fail_upload = 2                                             # 交付に失敗している間
    env.clock.now = at(12, 1, 10, 0)
    env.runner.run_once()
    assert state_of(env)["notices"]["2026-11"]["status"] == AWAITING
    assert [u for u in env.cw.uploads if u[0] == PAYMENT_ROOM] == []
    env.runner.run_once()
    assert state_of(env)["notices"]["2026-11"]["payment_request"]["status"] == "done"


def test_a_payment_request_that_broke_off_continues_where_it_stopped(env):
    with_payment_request(env)
    request = two_receipts(env)
    env.cw.reply(APPROVAL_ROOM, SAKATA, request, "承認")
    env.clock.now = at(12, 1, 10, 0)
    real = env.cw.upload_file
    seen = []

    def flaky(room_id, filename, data, message=""):
        seen.append((room_id, filename))
        if room_id == PAYMENT_ROOM and filename == "IMG_0002.jpg" and seen.count((room_id, filename)) == 1:
            raise RuntimeError("upload failed")
        return real(room_id, filename, data, message)

    env.cw.upload_file = flaky
    env.runner.run_once()
    notice = state_of(env)["notices"]["2026-11"]
    assert notice["status"] == ISSUED and notice["payment_request"]["status"] == "done"   # 同じ巡回の中でやり直して出しきる
    names = [u[1] for u in env.cw.uploads if u[0] == PAYMENT_ROOM]
    assert names.count("IMG_0001.jpg") == 1 and names.count("IMG_0002.jpg") == 1          # 送れた分は二度送らない
    assert sum(1 for u in env.cw.uploads if u[0] == PAYMENT_ROOM and u[1].endswith(".pdf")) == 1
    assert any("振込の依頼を出しきれませんでした" in p for p in env.cw.posts(APPROVAL_ROOM))


def test_a_receipt_file_that_is_gone_is_pointed_to_instead(env):
    with_payment_request(env)
    request = december(env)
    stored = Path(state_of(env)["receipts"][0]["stored"])
    assert stored.read_bytes() == b"\xff\xd8image-bytes"
    stored.unlink()
    env.runner._download = lambda url: (404, b"")                      # 元の投稿からも取れない
    env.cw.reply(APPROVAL_ROOM, SAKATA, request, "承認")
    env.runner.run_once()
    assert state_of(env)["notices"]["2026-11"]["payment_request"]["status"] == "done"
    note = env.cw.posts(PAYMENT_ROOM)[-1]
    assert "次の証憑は添付できませんでした" in note and "② IMG_0001.jpg（ファイルを取得できませんでした）" in note
    assert f"#!rid{PAYEE_ROOM}-" in note


def test_a_rehearsal_sends_the_payment_request_to_the_admin_room(env, tmp_path):
    with_payment_request(env)
    env.config.data["payment_notice"]["live"] = False
    image = tmp_path / "sample.jpg"
    image.write_bytes(b"\xff\xd8sample")
    env.model.receipts["sample.jpg"] = {"receipts": [receipt(day="2026-10-20")], "note": ""}
    env.runner.rehearse("2026-10", [image])
    env.cw.reply(ADMIN_ROOM, ADACHI, state_of(env, "paynotice-rehearsal.json")["notices"]["2026-10"]["request_ids"][0], "承認")
    env.runner.run_once()
    assert [u[0] for u in env.cw.uploads] == [ADMIN_ROOM] * 4          # 確認用・交付・振込依頼のPDF・証憑
    assert env.cw.uploads[2][3].startswith(f"[To:{ADACHI}]\n【リハーサル】【振込のお願い】宮﨑 大樹様 2026年10月分")
    assert "（承認: 足立さん・" in env.cw.uploads[2][3]   # リハーサルでも承認者を名前で書く
    assert (env.cw.uploads[3][1], env.cw.uploads[3][2], env.cw.uploads[3][3]) == ("sample.jpg", b"\xff\xd8sample", "【リハーサル】承認済みの証憑（②）です。")
    assert env.cw.posts(PAYMENT_ROOM) == [] and env.cw.posts(PAYEE_ROOM) == []   # 本番のルームには出さない
    assert "あわせてこのルーム（リハーサルのため）へ、振込の依頼を出しました" in env.cw.posts(ADMIN_ROOM)[-1]


def test_the_payment_request_alone_can_be_tried_in_the_rehearsal(env, tmp_path):
    # 指示 2026-11-23: 振込の依頼だけをもう一度テストで送る。承認待ちの控えは変えない
    with_payment_request(env)
    env.config.data["payment_notice"]["live"] = False
    image = tmp_path / "sample.jpg"
    image.write_bytes(b"\xff\xd8sample")
    env.model.receipts["sample.jpg"] = {"receipts": [receipt(day="2026-10-20")], "note": ""}
    env.runner.rehearse("2026-10", [image])
    before = state_of(env, "paynotice-rehearsal.json")
    assert env.runner.rehearse_payment("2026-10") == 2
    assert [(u[0], u[1][-4:]) for u in env.cw.uploads[1:]] == [(ADMIN_ROOM, ".pdf"), (ADMIN_ROOM, ".jpg")]   # PDF → 証憑
    text = env.cw.uploads[1][3]
    assert text.startswith(f"[To:{ADACHI}]\n【リハーサル】【試し送信】【振込のお願い】宮﨑 大樹様 2026年10月分")
    assert "試し送信です（承認・交付はまだです）" in text and "交付しました" not in text
    assert env.cw.uploads[2][3] == "【リハーサル】【試し送信】承認済みの証憑（②）です。"
    assert state_of(env, "paynotice-rehearsal.json") == before                      # 控えは変えない（承認待ちのまま）
    assert env.cw.posts(PAYMENT_ROOM) == [] and env.cw.posts(PAYEE_ROOM) == []      # 本番のルームには出さない
    with pytest.raises(NoticeError):
        env.runner.rehearse_payment("2026-09")                                      # リハーサルの無い月


# --- 承認待ちの催促 ---


def test_waiting_for_approval_is_chased_every_business_morning(env):
    december(env)
    env.clock.now = at(12, 1, 15, 0)
    env.runner.run_once()
    assert len(env.cw.posts(APPROVAL_ROOM)) == 1   # 見せた当日は催促しない
    env.clock.now = at(12, 2, 9, 0)
    env.runner.run_once()
    chase = env.cw.posts(APPROVAL_ROOM)[-1]
    assert chase.startswith(f"[To:{ADACHI}]足立さん [To:{SAKATA}]坂田さん [To:{IWANAGA}]岩永さん\n【承認待ち】宮﨑 大樹様 2026年11月分の支払通知書（第1版）")
    assert "交付期限は12月7日（月）で、今日を含めてあと4営業日です。" in chase
    env.clock.now = at(12, 2, 11, 0)
    env.runner.run_once()
    env.clock.now = at(12, 5, 9, 0)   # 土曜
    env.runner.run_once()
    assert len(env.cw.posts(APPROVAL_ROOM)) == 2
    env.clock.now = at(12, 7, 9, 0)
    env.runner.run_once()
    assert "本日が交付期限です。" in env.cw.posts(APPROVAL_ROOM)[-1]
    assert "未承認です。本日が交付期限です。" in env.cw.posts(ADMIN_ROOM)[-1]
    env.clock.now = at(12, 8, 9, 0)
    env.runner.run_once()
    assert "交付期限（12月7日（月））を過ぎています。至急ご確認ください。" in env.cw.posts(APPROVAL_ROOM)[-1]


def test_an_approval_as_a_reply_to_the_chase_counts(env):
    december(env)
    env.clock.now = at(12, 2, 9, 0)
    env.runner.run_once()
    chase = state_of(env)["notices"]["2026-11"]["request_ids"][-1]
    env.cw.reply(APPROVAL_ROOM, ADACHI, chase, "承認")
    env.runner.run_once()
    assert state_of(env)["notices"]["2026-11"]["status"] == ISSUED


# --- リハーサルと設定 ---


def test_a_rehearsal_stays_inside_the_admin_room(env, tmp_path):
    env.config.data["payment_notice"]["live"] = False
    image = tmp_path / "sample.jpg"
    image.write_bytes(b"\xff\xd8sample")
    env.model.receipts["sample.jpg"] = {"receipts": [receipt(day="2026-10-20")], "note": ""}
    notice = env.runner.rehearse("2026-10", [image])
    assert notice["total"] == 101240 and "テスト_2026年10月(源泉なし)" in env.sheets.tabs
    request = env.cw.posts(ADMIN_ROOM)[0]
    assert request.startswith(f"[To:{ADACHI}]\n【リハーサル】【承認のお願い】宮﨑 大樹様 2026年10月分")
    assert "このルーム（リハーサルのため）へPDFを交付します" in request and "証憑: sample.jpg" in request
    env.cw.reply(ADMIN_ROOM, ADACHI, state_of(env, "paynotice-rehearsal.json")["notices"]["2026-10"]["request_ids"][0], "承認")
    env.runner.run_once()
    assert state_of(env, "paynotice-rehearsal.json")["notices"]["2026-10"]["status"] == ISSUED
    assert [u[0] for u in env.cw.uploads] == [ADMIN_ROOM, ADMIN_ROOM]
    assert "【リハーサル】2026年10月分の支払通知書をお送りします。" in env.cw.uploads[1][3]
    assert env.cw.posts(PAYEE_ROOM) == [] and env.cw.posts(APPROVAL_ROOM) == []   # 本番のルームには何も出ない
    assert env.runner.cleanup_rehearsal() == ["テスト_2026年10月(源泉なし)"]
    assert list(env.sheets.tabs) == [TEMPLATE]


def test_a_rehearsal_can_use_the_real_receipts_in_the_payee_room(env):
    # 指示（2026-11-20）: 読み取りテストで足立さんが上げた実物の領収書を、支払通知書に載せて確かめる
    env.config.data["payment_notice"]["live"] = False
    env.model.receipts["stamp.JPG"] = {"receipts": [receipt(day="2026-09-25", payee="LAWSON 近鉄奈良駅前店", amount=200, purpose="収入印紙")], "note": ""}
    env.model.receipts["parking.JPG"] = {"receipts": [receipt(day="2026-11-16", payee="パーキングプレイス油阪町第1（株式会社リープリング）", amount=300, purpose="駐車料金")], "note": ""}
    first = env.cw.attach(PAYEE_ROOM, ADACHI, ["stamp.JPG"], at=at(11, 20, 21, 21))
    second = env.cw.attach(PAYEE_ROOM, ADACHI, ["parking.JPG"], at=at(11, 20, 21, 21))
    env.cw.attach(PAYEE_ROOM, IWANAGA, ["業務委託契約書.pdf"])   # 関係のない人の添付は使わない
    notice = env.runner.rehearse("2026-11", [], from_room=True)
    assert notice["total"] == 100500 and "テスト_2026年11月(源泉なし)" in env.sheets.tabs
    request = env.cw.posts(ADMIN_ROOM)[0]
    assert "【リハーサル】【承認のお願い】宮﨑 大樹様 2026年11月分 支払通知書（第1版）" in request
    assert "① 11/30 ヤマトライジング業務委託費 100,000円" in request
    assert f"② 9/25 立替：収入印紙（LAWSON 近鉄奈良駅前店） 200円 ※課税対象外　証憑: https://www.chatwork.com/#!rid{PAYEE_ROOM}-{first}" in request
    assert "【要確認】日付（2026-09-25）が契約の開始日より前です" in request
    assert f"③ 11/16 立替：駐車料金（パーキングプレイス油阪町第1） 300円 ※課税対象外　証憑: https://www.chatwork.com/#!rid{PAYEE_ROOM}-{second}" in request
    cells = env.sheets.tabs["テスト_2026年11月(源泉なし)"]["cells"]
    assert (cells["B21"], cells["N21"], cells["K21"]) == (date(2026, 9, 25), 200, "※")
    assert (cells["B22"], cells["D22"], cells["N22"]) == (date(2026, 11, 16), "立替：駐車料金（パーキングプレイス油阪町第1）", 300)
    assert env.cw.posts(PAYEE_ROOM) == [] and not (env.tmp / "state" / "paynotice.json").exists()   # 本番には何も残さない


def test_a_registration_number_carries_a_check_digit():
    for real in ("T4120001256055", "T3810281025762", "T2810826779764", "T4120001210276"):   # 実物の番号
        assert registration_verdict(real) == (real, "ok")
    assert registration_verdict("T4120001256065") == ("T4120001256065", "check")             # 1文字違い
    assert registration_verdict("T41200001256055") == ("T41200001256055", "length")
    assert registration_verdict("") == ("", "")


def test_a_miscounted_run_of_digits_can_be_repaired():
    assert repair_registration("T41200001256055") == "T4120001256055"   # 実例 2026-11-23: 0 を1つ多く読んだ
    assert repair_registration("T41200001210276") == "T4120001210276"   # 実例 2026-11-20
    assert repair_registration("T412001256055") == "T4120001256055"     # 1つ少なく読んだ
    assert repair_registration("T12345678901234") == ""                 # 続いている数字が無い
    assert repair_registration("T4120001256055") == ""                  # 13桁は触らない
    assert repair_registration("T412000001256055") == ""                # 2桁以上の違いは直さない


def test_a_registration_number_must_be_t_and_thirteen_digits():
    assert clean_registration("T2810826779764") == ("T2810826779764", True)
    assert clean_registration("Ｔ2810-8267-79764") == ("T2810826779764", True)
    assert clean_registration("T41200001210276") == ("T41200001210276", False)   # 実例: 14桁に読んだ
    assert clean_registration("") == ("", False)


def test_while_not_live_the_real_rooms_are_left_alone(env):
    env.config.data["payment_notice"]["live"] = False
    submit(env)
    env.clock.now = at(12, 1, 9, 0)
    env.runner.run_once()
    assert env.cw.sent == [] and env.cw.uploads == [] and env.model.calls == []


def test_the_settings_follow_the_contract():
    config = Config.load()
    settings = config.payment_notice
    contract = settings["contracts"][0]
    assert settings["live"] is False                      # 本番への切替は管理者の了承を得てから
    assert settings["approval_room_id"] == APPROVAL_ROOM
    assert [a["account_id"] for a in settings["approvers"]] == [ADACHI, SAKATA, IWANAGA]
    assert settings["issue_within_business_days"] == 5    # 契約6条2項
    assert contract["fee"] == {"label": "ヤマトライジング業務委託費", "amount": 100000, "quantity": 1, "unit": "式"}
    assert contract["payment_day"] == 15 and contract["term"] == {"start": "2026-10-01", "end": "2027-09-30"}
    assert contract["room_id"] == PAYEE_ROOM and PAYEE_ROOM not in config.allowed_room_ids
    payment = settings["payment_request"]
    assert payment["room_id"] == PAYMENT_ROOM and PAYMENT_ROOM not in config.allowed_room_ids
    assert [r["account_id"] for r in payment["recipients"]] == [SAKATA, ITO, IWANAGA]
