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

    def test_it_says_who_it_is_for_and_who_is_posting(self):
        # 総務から見て、アシスタントが足立さんの代理で出していると分かること
        text = build_request_text(DETAIL, 1, "レターパックプラス")
        assert "経理財務アシスタントです" in text
        assert "経理財務部の足立さんの依頼で" in text

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
    """総務へ投稿済みの状態を作る。"""
    run = LetterpackRunner(make_config(tmp_path), chatwork)
    run.offer(DEPT_ROOM, REQUESTER, 1000, DETAIL)
    run.handle(DEPT_ROOM, REQUESTER, 1100, "ライト2枚で")
    run.handle(DEPT_ROOM, REQUESTER, 1200, "送信", display_name="足立 海里")
    return run


def staff_message(message_id, body):
    return {
        "message_id": str(message_id),
        "body": body,
        "account": {"account_id": STAFF_ID, "name": "坂口 美代子"},
    }


class TestFollowUp:
    def test_completion_is_relayed_and_thanked(self, tmp_path):
        chatwork = FakeChatwork()
        open_thread(tmp_path, chatwork)
        posted_id = int(chatwork.sent and chatwork._next_id)
        chatwork._messages = [staff_message(posted_id + 5, "用意しました。総務の棚に置いています。")]
        chatwork.sent.clear()

        follower = LetterpackFollower(
            make_config(tmp_path),
            chatwork,
            client=fake_client(
                {"kind": "完了", "summary": "レターパックライト2枚を総務の棚に用意いただきました。", "question": ""}
            ),
        )
        follower.run_once()

        rooms = [room for room, _ in chatwork.sent]
        assert DEPT_ROOM in rooms and SUPPLIES_ROOM in rooms
        to_requester = next(b for r, b in chatwork.sent if r == DEPT_ROOM)
        assert f"[To:{REQUESTER}] 足立 海里さん" in to_requester
        assert "総務の棚に用意" in to_requester
        thanks = next(b for r, b in chatwork.sent if r == SUPPLIES_ROOM)
        assert "ありがとうございます" in thanks

    def test_a_question_is_relayed_and_the_answer_goes_back(self, tmp_path):
        chatwork = FakeChatwork()
        run = open_thread(tmp_path, chatwork)
        posted_id = chatwork._next_id
        chatwork._messages = [staff_message(posted_id + 5, "いつまでに必要でしょうか？")]
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

    def test_a_new_request_is_not_swallowed_as_an_answer(self, tmp_path):
        chatwork = FakeChatwork()
        run = open_thread(tmp_path, chatwork)
        chatwork._messages = [staff_message(chatwork._next_id + 5, "いつまでに必要ですか？")]
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
        open_thread(tmp_path, chatwork)
        chatwork._messages = [
            {
                "message_id": str(chatwork._next_id + 5),
                "body": "こちらは別の備品の話です",
                "account": {"account_id": 999999, "name": "別の人"},
            }
        ]
        chatwork.sent.clear()
        LetterpackFollower(make_config(tmp_path), chatwork, client=None).run_once()
        assert chatwork.sent == []

    def test_the_same_reply_is_not_relayed_twice(self, tmp_path):
        chatwork = FakeChatwork()
        open_thread(tmp_path, chatwork)
        chatwork._messages = [staff_message(chatwork._next_id + 5, "用意しました")]
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
        open_thread(tmp_path, chatwork)
        chatwork._messages = [staff_message(chatwork._next_id + 5, "用意しました")]
        chatwork.sent.clear()
        LetterpackFollower(make_config(tmp_path), chatwork, client=BrokenClient()).run_once()
        to_requester = next(b for r, b in chatwork.sent if r == DEPT_ROOM)
        assert "用意しました" in to_requester  # 判定できなくても伝える


class TestStore:
    def test_a_corrupt_state_file_does_not_break_the_flow(self, tmp_path):
        config = make_config(tmp_path)
        path = config.resolve_path(config.state_dir) / "letterpack.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{壊れている", encoding="utf-8")
        store = LetterpackStore(path)
        assert store.load() == {"offers": {}, "drafts": {}, "threads": {}}
