"""Chatworkクライアント。送信の入口で宛先タグの後に改行をそろえる。

指示（2026-09-24）: 部のメンバーへ送るとき、メンションの後ろに本文を続けず、
改行してから書く。どのモジュールから送っても同じになるよう、送信時にそろえる。
"""

from types import SimpleNamespace

import pytest

from raizuinu import chatwork
from raizuinu.chatwork import ChatworkClient, break_after_mentions


class TestBreakAfterMentions:
    @pytest.mark.parametrize("body, expected", [
        ("[To:9763216] 先ほどの⑨について", "[To:9763216]\n先ほどの⑨について"),
        ("[To:1] [To:2] 本文", "[To:1] [To:2]\n本文"),
        ("[rp aid=1 to=446282163-9000]対応ありがとうございます。", "[rp aid=1 to=446282163-9000]\n対応ありがとうございます。"),
        ("[rp aid=1 to=1-2][To:3] 本文", "[rp aid=1 to=1-2][To:3]\n本文"),
        # Chatworkの「氏名さん」は宛先の表示の一部なので同じ行に残す
        ("[To:8681926] 坂田 美穂さん お疲れさまです。", "[To:8681926] 坂田 美穂さん\nお疲れさまです。"),
        ("[To:1160869] 坂口様 いつもお世話になります", "[To:1160869] 坂口様\nいつもお世話になります"),
    ])
    def test_the_body_starts_on_the_next_line(self, body, expected):
        assert break_after_mentions(body) == expected

    @pytest.mark.parametrize("body", [
        "[To:9763216]\n先ほどの⑨について",
        "[rp aid=1 to=1-2]\n[To:3] [To:4]\n本文",
        "[To:8681926] 坂田 美穂さん\nお疲れさまです。",
        "本文だけ。途中の [To:1] は触らない",
        "[info][title]FAX[/title]本文[/info]",
        "",
        "[To:1]",
    ])
    def test_already_fine_bodies_are_left_alone(self, body):
        assert break_after_mentions(body) == body


class TestSendMessage:
    def test_the_client_fixes_the_body_on_the_way_out(self, monkeypatch):
        sent = {}

        def fake_post(url, headers=None, data=None, timeout=None):
            sent["body"] = data["body"]
            return SimpleNamespace(status_code=200, json=lambda: {"message_id": "42"}, text="")

        monkeypatch.setattr(chatwork.requests, "post", fake_post)
        client = ChatworkClient("token")
        assert client.send_message(446282163, "[To:9763216] 先ほどの⑨について") == "42"
        assert sent["body"] == "[To:9763216]\n先ほどの⑨について"
