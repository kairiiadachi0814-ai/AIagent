"""営業メールの見張り。

指示（2026-11-20）: 「楽天軒　営業メール確認」ルームに流れるメールのうち、取引先の担当者からの
メールだけを、宛名に書かれた担当者（森さん・藤田さん）へ知らせる。期日を確かめ、返信が要る内容なら
当社からの返信が流れてくるまで追い、返信の中身も読んで、遅れないように知らせる。
"""

import json
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from raizuinu.config import Config
from raizuinu.mailwatch import (
    ACK_ONLY,
    ANSWERED,
    JST,
    PARTIAL,
    PARTNER,
    UNRELATED,
    MailWatcher,
    addressed_to,
    check_deadline,
    find_date_quote,
    is_done_report,
    is_forward,
    matches_sender,
    parse_mail_notice,
    relative_deadline,
    same_thread,
    thread_key,
    written_to,
)

ROOM, NOTIFIER, ME = 451262874, 10294683, 11574026
MORI, FUJITA, SHINODA = 10604615, 5631017, 9228914
THU_1000 = datetime(2026, 11, 19, 10, 0, tzinfo=JST)


def notice(sender, subject, text, received="2026-11-19 09:55", attached="なし", cut=False):
    body = (
        "[info][title]📩 新着メール[/title]\n"
        f"差出人: {sender}\n受信: {received}\n件名: {subject}\n添付: {attached}\n────\n{text}"
    )
    return body + ("\n…(以下省略)" if cut else "") + "[/info]"


class FakeChatwork:
    def __init__(self):
        self.messages, self.sent, self._next = [], [], 1000

    def add(self, account, body, at=None):
        self._next += 1
        self.messages.append({
            "message_id": str(self._next),
            "account": {"account_id": account, "name": "x"},
            "body": body,
            "send_time": int((at or THU_1000).timestamp()),
        })
        return str(self._next)

    def mail(self, *args, **kwargs):
        return self.add(NOTIFIER, notice(*args, **kwargs))

    def get_me(self):
        return ME

    def get_recent_messages(self, room_id, limit=100):
        return list(self.messages)[-limit:]

    def send_message(self, room_id, body):
        self._next += 1
        self.sent.append((room_id, body))
        self.messages.append({"message_id": str(self._next), "account": {"account_id": ME}, "body": body,
                              "send_time": 0})
        return str(self._next)


class FakeModel:
    """読み取り（仕分け）と返信の確認を、件名に含まれる語で引いて返す。"""

    def __init__(self):
        self.reads, self.checks, self.calls, self.fail = {}, {}, [], 0
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, messages, **kwargs):
        if self.fail:
            self.fail -= 1
            raise RuntimeError("overloaded")
        text = messages[0]["content"]
        checking = "返信したかを確かめる" in kwargs["system"]
        table = self.checks if checking else self.reads
        found = next((v for k, v in table.items() if k in text), None)
        if found is None:
            found = {"verdict": UNRELATED, "reply_summary": "", "remaining": ""} if checking else read()
        self.calls.append(("check" if checking else "read", text))
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=json.dumps(found, ensure_ascii=False))],
            usage=SimpleNamespace(input_tokens=900, output_tokens=150),
        )


def read(category=PARTNER, staff=(), addressee="", partner="株式会社山星屋 遠藤様", summary="パッケージの確認です。",
         needs_reply=True, ask="ロゴシールを貼るか回答してほしい", quote="", day="", cut_off=False):
    return {"category": category, "staff": list(staff), "addressee": addressee, "partner": partner,
            "summary": summary, "needs_reply": needs_reply, "ask": ask, "deadline_quote": quote,
            "deadline_date": day, "cut_off": cut_off}


def check(verdict, summary="ロゴシールは貼らないと回答しました。", remaining=""):
    return {"verdict": verdict, "reply_summary": summary, "remaining": remaining}


@pytest.fixture
def env(tmp_path):
    config = Config.load(tmp_path / "no-config.json")
    config.base_dir = tmp_path
    config.data["state_dir"] = str(tmp_path / "state")
    config.data["audit_log_dir"] = str(tmp_path / "audit")
    config.data["mail_watch"].update({
        "enabled": True, "room_id": ROOM, "notifier_account_id": NOTIFIER,
        "own_domains": ["rakutenken.co.jp", "rakutenken.jp"],
        "skip_senders": ["superdelivery.com", "info@jpts-unity.jp"],
        "staff": [
            {"account_id": MORI, "name": "森", "addresses": ["ymori@rakutenken.co.jp"]},
            {"account_id": FUJITA, "name": "藤田", "addresses": ["fujita@rakutenken.jp"]},
        ],
    })
    cw, model = FakeChatwork(), FakeModel()
    clock = SimpleNamespace(now=THU_1000)
    watcher = MailWatcher(config, cw, client=model, now=lambda: clock.now)
    cw.add(SHINODA, "テスト")
    watcher.run_once()  # 初回は既読にするだけ
    return SimpleNamespace(config=config, cw=cw, model=model, clock=clock, watcher=watcher, tmp=tmp_path)


def state_of(env):
    return json.loads((env.tmp / "state" / "mailwatch.json").read_text(encoding="utf-8"))


def audit_of(env):
    lines = []
    for path in (env.tmp / "audit").glob("audit-*.jsonl"):
        lines += [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return lines


def partner_mail(env, subject="確認：天津甘栗：パッケージの件", text="RAKUTENKEN㈱\n森さま\nロゴシールの貼付はされていますでしょうか。", **found):
    env.model.reads[subject] = read(**{"staff": ["森"], **found})
    return env.cw.mail("endo.masami@arista.co.jp", subject, text)


# --- 通知の読み取り ---


def test_a_notice_is_parsed_into_its_fields():
    mail = parse_mail_notice(notice(
        "yamaki-eri@ad.takashimaya.co.jp", "※要ご返信※休業日のご確認●11月27日(金)まで●",
        "ＲＡＫＵＴＥＮＫＥＮ　株式会社\n森様\nいつもお世話になっております。", received="2026-11-19 15:15",
        attached="休業日確認リスト.xlsx, 記入例.xlsx", cut=True,
    ))
    assert mail["sender"] == "yamaki-eri@ad.takashimaya.co.jp"
    assert mail["received"] == datetime(2026, 11, 19, 15, 15, tzinfo=JST)
    assert mail["subject"] == "※要ご返信※休業日のご確認●11月27日(金)まで●"
    assert mail["attachments"] == ["休業日確認リスト.xlsx", "記入例.xlsx"]
    assert mail["text"].endswith("いつもお世話になっております。") and mail["truncated"] is True


def test_other_posts_are_not_mail_notices():
    assert parse_mail_notice("[info][title]FAX受信通知[/title]ファイル名: 1.pdf[/info]") is None
    assert parse_mail_notice("了解です") is None
    assert parse_mail_notice(notice("a@b.co.jp", "件", "本文"))["attachments"] == []


def test_sender_rules_match_a_domain_its_subdomains_or_one_address():
    assert matches_sender("support@superdelivery.com", ["superdelivery.com"])
    assert matches_sender("x@mc.superdelivery.com", ["superdelivery.com"])
    assert not matches_sender("x@notsuperdelivery.com", ["superdelivery.com"])
    assert matches_sender("info@jpts-unity.jp", ["info@jpts-unity.jp"])
    assert not matches_sender("tanaka@jpts-unity.jp", ["info@jpts-unity.jp"])


def test_replies_share_a_thread_with_the_mail_they_answer():
    incoming = thread_key("【返信期日：11/19（木）まで】いずみ　1月2回企画連絡確認")
    assert thread_key("Re: 【返信期日：11/19（木）まで】いずみ 1月2回企画連絡確認") == incoming
    assert thread_key("RE: Re: Fwd: 福袋関連　在庫確認の件") == thread_key("福袋関連 在庫確認の件")
    # 先方が頭に語を足して返してくる形
    longer = thread_key("【ご対応依頼】⇒RE: 【初稿修正依頼 11/24(火)まで】86764_RAKUTENKEN株式会社様")
    assert same_thread(thread_key("Re: 【初稿修正依頼 11/24(火)まで】86764_RAKUTENKEN株式会社様"), longer)
    assert not same_thread(thread_key("見積の件"), thread_key("お見積の件について"))  # 短い件名は一致だけ
    assert is_forward("Fwd: 阪急うめだ本店") and is_forward("Re: FW: 件") and not is_forward("Re: 件")


def test_a_deadline_is_taken_only_when_the_quote_says_the_same_day():
    received = date(2026, 11, 19)
    assert check_deadline("2026-11-27", "●11月27日(金)まで●", received) == date(2026, 11, 27)
    assert check_deadline("2026-12-09", "2026年12月９日（水）厳守", received) == date(2026, 12, 9)
    assert check_deadline("2026-11-20", "【11/20(金)午前中まで】", received) == date(2026, 11, 20)
    assert check_deadline("2026-11-24", "11/18〆", received) is None           # 抜き書きと日が違う
    assert check_deadline("2026-12-27", "11月27日(金)まで", received) is None    # 月が違う
    assert check_deadline("2026-11-26", "11月26日(金)まで", received) is None    # 曜日が合わない
    assert check_deadline("2027-11-27", "11月27日まで", received) is None        # 年の取り違え
    assert check_deadline("2026-11-27", "", received) is None                    # 裏付けの文が無い
    assert check_deadline("2026-11-19", "本日中にご返信ください", received) == received
    assert check_deadline("2026-11-20", "今週中に", received) == date(2026, 11, 20)
    assert check_deadline("2026-11-25", "今週中に", received) is None
    assert relative_deadline("今月中", received) == date(2026, 11, 30)


def test_an_addressee_must_be_written_as_one():
    assert addressed_to("森", "RAKUTENKEN㈱\n森さま\nお世話になります")
    assert addressed_to("森", "ＲＡＫＵＴＥＮＫＥＮ　株式会社\n森 美明　様")
    assert addressed_to("藤田", "RAKUTENKEN株式会社\n藤田　様")
    assert not addressed_to("藤田", "お取引先様　各位")


def test_the_place_a_date_is_written_can_be_found_in_the_mail():
    text = "販売する全商品を記入し、2026年11月18日までにメール返信してください。"
    assert find_date_quote(date(2026, 11, 18), text) == "11月18日までにメール返信して"
    assert find_date_quote(date(2026, 11, 20), "着日希望：11/20(金)着 でお願いします") == "11/20(金)着 でお願いします"
    assert find_date_quote(date(2026, 11, 8), text) == ""  # 「11月18日」の一部を別の日と読まない
    assert find_date_quote(date(2026, 12, 1), text) == ""


def test_a_mail_of_ours_is_matched_by_the_name_it_is_written_to():
    partner = "株式会社髙島屋 安田様"
    assert written_to(partner, "株式会社高島屋\n安田様\nいつもお世話になっております。")
    assert written_to(partner, "髙島屋　安田 千稲　様")
    assert not written_to(partner, "株式会社山星屋\n遠藤様")
    assert not written_to("", "安田様")


def test_seeing_a_mail_is_not_the_same_as_answering_it():
    assert is_done_report("③ 対応完了") and is_done_report("③返信しました") and is_done_report("3番 返信不要です")
    assert is_done_report("電話で回答済みです")
    assert not is_done_report("③ 確認しました")       # メールを見ただけかもしれない
    assert not is_done_report("③はまだ返信していません")


# --- 届いたメール ---


def test_the_first_run_only_marks_what_is_there_as_seen(env):
    assert env.cw.sent == [] and state_of(env)["last_seen"] == 1001


def test_a_partner_mail_is_told_to_the_person_it_is_addressed_to(env):
    mid = partner_mail(env, quote="11月27日(金)まで", day="2026-11-27",
                       text="RAKUTENKEN㈱\n森さま\n11月27日(金)までにご回答ください。")
    assert env.watcher.run_once() == 1
    room, body = env.cw.sent[0]
    assert room == ROOM
    assert body.startswith(f"[To:{MORI}]森さん\n取引先からメールが届いています。")
    assert f"[To:{FUJITA}]" not in body
    assert "① 株式会社山星屋 遠藤様　→ 森さん" in body
    assert "件名: 確認：天津甘栗：パッケージの件" in body
    assert "返信: 必要（ロゴシールを貼るか回答してほしい）" in body
    assert "期日: 11月27日（金）・あと8日　原文「11月27日(金)まで」" in body
    assert f"メール: https://www.chatwork.com/#!rid{ROOM}-{mid}" in body
    assert "「① 対応完了」" in body
    item = state_of(env)["open"][0]
    assert item["number"] == 1 and item["deadline"] == "2026-11-27" and item["recipients"] == [MORI]


def test_a_mail_addressed_to_nobody_goes_to_everyone(env):
    partner_mail(env, subject="【要確認】フードイベントプラザ", text="お取引先様\n各位\nご確認ください。",
                 staff=[], addressee="お取引先様 各位")
    env.watcher.run_once()
    body = env.cw.sent[0][1]
    assert body.startswith(f"[To:{MORI}]森さん [To:{FUJITA}]藤田さん\n")
    assert "→ 宛名が「お取引先様 各位」のため、森さん・藤田さんへ" in body


def test_a_name_the_model_made_up_is_not_used(env):
    # モデルが藤田さんを挙げても、本文の宛名に無ければ採らない（全員へ知らせる）
    partner_mail(env, staff=["藤田"])
    env.watcher.run_once()
    assert state_of(env)["open"][0]["recipients"] == [MORI, FUJITA]


def test_ads_and_system_mails_are_dropped_without_reading_them(env):
    env.cw.mail("support@superdelivery.com", "[SD]【特集】クリスマスギフト", "特集のご紹介です。")
    env.cw.mail("info@jpts-unity.jp", "出荷指示のお知らせ", "出荷指示がありました。")
    assert env.watcher.run_once() == 0
    assert env.cw.sent == [] and env.model.calls == []


def test_an_unknown_sender_is_read_and_dropped_when_it_is_not_a_partner(env):
    env.model.reads["セミナー"] = read(category="広告・メルマガ", needs_reply=False)
    env.cw.mail("news@example.co.jp", "無料セミナーのご案内", "ぜひご参加ください。")
    assert env.watcher.run_once() == 0
    assert env.cw.sent == [] and len(env.model.calls) == 1


def test_a_mail_that_only_shares_information_is_told_but_not_followed(env):
    partner_mail(env, subject="【情報共有】売上見込について", needs_reply=False, ask="",
                 summary="今週分の売上想定の共有です。")
    env.watcher.run_once()
    body = env.cw.sent[0][1]
    assert "・ 株式会社山星屋 遠藤様" in body and "①" not in body
    assert "返信: 不要と読みました" in body and "期日: 記載なし" in body
    assert "対応完了" not in body  # 追わないメールに、完了の返事は求めない
    assert state_of(env)["open"] == []


def test_a_deadline_the_code_cannot_confirm_is_handed_to_a_person(env):
    partner_mail(env, subject="11/18〆 見積書送付依頼", text="森様\n11/18〆でお願いします。",
                 quote="11/18〆", day="2026-11-24")
    env.watcher.run_once()
    body = env.cw.sent[0][1]
    assert "期日: 「11/18〆」とありますが、日付を確定できませんでした。" in body
    item = state_of(env)["open"][0]
    assert item["deadline"] == "" and item["deadline_unsure"] is True and item["number"] == 1


def test_a_deadline_quote_that_is_not_in_the_mail_is_ignored(env):
    partner_mail(env, quote="11月25日まで", day="2026-11-25", needs_reply=False)
    env.watcher.run_once()
    assert "期日: 記載なし" in env.cw.sent[0][1] and state_of(env)["open"] == []


def test_several_mails_in_one_round_are_told_in_one_post(env):
    partner_mail(env)
    partner_mail(env, subject="福袋について", text="藤田様\nいかがでしょうか。", staff=["藤田"])
    partner_mail(env, subject="【情報共有】売上", needs_reply=False, ask="")
    assert env.watcher.run_once() == 3
    assert len(env.cw.sent) == 1
    body = env.cw.sent[0][1]
    assert body.startswith(f"[To:{MORI}]森さん [To:{FUJITA}]藤田さん\n取引先からのメールが3件届いています。")
    assert "① " in body and "② " in body and "\n・ " in body


def test_the_same_mail_arriving_twice_is_told_once(env):
    # 実例（2026-11-17・18）: 京阪百貨店の同じメールが、同じ受信日時で2回流れた
    partner_mail(env, needs_reply=False, ask="")
    env.watcher.run_once()
    partner_mail(env, needs_reply=False, ask="")
    assert env.watcher.run_once() == 0 and len(env.cw.sent) == 1


def test_a_long_notice_is_split_and_the_numbers_go_on(env):
    env.config.data["mail_watch"]["max_chars"] = 250
    for n in range(3):
        partner_mail(env, subject=f"確認{n}")
    env.watcher.run_once()
    assert len(env.cw.sent) == 3
    assert "取引先からのメールが3件届いています。" in env.cw.sent[0][1] and "① " in env.cw.sent[0][1]
    assert "（続き）" in env.cw.sent[2][1] and "③ " in env.cw.sent[2][1] and "「③ 対応完了」" in env.cw.sent[2][1]


def test_a_date_the_model_did_not_quote_exactly_is_looked_up_in_the_mail(env):
    partner_mail(env, subject="【出店】フードイベント", quote="11月18日までに返信", day="2026-11-25",
                 text="森様\n全商品を記入し、2026年11月25日までにメール返信してください。")
    env.watcher.run_once()
    assert "期日: 11月25日（水）・あと6日　原文「11月25日までにメール返信して」" in env.cw.sent[0][1]


def test_nothing_is_read_or_posted_outside_the_hours(env):
    partner_mail(env)
    env.clock.now = datetime(2026, 11, 19, 21, 0, tzinfo=JST)
    assert env.watcher.run_once() == 0 and env.cw.sent == []
    env.clock.now = datetime(2026, 11, 21, 10, 0, tzinfo=JST)  # 土曜
    assert env.watcher.run_once() == 0 and env.cw.sent == []
    env.clock.now = datetime(2026, 11, 23, 8, 30, tzinfo=JST)  # 月曜（祝日でも平日と同じ）
    assert env.watcher.run_once() == 1


def test_the_log_keeps_no_subject_and_no_body(env):
    partner_mail(env)
    env.watcher.run_once()
    logged = json.dumps(audit_of(env), ensure_ascii=False)
    assert "mail_notice" in logged and "arista.co.jp" in logged
    assert "天津甘栗" not in logged and "ロゴシール" not in logged


def test_the_room_is_not_a_question_room():
    config = Config.load()
    settings = config.mail_watch
    assert settings["room_id"] == ROOM and settings["room_id"] not in config.allowed_room_ids
    assert [s["name"] for s in settings["staff"]] == ["森", "藤田"]


# --- 当社の返信 ---


def reply_from_fujita(env, subject="Re: 確認：天津甘栗：パッケージの件", text="遠藤様\nロゴシールは貼りません。", **kw):
    return env.cw.mail("fujita@rakutenken.jp", subject, text, **kw)


def test_our_reply_is_read_and_closes_the_mail(env):
    partner_mail(env)
    env.watcher.run_once()
    env.model.checks["天津甘栗"] = check(ANSWERED)
    rid = reply_from_fujita(env)
    env.watcher.run_once()
    body = env.cw.sent[1][1]
    assert "[To:" not in body  # 済んだ知らせで呼び出さない
    assert "① 株式会社山星屋 遠藤様 への返信を確認しました（藤田さん・11/19（木）09:55）。" in body
    assert "返信の内容: ロゴシールは貼らないと回答しました。" in body
    assert f"#!rid{ROOM}-{rid}" in body
    assert "① は完了にします。返信・対応待ちのメールは、これでありません。" in body
    state = state_of(env)
    assert state["open"] == [] and state["closed_log"][0]["how"] == "reply"
    # 確認には、届いたメールの求めと当社の返信の両方を渡している
    kind, text = env.model.calls[-1]
    assert kind == "check" and "先方の求め: ロゴシールを貼るか回答してほしい" in text and "ロゴシールは貼りません。" in text


def test_a_reply_that_only_acknowledges_keeps_the_mail_open(env):
    partner_mail(env)
    env.watcher.run_once()
    env.model.checks["天津甘栗"] = check(ACK_ONLY, "確認して改めて連絡すると伝えました。", "ロゴシールを貼るかどうかの回答")
    reply_from_fujita(env, text="遠藤様\n確認して改めてご連絡します。", cut=True)
    env.watcher.run_once()
    body = env.cw.sent[1][1]
    assert "[To:" not in body  # 途中経過の返信では呼び出さない（残っていることは翌朝の一覧で知らせる）
    assert "受け取りの連絡や後日の回答の予告のように読めました。" in body
    assert "残っていそうな点: ロゴシールを貼るかどうかの回答" in body
    assert "後半は確認できていません" in body
    assert "「① 対応完了」とお知らせください" in body
    item = state_of(env)["open"][0]
    assert item["replies"][0]["verdict"] == ACK_ONLY


def test_a_partial_reply_is_asked_about_not_judged(env):
    partner_mail(env)
    env.watcher.run_once()
    env.model.checks["天津甘栗"] = check(PARTIAL, remaining="通常パッケージに戻る時期")
    reply_from_fujita(env)
    env.watcher.run_once()
    assert "求めの一部への回答のように読めました。" in env.cw.sent[1][1]
    assert len(state_of(env)["open"]) == 1


def test_a_reply_under_a_new_subject_is_found_by_who_it_is_written_to(env):
    # 実例（2026-11-16）: 髙島屋の「産地証明書を提出してほしい」に、「マロングラッセ産地証明」という
    # 新しい件名で送っていた。件名が違っても、宛名にその取引先の名前があれば中身を読んで確かめる
    partner_mail(env, subject="RE: 初校のお願いです", text="藤田様\n産地証明書をご提出ください。", staff=["藤田"],
                 partner="株式会社髙島屋 安田様", ask="マロングラッセの産地証明書を提出してほしい")
    env.watcher.run_once()
    env.model.checks["マロングラッセ産地証明"] = check(ANSWERED, "産地証明書を添付して送付しました。")
    env.cw.mail("fujita@rakutenken.jp", "マロングラッセ産地証明", "株式会社高島屋\n安田様\n産地証明書を送付します。",
                attached="産地証明書.pdf")
    env.watcher.run_once()
    assert "① 株式会社髙島屋 安田様 への返信を確認しました（藤田さん・" in env.cw.sent[-1][1]
    assert state_of(env)["open"] == []


def test_a_mail_of_ours_to_someone_else_is_not_read_against_the_open_ones(env):
    partner_mail(env)  # 山星屋 遠藤様あて
    env.watcher.run_once()
    calls = len(env.model.calls)
    env.cw.mail("fujita@rakutenken.jp", "１２月発注分の件", "株式会社髙山\n営業事務 御中\n別添ご確認下さい。")
    env.watcher.run_once()
    assert len(env.model.calls) == calls and len(state_of(env)["open"]) == 1


def test_a_colleague_who_replies_is_called_by_name(env):
    env.config.data["mail_watch"]["colleagues"] = [{"name": "福本", "addresses": ["jimu@rakutenken.co.jp"]}]
    partner_mail(env)
    env.watcher.run_once()
    env.model.checks["天津甘栗"] = check(ANSWERED)
    env.cw.mail("jimu@rakutenken.co.jp", "Re: 確認：天津甘栗：パッケージの件", "遠藤様\n貼りません。")
    env.watcher.run_once()
    assert "への返信を確認しました（福本さん・" in env.cw.sent[-1][1]


def test_several_interim_replies_in_one_round_are_noted_once(env):
    # 実例（2026-11-23）: 15分の間に途中経過の返信が2通続いた。知らせるのは最後の1通だけ
    partner_mail(env)
    env.watcher.run_once()
    env.model.checks["天津甘栗"] = check(ACK_ONLY, "追って報告すると伝えました。", "ロゴシールを貼るかどうかの回答")
    reply_from_fujita(env, text="遠藤様\n確認します。", received="2026-11-19 10:10")
    reply_from_fujita(env, text="遠藤様\n追ってご報告します。", received="2026-11-19 10:25")
    env.watcher.run_once()
    assert len(env.cw.sent) == 2 and "（藤田さん・11/19（木）10:25）" in env.cw.sent[1][1]
    assert len(state_of(env)["open"][0]["replies"]) == 2


def test_forwards_and_unrelated_mails_of_ours_change_nothing(env):
    partner_mail(env)
    env.watcher.run_once()
    env.cw.mail("ymori@rakutenken.co.jp", "Fwd: 確認：天津甘栗：パッケージの件", "藤田さん、対応お願いします。")
    env.cw.mail("fujita@rakutenken.jp", "１２月発注分及び年末年始休業の案内", "各位")
    reply_from_fujita(env)  # 件名は同じだが、モデルは別件と読んだ
    calls = len(env.model.calls)
    env.watcher.run_once()
    assert len(env.cw.sent) == 1 and len(state_of(env)["open"]) == 1
    assert len(env.model.calls) == calls + 1  # 転送と別の件名には、モデルを使わない


def test_a_follow_up_takes_over_the_number_and_the_deadline(env):
    partner_mail(env, subject="休業日のご確認", text="森様\n11月27日(金)までにご返信ください。",
                 quote="11月27日(金)まで", day="2026-11-27")
    env.watcher.run_once()
    env.model.reads["RE: 休業日のご確認"] = read(staff=["森"], summary="リマインドです。")
    env.cw.mail("endo.masami@arista.co.jp", "RE: 休業日のご確認", "森様\nご返信をお待ちしております。",
                received="2026-11-19 13:20")
    env.watcher.run_once()
    body = env.cw.sent[1][1]
    assert "① 株式会社山星屋 遠藤様（①の続き）" in body
    assert "期日: 11月27日（金）・あと8日" in body
    state = state_of(env)
    assert [i["number"] for i in state["open"]] == [1] and state["closed_log"][0]["how"] == "superseded"
    # 続きを読むときは、先に届いた分を添えている
    assert "[先に届いて、まだ返信・対応が済んでいないメール]" in env.model.calls[-1][1]


# --- 毎朝の一覧と、期日当日の確認 ---


def test_every_morning_the_unanswered_mails_are_listed_once(env):
    partner_mail(env, quote="11月27日(金)まで", day="2026-11-27",
                 text="森さま\n11月27日(金)までにご回答ください。")
    partner_mail(env, subject="福袋について", text="藤田様\nいかがでしょうか。", staff=["藤田"],
                 partner="株式会社大丸松坂屋百貨店 山岡様", ask="チラシの内容でよいか回答してほしい")
    env.watcher.run_once()
    env.clock.now = datetime(2026, 11, 19, 16, 0, tzinfo=JST)
    env.watcher.run_once()
    assert len(env.cw.sent) == 1  # 知らせた当日は、朝の一覧に載せない
    env.clock.now = datetime(2026, 11, 20, 8, 50, tzinfo=JST)
    env.watcher.run_once()
    assert len(env.cw.sent) == 1  # 9:00 より前
    env.clock.now = datetime(2026, 11, 20, 9, 0, tzinfo=JST)
    env.watcher.run_once()
    body = env.cw.sent[1][1]
    assert body.startswith(f"[To:{MORI}]森さん [To:{FUJITA}]藤田さん\nおはようございます。返信・対応をまだ確認できていないメールが2件あります。")
    first, second = body.index("①【期日 11/27（金）・あと7日】"), body.index("②【期日の記載なし・受信から1日】")
    assert first < second  # 期日のあるものが先
    assert "先方の求め: チラシの内容でよいか回答してほしい" in body
    env.clock.now = datetime(2026, 11, 20, 11, 0, tzinfo=JST)
    env.watcher.run_once()
    assert len(env.cw.sent) == 2  # 1日1回


def test_the_morning_list_says_what_a_reply_left_open(env):
    partner_mail(env)
    env.watcher.run_once()
    env.model.checks["天津甘栗"] = check(PARTIAL, remaining="通常パッケージに戻る時期")
    reply_from_fujita(env)
    env.watcher.run_once()
    env.clock.now = datetime(2026, 11, 20, 9, 5, tzinfo=JST)
    env.watcher.run_once()
    assert "※11/19（木）09:55に藤田さんの返信を確認しましたが、まだ残っていそうな点があります（通常パッケージに戻る時期）" in env.cw.sent[-1][1]


def test_a_deadline_without_a_reply_is_listed_only_when_it_gets_close(env):
    partner_mail(env, subject="図面の確認", text="森様\n修正があれば12月9日（水）厳守でご連絡ください。",
                 needs_reply=False, ask="修正があれば連絡", quote="12月9日（水）厳守", day="2026-12-09")
    env.watcher.run_once()
    assert "① " in env.cw.sent[0][1]  # 期日があるので番号を振って追う
    env.clock.now = datetime(2026, 11, 20, 9, 0, tzinfo=JST)
    env.watcher.run_once()
    assert len(env.cw.sent) == 1  # まだ先
    env.clock.now = datetime(2026, 12, 7, 9, 0, tzinfo=JST)
    env.watcher.run_once()
    assert "①【期日 12/9（水）・あと2日】" in env.cw.sent[1][1]


def test_on_the_day_it_is_due_the_mail_is_raised_again_in_the_afternoon(env):
    partner_mail(env, subject="企画連絡確認", text="藤田様\n【返信期日：11/20（金）まで】", staff=["藤田"],
                 quote="【返信期日：11/20（金）まで】", day="2026-11-20")
    env.watcher.run_once()
    env.clock.now = datetime(2026, 11, 20, 9, 0, tzinfo=JST)
    env.watcher.run_once()
    assert "①【本日期日】" in env.cw.sent[1][1]
    env.clock.now = datetime(2026, 11, 20, 14, 55, tzinfo=JST)
    env.watcher.run_once()
    assert len(env.cw.sent) == 2
    env.clock.now = datetime(2026, 11, 20, 15, 0, tzinfo=JST)
    env.watcher.run_once()
    body = env.cw.sent[2][1]
    assert body.startswith(f"[To:{FUJITA}]藤田さん\n期日が今日")
    assert "①【本日期日】" in body
    env.clock.now = datetime(2026, 11, 20, 16, 0, tzinfo=JST)
    env.watcher.run_once()
    assert len(env.cw.sent) == 3
    env.clock.now = datetime(2026, 11, 23, 9, 0, tzinfo=JST)
    env.watcher.run_once()
    assert "①【期日超過・11/20（金）まで】" in env.cw.sent[3][1]


def test_a_weekend_deadline_is_raised_on_the_friday(env):
    partner_mail(env, subject="企画連絡確認", text="森様\n11月22日(日)までにご返信ください。",
                 quote="11月22日(日)まで", day="2026-11-22")
    env.watcher.run_once()
    env.clock.now = datetime(2026, 11, 20, 9, 0, tzinfo=JST)
    env.watcher.run_once()
    env.clock.now = datetime(2026, 11, 20, 15, 0, tzinfo=JST)
    env.watcher.run_once()
    assert len(env.cw.sent) == 3
    assert "①【期日 11/22（日）・あと2日】" in env.cw.sent[-1][1]
    assert env.cw.sent[-1][1].startswith(f"[To:{MORI}]森さん\n期日が今日（または次の平日より前）")


def test_a_late_first_round_does_not_post_the_list_twice(env):
    partner_mail(env, subject="企画連絡確認", text="森様\n11月20日(金)までにご返信ください。",
                 quote="11月20日(金)まで", day="2026-11-20")
    env.watcher.run_once()
    env.clock.now = datetime(2026, 11, 20, 15, 30, tzinfo=JST)  # その日の最初の巡回が午後
    env.watcher.run_once()
    assert len(env.cw.sent) == 2 and "①【本日期日】" in env.cw.sent[-1][1]


def test_an_answered_mail_is_not_listed(env):
    partner_mail(env)
    env.watcher.run_once()
    env.model.checks["天津甘栗"] = check(ANSWERED)
    reply_from_fujita(env)
    env.watcher.run_once()
    env.clock.now = datetime(2026, 11, 20, 9, 0, tzinfo=JST)
    env.watcher.run_once()
    assert len(env.cw.sent) == 2  # 届いた知らせと、返信を確かめた知らせだけ


# --- 人からの返事 ---


def two_open(env):
    partner_mail(env)
    partner_mail(env, subject="福袋について", text="藤田様\nいかがでしょうか。", staff=["藤田"])
    env.watcher.run_once()
    return state_of(env)["post_log"][0]


def test_a_number_closes_that_mail(env):
    post = two_open(env)
    mid = env.cw.add(MORI, f"[rp aid={ME} to={ROOM}-{post}]経理財務アシスタントさん\n① 電話で回答済みです")
    env.watcher.run_once()
    body = env.cw.sent[-1][1]
    assert body.startswith(f"[rp aid={MORI} to={ROOM}-{mid}]\nありがとうございます。① を完了にしました。")
    assert "返信・対応待ちのメールは、あと1件です。" in body
    state = state_of(env)
    assert [i["number"] for i in state["open"]] == [2]
    assert state["closed_log"][0]["how"] == "manual" and state["closed_log"][0]["closed_by"] == MORI


def test_done_without_a_number_is_asked_about_when_there_are_several(env):
    post = two_open(env)
    env.cw.add(MORI, f"[rp aid={ME} to={ROOM}-{post}]経理財務アシスタントさん\n対応完了")
    env.watcher.run_once()
    assert "どのメールが済んだか番号を添えてお知らせください（例:「① 対応完了」）" in env.cw.sent[-1][1]
    assert len(state_of(env)["open"]) == 2


def test_done_as_a_reply_to_a_notice_of_one_mail_closes_it(env):
    partner_mail(env)
    env.watcher.run_once()
    post = state_of(env)["post_log"][0]
    env.cw.add(MORI, f"[rp aid={ME} to={ROOM}-{post}]経理財務アシスタントさん\n返信不要です")
    env.watcher.run_once()
    assert state_of(env)["open"] == [] and "① を完了にしました。" in env.cw.sent[-1][1]


def test_i_have_seen_it_does_not_close_anything(env):
    post = two_open(env)
    env.cw.add(MORI, f"[rp aid={ME} to={ROOM}-{post}]経理財務アシスタントさん\n① 確認しました")
    env.watcher.run_once()
    assert len(state_of(env)["open"]) == 2


def test_a_number_that_is_not_waiting_is_said_so(env):
    two_open(env)
    env.cw.add(FUJITA, "⑦ 対応完了")
    env.watcher.run_once()
    body = env.cw.sent[-1][1]
    assert "⑦ に当たるメールは、返信・対応待ちの中に見当たりませんでした。" in body and "を完了にしました" not in body
    assert len(state_of(env)["open"]) == 2


def test_a_closed_mail_can_be_brought_back(env):
    two_open(env)
    env.cw.add(MORI, "① 対応完了")
    env.watcher.run_once()
    env.cw.add(MORI, "①はまだでした。戻してください")
    env.watcher.run_once()
    assert "① を返信・対応待ちへ戻しました。" in env.cw.sent[-1][1]
    assert sorted(i["number"] for i in state_of(env)["open"]) == [1, 2]


def test_long_talk_between_people_is_left_alone(env):
    two_open(env)
    env.cw.add(SHINODA, f"[To:{MORI}]森さん\n①の件ですが、先方へは来週こちらから連絡して対応完了とする予定です。よろしくお願いします。")
    env.watcher.run_once()
    assert len(env.cw.sent) == 1 and len(state_of(env)["open"]) == 2


def test_asking_for_the_list_shows_what_is_waiting(env):
    two_open(env)
    env.cw.add(SHINODA, f"[To:{ME}]経理財務アシスタントさん\n未返信の一覧を教えて")
    env.watcher.run_once()
    body = env.cw.sent[-1][1]
    assert "返信・対応待ちのメールは2件です。" in body and "①【期日の記載なし・受信から0日】" in body


def test_other_mentions_get_a_short_guide(env):
    env.cw.add(SHINODA, f"[To:{ME}]経理財務アシスタントさん\nこのルームでは何ができますか")
    env.watcher.run_once()
    assert "「③ 対応完了」のように番号でお知らせください" in env.cw.sent[-1][1]


# --- 読めなかったとき・番号 ---


def test_a_mail_that_cannot_be_read_is_tried_again_then_handed_over(env):
    partner_mail(env)
    env.model.fail = 2
    env.watcher.run_once()
    env.watcher.run_once()
    assert env.cw.sent == []  # 2回までは黙ってやり直す
    env.watcher.run_once()
    assert "取引先からメールが届いています。" in env.cw.sent[0][1]  # 3回目で読めた

    partner_mail(env, subject="別の件")
    env.model.fail = 3
    for _ in range(3):
        env.watcher.run_once()
    body = env.cw.sent[1][1]
    assert "こちらで内容を確認できませんでした" in body and "件名: 別の件" in body and "[To:" not in body


def test_numbers_start_over_once_nothing_is_waiting(env):
    env.config.data["mail_watch"]["numbering"] = {"restart_after": 2}
    for n in range(3):
        partner_mail(env, subject=f"確認{n}")
    env.watcher.run_once()
    assert [i["number"] for i in state_of(env)["open"]] == [1, 2, 3]
    env.cw.add(MORI, "①②③ 対応完了")
    env.watcher.run_once()
    partner_mail(env, subject="確認9")
    env.watcher.run_once()
    assert [i["number"] for i in state_of(env)["open"]] == [1]
