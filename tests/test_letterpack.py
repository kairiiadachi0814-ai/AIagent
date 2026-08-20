import json
from types import SimpleNamespace

import pytest

from raizuinu.config import Config
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
        "supplies_room_id": SUPPLIES_ROOM,
        "staff_account_id": STAFF_ID,
        "staff_name": "坂口 美代子",
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
        assert "手配は行いません" in reply
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
        assert "取りやめます" in reply
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
        assert SUPPLIES_ROOM not in [r for r, _ in chatwork.sent]  # 勝手に答えない

        chatwork.sent.clear()
        reply = run.handle(DEPT_ROOM, REQUESTER, 99999999, "明後日までにお願いします")
        assert "総務へお伝えしました" in reply
        room_id, body = chatwork.sent[0]
        assert room_id == SUPPLIES_ROOM
        assert "明後日までにお願いします" in body

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


class TestStore:
    def test_a_corrupt_state_file_does_not_break_the_flow(self, tmp_path):
        config = make_config(tmp_path)
        path = config.resolve_path(config.state_dir) / "letterpack.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{壊れている", encoding="utf-8")
        store = LetterpackStore(path)
        assert store.load() == {"offers": {}, "drafts": {}, "threads": {}}
