"""定型の受け答えの揺らぎ。

ここで守りたいのは2つ。
- 毎回同じ言い方にならないこと（機械が喋っている感じを減らす）
- どの言い方を選んでも意味が変わらないこと。呼び出し側が持っている判定
  （未来形を弾く／完成したと読める文面を弾く 等）を、辞書のすべての候補が
  満たすことをここで固定する
"""

import pathlib
import re

import pytest

from raizuinu.config import Config
from raizuinu.phrasing import BANKS, Phrasebook, build


class TestPicking:
    def test_the_same_phrase_does_not_repeat_back_to_back(self, tmp_path):
        book = Phrasebook(path=tmp_path / "p.json", rng=lambda n: 0)
        seen = [book.pick("doc_done") for _ in range(6)]
        assert all(a != b for a, b in zip(seen, seen[1:]))

    def test_every_phrase_gets_used_eventually(self, tmp_path):
        book = Phrasebook(path=tmp_path / "p.json", rng=lambda n: n - 1)
        seen = {book.pick("doc_done") for _ in range(40)}
        assert len(seen) >= 2

    def test_rooms_do_not_interfere(self, tmp_path):
        book = Phrasebook(path=tmp_path / "p.json", rng=lambda n: 0)
        first = book.pick("doc_done", scope="384793683")
        # 別ルームの直前の選択に引っ�張られない
        assert book.pick("doc_done", scope="444781726") == first

    def test_turning_it_off_gives_the_fixed_wording(self, tmp_path):
        book = Phrasebook(path=tmp_path / "p.json", enabled=False)
        assert {book.pick("doc_done") for _ in range(5)} == {BANKS["doc_done"][0]}

    def test_an_unknown_bank_is_an_error_not_a_blank(self, tmp_path):
        with pytest.raises(KeyError):
            Phrasebook(path=tmp_path / "p.json").pick("存在しない")

    def test_a_broken_state_file_does_not_stop_the_reply(self, tmp_path):
        path = tmp_path / "p.json"
        path.write_text("{壊れている", encoding="utf-8")
        assert Phrasebook(path=path).pick("doc_done") in BANKS["doc_done"]

    def test_the_config_switch_is_honoured(self, tmp_path):
        config = Config.load(tmp_path / "no-config.json")
        config.data["state_dir"] = str(tmp_path / "state")
        config.data["phrasing"] = {"vary_openings": False}
        config.base_dir = tmp_path
        assert {build(config).pick("doc_done") for _ in range(5)} == {BANKS["doc_done"][0]}


class TestMeaningIsPreserved:
    """揺らぎで意味が変わらないこと。呼び出し側の判定を全候補に当てる。"""

    def test_finished_document_wording_never_reads_as_a_request(self):
        from raizuinu.docbuild import _ASKING_OPENING_RE, _delivering_opening

        for phrase in BANKS["doc_done"]:
            assert not _ASKING_OPENING_RE.match(phrase)
            # そのまま渡しても差し替えられない＝完了の言い方として通る
            assert _delivering_opening(phrase) == phrase

    def test_ask_back_wording_never_claims_the_document_exists(self):
        from raizuinu.docbuild import _MADE_IT_RE, _asking_opening

        for key in ("doc_ask", "doc_kind_ask"):
            for phrase in BANKS[key]:
                assert not _MADE_IT_RE.search(phrase), phrase
                assert _asking_opening(phrase, "既定") == phrase

    def test_schedule_wording_never_reads_as_not_yet_done(self):
        from raizuinu.scheduletask import _ASKING_OPENING_RE, _FUTURE_OPENING_RE, _done_opening

        for phrase in BANKS["schedule_done"]:
            assert not _FUTURE_OPENING_RE.search(phrase), phrase
            assert not _ASKING_OPENING_RE.match(phrase), phrase
            assert _done_opening(phrase) == phrase

    def test_letterpack_declines_all_say_it_will_not_be_arranged(self):
        for phrase in BANKS["letterpack_declined"]:
            assert re.search(r"(行いません|見送り|なし)", phrase), phrase
        for phrase in BANKS["letterpack_cancelled"]:
            assert re.search(r"(取りやめ|止めて|送らず)", phrase), phrase
        for phrase in BANKS["letterpack_forwarded"]:
            # 「伝え終えた」と読めること（これからでは、依頼者が待ってしまう）
            assert re.search(r"(伝えました|伝えておきました|お伝えしました)", phrase), phrase

    def test_no_phrase_carries_a_number_a_name_or_a_source(self):
        # 揺らぐのは社交辞令だけ。値・固有名詞・出典は混ぜない
        for key, options in BANKS.items():
            for phrase in options:
                assert not re.search(r"\d", phrase), f"{key}: {phrase}"
                assert "http" not in phrase, f"{key}: {phrase}"
                assert "※" not in phrase, f"{key}: {phrase}"

    def test_every_bank_has_room_to_vary(self):
        for key, options in BANKS.items():
            assert len(options) >= 3, key
            assert len(set(options)) == len(options), key


class TestWhatMustNotVary:
    """重要な部分に揺らぎを持ち込んでいないこと。"""

    def test_fixed_guidance_texts_are_still_constants(self):
        from raizuinu.docbuild import CONTRACT_GUIDANCE
        from raizuinu.doctask import NO_DOCUMENT_GUIDANCE
        from raizuinu.guest import FALLBACK

        for text in (CONTRACT_GUIDANCE, NO_DOCUMENT_GUIDANCE, FALLBACK):
            assert isinstance(text, str) and text
            assert text not in [p for options in BANKS.values() for p in options]

    def test_the_request_sent_to_another_department_is_fixed(self):
        # 総務が読む文面は揃っていたほうがよい。揺らぎの対象にしない
        source = pathlib.Path("src/raizuinu/letterpack.py").read_text(encoding="utf-8")
        head, _, _ = source.partition("def room_link")
        assert "_phrasebook" not in head  # 依頼文の組み立て部分では使っていない

    def test_disclaimers_and_sources_are_not_in_any_bank(self):
        joined = " ".join(p for options in BANKS.values() for p in options)
        for forbidden in ("出典", "税理士", "弁護士", "一般的な会計知識", "ひな形"):
            assert forbidden not in joined


class TestEveryUserFacingPromptHasATone:
    """利用者に直接届く文を書かせるプロンプトは、必ず話し方を指示していること。

    指示が無い経路は機械的な返答になる。実例（2026-09-04）: 過去ログ照会の
    プロンプトに話し方が無く、パスワードだけを1行で返してしまった。
    """

    def prompts(self):
        from raizuinu.answer import SYSTEM_INSTRUCTIONS as qa
        from raizuinu.chatlog import LOOKUP_SYSTEM as chatlog
        from raizuinu.doctask import SYSTEM_PROMPT as doctask
        from raizuinu.guest import SYSTEM_PROMPT as guest

        return {"Q&A": qa, "過去ログ照会": chatlog, "文書タスク": doctask, "部外応対": guest}

    @pytest.mark.parametrize("name", ["Q&A", "過去ログ照会", "文書タスク", "部外応対"])
    def test_the_prompt_says_how_to_speak(self, name):
        prompt = self.prompts()[name]
        assert "です・ます" in prompt, name

    @pytest.mark.parametrize("name", ["Q&A", "過去ログ照会", "文書タスク", "部外応対"])
    def test_the_prompt_asks_for_variety(self, name):
        # 同じ型が続くと、機械が定型文を返しているように読める
        prompt = self.prompts()[name]
        assert re.search(r"(毎回.{0,8}変え|使い回さず|同じ.{0,10}にしない)", prompt), name

    def test_the_letterpack_request_stays_fixed(self):
        # 他部署が読む依頼文は、揺らがず揃っていたほうがよい
        from raizuinu.letterpack import build_request_text

        detail = {"company": "株式会社A", "to_lines": ["株式会社B"], "items": [], "staff": "足立"}
        assert build_request_text(detail, 1, "レターパックライト") == build_request_text(
            detail, 1, "レターパックライト"
        )
