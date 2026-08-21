import json
from types import SimpleNamespace

import pytest

from raizuinu.config import Config
from raizuinu.phrasing import BANKS
from raizuinu.letterpack import (
    LetterpackError,
    LetterpackFollower,
    LetterpackRunner,
    LetterpackStore,
    build_request_text,
    read_request,
    summarize_use,
)

SUPPLIES_ROOM = 345854487
STAFF_ID = 1160869
DEPT_ROOM = 384793683
REQUESTER = 6945415
AGENT = 999  # アシスタント自身のアカウント
RAKUTEN_ROOM = 392462780  # 楽天軒　備品チャット
RAKUTEN_STAFF = (
    (9228914, "篠田 笑佳"),
    (9763216, "福本 明日香"),
    (10622368, "中浦 祐子"),
    (10675817, "札葉 美早"),
)

DETAIL = {
    "company": "株式会社ライズクリエイション",
    "to_lines": ["株式会社大塚商会 御中", "販売２課　濵野 康一　様"],
    "items": [{"name": "業務委託基本契約書", "qty": "2部"}],
    "staff": "足立",
}


def make_config(tmp_path):
    config = Config.load(tmp_path / "no-config.json")
    config.data["letterpack"] = {
        "enabled": True,
        "routes": {
            "default": {
                "room_id": SUPPLIES_ROOM,
                "recipients": [{"account_id": STAFF_ID, "name": "坂口 美代子"}],
            },
            "RAKUTENKEN": {
                "room_id": RAKUTEN_ROOM,
                "recipients": [
                    {"account_id": a, "name": n} for a, n in RAKUTEN_STAFF
                ],
            },
        },
        "follow_up": True,
        "reply_window_minutes": 120,
        "max_open_days": 7,
    }
    config.data["state_dir"] = str(tmp_path / "state")
    config.base_dir = tmp_path
    return config


class FakeChatwork:
    def __init__(self, messages=None):
        self.sent = []
        self._messages = messages or []
        self._next_id = 9000

    def send_message(self, room_id, body):
        self._next_id += 1
        self.sent.append((room_id, body))
        return str(self._next_id)

    def get_recent_messages(self, room_id, limit=20):
        return self._messages

    def get_me(self):
        return AGENT


def fake_client(payload):
    response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps(payload, ensure_ascii=False))],
        usage=SimpleNamespace(
            input_tokens=300, output_tokens=80,
            cache_creation_input_tokens=0, cache_read_input_tokens=0,
        ),
    )
    client = SimpleNamespace(kwargs=None)

    def create(**kwargs):
        client.kwargs = kwargs
        return response

    client.messages = SimpleNamespace(create=create)
    return client


def runner(tmp_path, chatwork=None):
    return LetterpackRunner(make_config(tmp_path), chatwork or FakeChatwork())


class TestReadRequest:
    @pytest.mark.parametrize(
        "text,count,kind",
        [
            ("2枚でお願いします", 2, ""),
            ("レターパックライトを3枚", 3, "レターパックライト"),
            ("プラス1枚", 1, "レターパックプラス"),
            ("ライト２枚", 2, "レターパックライト"),  # 全角数字
            ("枚数はまだ未定", None, ""),
        ],
    )
    def test_count_and_kind(self, text, count, kind):
        got = read_request(text)
        assert (got["count"], got["kind"]) == (count, kind)
        assert got["declined"] is False

    @pytest.mark.parametrize(
        "text", ["不要です", "いらないです", "いいえ", "今回は結構です", "見送ります"]
    )
    def test_declines(self, text):
        assert read_request(text)["declined"] is True


class TestRequestText:
    def test_the_request_carries_the_four_items(self):
        text = build_request_text(DETAIL, 2, "レターパックライト")
        assert "・使用会社名: 株式会社ライズクリエイション" in text
        assert "・宛先と使用内容: 株式会社大塚商会 御中／業務委託基本契約書 2部の送付" in text
        assert "・必要枚数: 2枚" in text
        assert "・種類: レターパックライト" in text

    def test_it_says_whose_request_it_is_without_naming_itself(self):
        # 総務から見て誰に確認すればよいか分かること。名乗りは入れない
        text = build_request_text(DETAIL, 1, "レターパックプラス")
        assert text.startswith(
            "お疲れさまです。\n経理財務部の足立さんの依頼です。レターパックの手配を"
        )
        assert "アシスタント" not in text

    def test_an_unknown_requester_leaves_no_dangling_phrase(self):
        text = build_request_text({**DETAIL, "staff": ""}, 1, "レターパックプラス")
        assert text.startswith(
            "お疲れさまです。\nレターパックの手配をお願いできますでしょうか。"
        )

    def test_missing_recipient_is_marked_not_invented(self):
        assert "（宛先未確認）" in summarize_use({"to_lines": [], "items": []})


class TestOfferFlow:
    def test_declining_closes_it_without_posting(self, tmp_path):
        chatwork = FakeChatwork()
        run = runner(tmp_path, chatwork)
        run.offer(DEPT_ROOM, REQUESTER, 1000, DETAIL)
        reply = run.handle(DEPT_ROOM, REQUESTER, 1100, "不要です")
        assert reply in BANKS["letterpack_declined"]  # 言い回しは選び直すが意味は同じ
        assert chatwork.sent == []  # 総務へは何も送らない
        assert run.handle(DEPT_ROOM, REQUESTER, 1200, "ありがとう") is None

    def test_missing_kind_is_asked_for(self, tmp_path):
        run = runner(tmp_path)
        run.offer(DEPT_ROOM, REQUESTER, 1000, DETAIL)
        reply = run.handle(DEPT_ROOM, REQUESTER, 1100, "2枚お願いします")
        assert "種類" in reply and "プラス" in reply
        # 覚えているので、種類だけ答えれば文面が出る
        reply = run.handle(DEPT_ROOM, REQUESTER, 1200, "ライトで")
        assert "・必要枚数: 2枚" in reply
        assert "・種類: レターパックライト" in reply

    def test_draft_is_shown_before_anything_is_posted(self, tmp_path):
        chatwork = FakeChatwork()
        run = runner(tmp_path, chatwork)
        run.offer(DEPT_ROOM, REQUESTER, 1000, DETAIL)
        reply = run.handle(DEPT_ROOM, REQUESTER, 1100, "ライト2枚で")
        assert "「送信」とお返事ください" in reply
        assert chatwork.sent == []  # 確認前に他部署のルームへは出さない

    def test_the_preview_does_not_notify_the_other_department(self, tmp_path):
        # 確認の段階で総務へ通知が飛ぶと、送るかどうかを決める前に相手を巻き込む。
        # 本文ごとサニタイズし、宛先タグを全角へ落として無効にする
        from raizuinu.answer import sanitize_for_chatwork

        run = runner(tmp_path)
        run.offer(DEPT_ROOM, REQUESTER, 1000, DETAIL)
        preview = run.handle(DEPT_ROOM, REQUESTER, 1100, "ライト2枚で")
        assert f"[To:{STAFF_ID}]" not in preview
        assert sanitize_for_chatwork(f"[To:{STAFF_ID}]") in preview  # 見た目は残す

    def test_send_posts_to_the_supplies_room(self, tmp_path):
        chatwork = FakeChatwork()
        run = runner(tmp_path, chatwork)
        run.offer(DEPT_ROOM, REQUESTER, 1000, DETAIL)
        run.handle(DEPT_ROOM, REQUESTER, 1100, "ライト2枚で")
        reply = run.handle(DEPT_ROOM, REQUESTER, 1200, "送信", display_name="足立 海里")
        room_id, body = chatwork.sent[0]
        assert room_id == SUPPLIES_ROOM
        assert f"[To:{STAFF_ID}]" in body
        assert "・必要枚数: 2枚" in body
        assert "依頼しました" in reply
        assert f"#!rid{SUPPLIES_ROOM}-" in reply  # 投稿先へのリンク

    def test_cancelling_the_draft_sends_nothing(self, tmp_path):
        chatwork = FakeChatwork()
        run = runner(tmp_path, chatwork)
        run.offer(DEPT_ROOM, REQUESTER, 1000, DETAIL)
        run.handle(DEPT_ROOM, REQUESTER, 1100, "ライト2枚で")
        reply = run.handle(DEPT_ROOM, REQUESTER, 1200, "やっぱり取り消しで")
        assert reply in BANKS["letterpack_cancelled"]
        assert chatwork.sent == []

    def test_an_expired_offer_is_left_to_normal_routing(self, tmp_path):
        run = runner(tmp_path)
        run.offer(DEPT_ROOM, REQUESTER, 1000, DETAIL)
        assert run.handle(DEPT_ROOM, REQUESTER, 1000 + 3 * 3600, "2枚") is None

    def test_another_person_in_the_same_room_is_not_affected(self, tmp_path):
        run = runner(tmp_path)
        run.offer(DEPT_ROOM, REQUESTER, 1000, DETAIL)
        assert run.handle(DEPT_ROOM, 9129422, 1100, "2枚") is None

    def test_posting_failure_is_reported_not_swallowed(self, tmp_path):
        class Broken(FakeChatwork):
            def send_message(self, room_id, body):
                raise RuntimeError("HTTP 403")

        run = runner(tmp_path, Broken())
        run.offer(DEPT_ROOM, REQUESTER, 1000, DETAIL)
        run.handle(DEPT_ROOM, REQUESTER, 1100, "ライト2枚で")
        with pytest.raises(LetterpackError):
            run.handle(DEPT_ROOM, REQUESTER, 1200, "送信")


def open_thread(tmp_path, chatwork):
    """総務へ投稿済みの状態を作る。→ (runner, 依頼を投稿したメッセージID)"""
    run = LetterpackRunner(make_config(tmp_path), chatwork)
    run.offer(DEPT_ROOM, REQUESTER, 1000, DETAIL)
    run.handle(DEPT_ROOM, REQUESTER, 1100, "ライト2枚で")
    run.handle(DEPT_ROOM, REQUESTER, 1200, "送信", display_name="足立 海里")
    return run, chatwork._next_id


def staff_message(message_id, body, reply_to=None, to=None):
    """総務からの発言。reply_to を指定すると、その投稿への「返信」になる。"""
    tags = ""
    if reply_to is not None:
        tags += f"[rp aid={AGENT} to={SUPPLIES_ROOM}-{reply_to}] "
    if to is not None:
        tags += f"[To:{to}] "
    return {
        "message_id": str(message_id),
        "body": tags + body,
        "account": {"account_id": STAFF_ID, "name": "坂口 美代子"},
    }


class TestFollowUp:
    def test_completion_is_relayed_and_thanked(self, tmp_path):
        chatwork = FakeChatwork()
        _, posted = open_thread(tmp_path, chatwork)
        chatwork._messages = [
            staff_message(posted + 5, "用意しました。総務の棚に置いています。", reply_to=posted)
        ]
        chatwork.sent.clear()

        LetterpackFollower(
            make_config(tmp_path),
            chatwork,
            client=fake_client(
                {"kind": "完了", "summary": "レターパックライト2枚を総務の棚に用意いただきました。", "question": ""}
            ),
        ).run_once()

        rooms = [room for room, _ in chatwork.sent]
        assert DEPT_ROOM in rooms and SUPPLIES_ROOM in rooms
        to_requester = next(b for r, b in chatwork.sent if r == DEPT_ROOM)
        assert f"[To:{REQUESTER}] 足立 海里さん" in to_requester
        assert "総務の棚に用意" in to_requester
        thanks = next(b for r, b in chatwork.sent if r == SUPPLIES_ROOM)
        assert "ありがとうございます" in thanks

    def test_a_reply_addressed_to_us_without_the_reply_button_still_counts(self, tmp_path):
        # 「返信」ではなく宛先タグで返された場合。進行中が1件なら判別できる
        chatwork = FakeChatwork()
        _, posted = open_thread(tmp_path, chatwork)
        chatwork._messages = [staff_message(posted + 5, "用意しました", to=AGENT)]
        chatwork.sent.clear()
        LetterpackFollower(
            make_config(tmp_path), chatwork,
            client=fake_client({"kind": "完了", "summary": "用意いただきました。", "question": ""}),
        ).run_once()
        assert DEPT_ROOM in [room for room, _ in chatwork.sent]

    @pytest.mark.parametrize(
        "message",
        [
            # 他の人の依頼への返事（タグなし）。実際にこれを横取りしていた
            "備品は明日届く予定です。受け取り後、C欄を受け取り済みに変更してください。",
            # 別の依頼者への返信
            "[rp aid=8681926 to=345854487-7777] 承知しました、手配します。",
            # 別ルームへの返信タグ
            "[rp aid=999 to=384793683-9001] こちらは別のルームの話です。",
        ],
    )
    def test_someone_elses_conversation_is_not_hijacked(self, tmp_path, message):
        chatwork = FakeChatwork()
        _, posted = open_thread(tmp_path, chatwork)
        chatwork._messages = [
            {
                "message_id": str(posted + 5), "body": message,
                "account": {"account_id": STAFF_ID, "name": "坂口 美代子"},
            }
        ]
        chatwork.sent.clear()
        LetterpackFollower(make_config(tmp_path), chatwork, client=None).run_once()
        assert chatwork.sent == []  # 依頼者にもルームにも何も出さない

    def test_an_ambiguous_mention_is_left_alone_when_several_are_open(self, tmp_path):
        # 進行中が複数あると、宛先タグだけではどの件への返事か決められない
        chatwork = FakeChatwork()
        run, posted = open_thread(tmp_path, chatwork)
        run.offer(DEPT_ROOM, 9129422, 2000, DETAIL)
        run.handle(DEPT_ROOM, 9129422, 2100, "ライト1枚で")
        run.handle(DEPT_ROOM, 9129422, 2200, "送信", display_name="伊藤")
        chatwork._messages = [staff_message(chatwork._next_id + 5, "用意しました", to=AGENT)]
        chatwork.sent.clear()
        LetterpackFollower(make_config(tmp_path), chatwork, client=None).run_once()
        assert chatwork.sent == []

    def test_a_question_is_relayed_and_the_answer_goes_back(self, tmp_path):
        chatwork = FakeChatwork()
        run, posted = open_thread(tmp_path, chatwork)
        chatwork._messages = [
            staff_message(posted + 5, "いつまでに必要でしょうか？", reply_to=posted)
        ]
        chatwork.sent.clear()

        LetterpackFollower(
            make_config(tmp_path),
            chatwork,
            client=fake_client(
                {"kind": "質問", "summary": "いつまでに必要か聞かれています。", "question": "納期"}
            ),
        ).run_once()
        to_requester = next(b for r, b in chatwork.sent if r == DEPT_ROOM)
        assert "いつまでに必要か" in to_requester
        # 相手を放置しない。ただし勝手に答えず、確認する旨だけを返す
        ack = next(b for r, b in chatwork.sent if r == SUPPLIES_ROOM)
        assert "依頼者に確認のうえ、折り返しご連絡します" in ack

        chatwork.sent.clear()
        # メンションのタグを外すと宛名だけが残る。そのまま転送すると先方に浮いて見える
        reply = run.handle(
            DEPT_ROOM, REQUESTER, 99999999, "経理財務アシスタントさん\n明後日までにお願いします"
        )
        assert reply in BANKS["letterpack_forwarded"]
        room_id, body = chatwork.sent[0]
        assert room_id == SUPPLIES_ROOM
        assert "明後日までにお願いしますとのことです。" in body  # こちらが伝える形にする
        assert "経理財務アシスタントさん" not in body

    def test_the_requester_has_days_to_answer_not_hours(self, tmp_path):
        # 先方の質問への回答は相手の都合次第。短い窓で切ると、答えても届かない
        chatwork = FakeChatwork()
        run, posted = open_thread(tmp_path, chatwork)
        chatwork._messages = [
            staff_message(posted + 5, "いつ取りに来られますか？", reply_to=posted)
        ]
        LetterpackFollower(
            make_config(tmp_path), chatwork,
            client=fake_client(
                {"kind": "質問", "arranged": True, "summary": "受け取り日の確認です。", "question": "受け取り日"}
            ),
        ).run_once()
        chatwork.sent.clear()
        import time

        later = int(time.time()) + 20 * 3600  # 20時間後に回答
        assert run.handle(DEPT_ROOM, REQUESTER, later, "明日伺います") is not None
        assert chatwork.sent[0][0] == SUPPLIES_ROOM

    def test_a_reply_to_our_follow_up_is_picked_up_too(self, tmp_path):
        # 依頼者の答えを総務へ返したあと、そこへの返信も同じやり取りとして拾う
        chatwork = FakeChatwork()
        run, posted = open_thread(tmp_path, chatwork)
        chatwork._messages = [
            staff_message(posted + 5, "いつまでに必要でしょうか？", reply_to=posted)
        ]
        LetterpackFollower(
            make_config(tmp_path), chatwork,
            client=fake_client({"kind": "質問", "summary": "納期の確認です。", "question": "納期"}),
        ).run_once()
        run.handle(DEPT_ROOM, REQUESTER, 99999999, "明後日までにお願いします")
        followup_id = chatwork._next_id  # 総務へ返した投稿

        chatwork._messages = [
            # メッセージIDは時系列で増える。前回の巡回より後の投稿になるようにする
            staff_message(posted + 20, "承知しました、明日用意します。", reply_to=followup_id)
        ]
        chatwork.sent.clear()
        LetterpackFollower(
            make_config(tmp_path), chatwork,
            client=fake_client({"kind": "完了", "summary": "明日用意いただけるそうです。", "question": ""}),
        ).run_once()
        assert "明日用意" in next(b for r, b in chatwork.sent if r == DEPT_ROOM)

    def test_a_reply_is_never_left_without_an_acknowledgement(self, tmp_path):
        """実例（2026-08-21）: 総務の「承知しました。お渡しいたしますので
        取りにきていただけるでしょうか。」に何も返さず、依頼者の回答待ちで
        止まっていた。相手からは無視されたように見える。
        """
        chatwork = FakeChatwork()
        _, posted = open_thread(tmp_path, chatwork)
        chatwork._messages = [
            staff_message(
                posted + 5,
                "承知しました。お渡しいたしますので取りにきていただけるでしょうか。",
                reply_to=posted,
            )
        ]
        chatwork.sent.clear()
        LetterpackFollower(
            make_config(tmp_path),
            chatwork,
            client=fake_client(
                {
                    "kind": "質問", "arranged": True,
                    "summary": "手配いただき、受け取りに来てほしいとのことです。",
                    "question": "受け取りに行けるか",
                }
            ),
        ).run_once()

        ack = next(b for r, b in chatwork.sent if r == SUPPLIES_ROOM)
        assert f"[To:{STAFF_ID}]" in ack  # 返してくれた本人あて
        assert "ご手配ありがとうございます" in ack  # 手配済みなら礼を言う
        assert "折り返しご連絡します" in ack  # 勝手に答えず、確認する旨だけ
        assert DEPT_ROOM in [r for r, _ in chatwork.sent]  # 依頼者へも伝える

    @pytest.mark.parametrize(
        "verdict",
        [
            {"kind": "質問", "arranged": False, "summary": "在庫を確認中とのことです。", "question": "在庫"},
            {"kind": "その他", "arranged": False, "summary": "受け取りました。", "question": ""},
            {"kind": "完了", "arranged": True, "summary": "用意いただきました。", "question": ""},
        ],
    )
    def test_something_always_goes_back_whatever_the_verdict(self, tmp_path, verdict):
        chatwork = FakeChatwork()
        _, posted = open_thread(tmp_path, chatwork)
        chatwork._messages = [staff_message(posted + 5, "本文", reply_to=posted)]
        chatwork.sent.clear()
        LetterpackFollower(
            make_config(tmp_path), chatwork, client=fake_client(verdict)
        ).run_once()
        rooms = [r for r, _ in chatwork.sent]
        assert SUPPLIES_ROOM in rooms and DEPT_ROOM in rooms

    def test_the_answer_is_reported_not_ventriloquised(self, tmp_path):
        # 依頼者の言葉をそのまま並べると、依頼者本人が喋っているように読める
        from raizuinu.letterpack import quote_answer

        assert quote_answer("取りに行きます。") == "取りに行きます。とのことです。"
        assert quote_answer("明日でお願いします") == "明日でお願いしますとのことです。"
        # すでに伝聞の形なら重ねない
        for already in ("木曜に伺うとのことです", "受け取れるそうです", "伺うと言っています"):
            assert quote_answer(already) == already

    @pytest.mark.parametrize(
        "waited,expects_apology",
        [(0, False), (60, False), (299, False), (300, True), (7200, True)],
    )
    def test_it_only_apologises_when_it_actually_kept_them_waiting(
        self, waited, expects_apology
    ):
        from raizuinu.letterpack import confirmed_lead

        lead = confirmed_lead(waited)
        assert ("お待たせしました" in lead) is expects_apology
        assert "依頼者に確認しました。" in lead  # 何をしたかは必ず言う

    def test_a_quick_answer_carries_no_apology_end_to_end(self, tmp_path):
        chatwork = FakeChatwork()
        run, posted = open_thread(tmp_path, chatwork)
        chatwork._messages = [
            staff_message(posted + 5, "いつ取りに来られますか？", reply_to=posted)
        ]
        LetterpackFollower(
            make_config(tmp_path), chatwork,
            client=fake_client(
                {"kind": "質問", "arranged": True, "summary": "受け取り日の確認です。", "question": "受け取り日"}
            ),
        ).run_once()
        import json as _json
        import time

        # 巡回が付けた asked_ts を読み、その1分後に答えた場合を作る
        state = _json.loads(
            (tmp_path / "state" / "letterpack.json").read_text(encoding="utf-8")
        )
        asked_ts = next(t["asked_ts"] for t in state["threads"].values() if t.get("asked_ts"))
        chatwork.sent.clear()
        run.handle(DEPT_ROOM, REQUESTER, int(asked_ts) + 60, "取りに行きます。")
        body = next(b for r, b in chatwork.sent if r == SUPPLIES_ROOM)
        assert body.startswith(f"[To:{STAFF_ID}] 坂口 美代子さん\n依頼者に確認しました。")
        assert "お待たせしました" not in body
        assert "取りに行きます。とのことです。" in body

        # 6時間後の回答なら詫びる
        chatwork.sent.clear()
        chatwork._messages = [
            staff_message(posted + 20, "いつ取りに来られますか？", reply_to=posted)
        ]
        LetterpackFollower(
            make_config(tmp_path), chatwork,
            client=fake_client(
                {"kind": "質問", "arranged": True, "summary": "再確認です。", "question": "受け取り日"}
            ),
        ).run_once()
        state = _json.loads(
            (tmp_path / "state" / "letterpack.json").read_text(encoding="utf-8")
        )
        asked_ts = next(t["asked_ts"] for t in state["threads"].values() if t.get("asked_ts"))
        chatwork.sent.clear()
        run.handle(DEPT_ROOM, REQUESTER, int(asked_ts) + 6 * 3600, "明日伺います")
        body = next(b for r, b in chatwork.sent if r == SUPPLIES_ROOM)
        assert "お待たせしました。依頼者に確認しました。" in body

    def test_a_new_request_is_not_swallowed_as_an_answer(self, tmp_path):
        chatwork = FakeChatwork()
        run, posted = open_thread(tmp_path, chatwork)
        chatwork._messages = [
            staff_message(posted + 5, "いつまでに必要ですか？", reply_to=posted)
        ]
        LetterpackFollower(
            make_config(tmp_path),
            chatwork,
            client=fake_client({"kind": "質問", "summary": "納期の確認です。", "question": "納期"}),
        ).run_once()
        # 質問待ちでも、別件の書類作成依頼はそちらへ回す
        assert run.handle(DEPT_ROOM, REQUESTER, 99999999, "南都銀行あての送付状を作って") is None

    def test_nothing_open_means_no_api_call(self, tmp_path):
        chatwork = FakeChatwork(messages=[staff_message(1, "用意しました")])
        LetterpackFollower(make_config(tmp_path), chatwork, client=None).run_once()
        assert chatwork.sent == []

    def test_messages_from_others_are_ignored(self, tmp_path):
        chatwork = FakeChatwork()
        _, posted = open_thread(tmp_path, chatwork)
        chatwork._messages = [
            {
                "message_id": str(posted + 5),
                "body": f"[rp aid=999 to={SUPPLIES_ROOM}-{posted}] こちらは別の備品の話です",
                "account": {"account_id": 999999, "name": "別の人"},
            }
        ]
        chatwork.sent.clear()
        LetterpackFollower(make_config(tmp_path), chatwork, client=None).run_once()
        assert chatwork.sent == []

    def test_the_same_reply_is_not_relayed_twice(self, tmp_path):
        chatwork = FakeChatwork()
        _, posted = open_thread(tmp_path, chatwork)
        chatwork._messages = [staff_message(posted + 5, "用意しました", reply_to=posted)]
        chatwork.sent.clear()
        payload = {"kind": "完了", "summary": "用意いただきました。", "question": ""}
        for _ in range(2):
            LetterpackFollower(
                make_config(tmp_path), chatwork, client=fake_client(payload)
            ).run_once()
        assert len([r for r, _ in chatwork.sent if r == DEPT_ROOM]) == 1

    def test_classification_failure_still_relays_the_raw_reply(self, tmp_path):
        class BrokenClient:
            class messages:  # noqa: N801
                @staticmethod
                def create(**kwargs):
                    raise RuntimeError("API down")

        chatwork = FakeChatwork()
        _, posted = open_thread(tmp_path, chatwork)
        chatwork._messages = [staff_message(posted + 5, "用意しました", reply_to=posted)]
        chatwork.sent.clear()
        LetterpackFollower(make_config(tmp_path), chatwork, client=BrokenClient()).run_once()
        to_requester = next(b for r, b in chatwork.sent if r == DEPT_ROOM)
        assert "用意しました" in to_requester  # 判定できなくても伝える

    def test_an_abandoned_request_is_reported_not_dropped_silently(self, tmp_path):
        # 返信が来ないまま期限を過ぎたら、依頼者に伝えてから閉じる
        chatwork = FakeChatwork()
        open_thread(tmp_path, chatwork)
        config = make_config(tmp_path)
        config.data["letterpack"]["max_open_days"] = 0  # 即座に期限切れにする
        chatwork.sent.clear()
        LetterpackFollower(config, chatwork, client=None).run_once()
        assert len(chatwork.sent) == 1
        room_id, body = chatwork.sent[0]
        assert room_id == DEPT_ROOM
        assert "返信を確認できませんでした" in body


class TestRouting:
    """差出人の会社ごとに、備品の依頼先が変わること。"""

    def draft(self, tmp_path, chatwork, company_id, company_name):
        run = LetterpackRunner(make_config(tmp_path), chatwork)
        detail = {**DETAIL, "company": company_name, "company_id": company_id}
        run.offer(DEPT_ROOM, REQUESTER, 1000, detail)
        preview = run.handle(DEPT_ROOM, REQUESTER, 1100, "ライト2枚で")
        sent = run.handle(DEPT_ROOM, REQUESTER, 1200, "送信", display_name="足立 海里")
        return run, preview, sent

    def test_rakutenken_goes_to_its_own_room_and_people(self, tmp_path):
        chatwork = FakeChatwork()
        _, preview, sent = self.draft(
            tmp_path, chatwork, "RAKUTENKEN", "ＲＡＫＵＴＥＮＫＥＮ株式会社"
        )
        room_id, body = chatwork.sent[0]
        assert room_id == RAKUTEN_ROOM
        for account_id, name in RAKUTEN_STAFF:
            assert f"[To:{account_id}] {name}さん" in body
        assert f"[To:{STAFF_ID}]" not in body  # 総務あてには出さない
        # 中身はライズのときと同じ4項目
        assert "・使用会社名: ＲＡＫＵＴＥＮＫＥＮ株式会社" in body
        assert "・必要枚数: 2枚" in body
        assert "・種類: レターパックライト" in body
        assert f"#!rid{RAKUTEN_ROOM}-" in sent

    def test_the_preview_names_the_right_people(self, tmp_path):
        chatwork = FakeChatwork()
        _, preview, _ = self.draft(
            tmp_path, chatwork, "RAKUTENKEN", "ＲＡＫＵＴＥＮＫＥＮ株式会社"
        )
        assert "篠田 笑佳・福本 明日香・中浦 祐子・札葉 美早さんへ" in preview
        # 確認の段階では誰にも通知が飛ばない
        for account_id, _ in RAKUTEN_STAFF:
            assert f"[To:{account_id}]" not in preview

    def test_other_companies_still_go_to_soumu(self, tmp_path):
        for company_id, name in [
            ("ライズクリエイション", "株式会社ライズクリエイション"),
            ("ヤマトライジング", "株式会社ヤマトライジング"),
            ("", "株式会社ライズクリエイション"),
        ]:
            chatwork = FakeChatwork()
            self.draft(tmp_path / f"c{company_id}", chatwork, company_id, name)
            room_id, body = chatwork.sent[0]
            assert room_id == SUPPLIES_ROOM, company_id
            assert f"[To:{STAFF_ID}] 坂口 美代子さん" in body

    def test_a_reply_in_the_rakuten_room_is_relayed(self, tmp_path):
        chatwork = FakeChatwork()
        self.draft(tmp_path, chatwork, "RAKUTENKEN", "ＲＡＫＵＴＥＮＫＥＮ株式会社")
        posted = chatwork._next_id
        chatwork._messages = [
            {
                "message_id": str(posted + 5),
                "body": f"[rp aid={AGENT} to={RAKUTEN_ROOM}-{posted}] 用意しました。",
                "account": {"account_id": RAKUTEN_STAFF[1][0], "name": "福本　明日香 (休)土日祝"},
            }
        ]
        chatwork.sent.clear()
        LetterpackFollower(
            make_config(tmp_path), chatwork,
            client=fake_client({"kind": "完了", "summary": "2枚用意いただきました。", "question": ""}),
        ).run_once()
        rooms = [r for r, _ in chatwork.sent]
        assert DEPT_ROOM in rooms and RAKUTEN_ROOM in rooms
        assert SUPPLIES_ROOM not in rooms
        # 返してくれた本人の名前で伝え、お礼もその人へ返す
        to_requester = next(b for r, b in chatwork.sent if r == DEPT_ROOM)
        assert "福本 明日香さんから返信がありました" in to_requester
        thanks = next(b for r, b in chatwork.sent if r == RAKUTEN_ROOM)
        assert f"[To:{RAKUTEN_STAFF[1][0]}]" in thanks

    def test_a_reply_from_someone_we_did_not_ask_is_ignored(self, tmp_path):
        # 楽天軒ルームにいる別の人（依頼先に含まれない）の発言は拾わない
        chatwork = FakeChatwork()
        self.draft(tmp_path, chatwork, "RAKUTENKEN", "ＲＡＫＵＴＥＮＫＥＮ株式会社")
        posted = chatwork._next_id
        chatwork._messages = [
            {
                "message_id": str(posted + 5),
                "body": f"[rp aid={AGENT} to={RAKUTEN_ROOM}-{posted}] 横から失礼します",
                "account": {"account_id": 111111, "name": "別部署の人"},
            }
        ]
        chatwork.sent.clear()
        LetterpackFollower(make_config(tmp_path), chatwork, client=None).run_once()
        assert chatwork.sent == []

    def test_two_companies_in_flight_are_kept_apart(self, tmp_path):
        # 2社ぶん同時に進行しても、それぞれのルームだけを見る
        chatwork = FakeChatwork()
        run = LetterpackRunner(make_config(tmp_path), chatwork)
        for account, company_id, company in [
            (REQUESTER, "RAKUTENKEN", "ＲＡＫＵＴＥＮＫＥＮ株式会社"),
            (9129422, "ライズクリエイション", "株式会社ライズクリエイション"),
        ]:
            run.offer(DEPT_ROOM, account, 1000, {**DETAIL, "company": company, "company_id": company_id})
            run.handle(DEPT_ROOM, account, 1100, "ライト1枚で")
            run.handle(DEPT_ROOM, account, 1200, "送信", display_name="依頼者")
        assert sorted(r for r, _ in chatwork.sent) == sorted([RAKUTEN_ROOM, SUPPLIES_ROOM])

        polled = []
        chatwork.get_recent_messages = lambda room_id, limit=20: polled.append(room_id) or []
        chatwork.sent.clear()
        LetterpackFollower(make_config(tmp_path), chatwork, client=None).run_once()
        assert sorted(polled) == sorted([RAKUTEN_ROOM, SUPPLIES_ROOM])


class TestStore:
    def test_a_corrupt_state_file_does_not_break_the_flow(self, tmp_path):
        config = make_config(tmp_path)
        path = config.resolve_path(config.state_dir) / "letterpack.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{壊れている", encoding="utf-8")
        store = LetterpackStore(path)
        assert store.load() == {"offers": {}, "drafts": {}, "threads": {}}
