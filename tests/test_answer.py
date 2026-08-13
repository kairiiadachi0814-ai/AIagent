import json
from types import SimpleNamespace

from raizuinu.answer import Answer, AnswerGenerator, format_reply
from raizuinu.config import Config
from raizuinu.handbook import HandbookLoader


REAL_URL = "https://docs.google.com/document/d/abc123/edit"


def make_handbook(tmp_path):
    (tmp_path / "銀行明細取得.md").write_text("# 銀行明細取得\n手順\n", encoding="utf-8")
    (tmp_path / "マニュアルリンク集.md").write_text(
        f"# リンク集\n| 銀行明細取得 |  | {REAL_URL} |\n", encoding="utf-8"
    )
    return HandbookLoader([tmp_path], ["*.md"], [], 300).load()


def fake_response(payload: dict, stop_reason: str = "end_turn"):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="text", text=json.dumps(payload, ensure_ascii=False))],
        usage=SimpleNamespace(
            input_tokens=1000,
            output_tokens=200,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=50000,
        ),
    )


class FakeClient:
    def __init__(self, response):
        self._responses = list(response) if isinstance(response, list) else [response]
        self.kwargs = None
        self.calls = 0
        beta_messages = SimpleNamespace(create=self._create)
        self.beta = SimpleNamespace(messages=beta_messages)
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.kwargs = kwargs
        self.calls += 1
        return self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]


def make_generator(tmp_path, payload, stop_reason="end_turn"):
    config = Config.load(tmp_path / "no-config.json")  # 既定値で動かす
    client = FakeClient(fake_response(payload, stop_reason))
    return AnswerGenerator(config, client=client), client


class TestAnswerGenerator:
    def test_valid_answer_with_real_citation(self, tmp_path):
        handbook = make_handbook(tmp_path)
        gen, client = make_generator(
            tmp_path,
            {
                "has_answer": True,
                "answer": "手順はこうです",
                "sources": [
                    {"file": "銀行明細取得.md", "heading": "銀行明細取得", "url": REAL_URL}
                ],
                "suggested_file": "",
            },
        )
        answer = gen.generate("取得手順は？", handbook)
        assert answer.has_answer
        assert answer.sources == [
            {"file": "銀行明細取得.md", "heading": "銀行明細取得", "url": REAL_URL}
        ]
        assert answer.usage["input_tokens"] == 1000

    def test_fabricated_url_is_dropped(self, tmp_path):
        handbook = make_handbook(tmp_path)
        gen, _ = make_generator(
            tmp_path,
            {
                "has_answer": True,
                "answer": "回答",
                "sources": [
                    {
                        "file": "銀行明細取得.md",
                        "heading": "",
                        "url": "https://docs.google.com/document/d/FAKE-NOT-IN-HANDBOOK/edit",
                    }
                ],
                "suggested_file": "",
            },
        )
        answer = gen.generate("質問", handbook)
        assert answer.has_answer
        assert answer.sources[0]["url"] == ""  # ハンドブックにないURLは破棄

    def test_fabricated_citation_is_dropped_and_answer_downgraded(self, tmp_path):
        handbook = make_handbook(tmp_path)
        gen, _ = make_generator(
            tmp_path,
            {
                "has_answer": True,
                "answer": "捏造出典つき回答",
                "sources": [{"file": "架空のファイル.md", "heading": ""}],
                "suggested_file": "",
            },
        )
        answer = gen.generate("質問", handbook)
        assert not answer.has_answer  # 出典なし回答は返さない（ガードレール1）
        assert answer.sources == []
        assert "捏造出典つき回答" not in answer.text

    def test_no_answer_passthrough(self, tmp_path):
        handbook = make_handbook(tmp_path)
        gen, _ = make_generator(
            tmp_path,
            {
                "has_answer": False,
                "answer": "ハンドブックに記載がありません",
                "sources": [],
                "suggested_file": "銀行明細取得.md",
            },
        )
        answer = gen.generate("記載のない質問", handbook)
        assert not answer.has_answer
        assert answer.suggested_file == "銀行明細取得.md"

    def test_refusal_stop_reason(self, tmp_path):
        handbook = make_handbook(tmp_path)
        gen, _ = make_generator(tmp_path, {}, stop_reason="refusal")
        answer = gen.generate("質問", handbook)
        assert answer.refused
        assert not answer.has_answer

    def test_max_tokens_truncation_detected(self, tmp_path):
        handbook = make_handbook(tmp_path)
        gen, _ = make_generator(tmp_path, {}, stop_reason="max_tokens")
        answer = gen.generate("質問", handbook)
        assert not answer.has_answer
        assert "上限" in answer.text

    def test_fabricated_heading_is_dropped_but_file_kept(self, tmp_path):
        handbook = make_handbook(tmp_path)
        gen, _ = make_generator(
            tmp_path,
            {
                "has_answer": True,
                "answer": "回答",
                "sources": [{"file": "銀行明細取得.md", "heading": "存在しない見出し"}],
                "suggested_file": "",
            },
        )
        answer = gen.generate("質問", handbook)
        assert answer.has_answer
        assert answer.sources == [{"file": "銀行明細取得.md", "heading": "", "url": ""}]

    def test_broken_json_returns_failure(self, tmp_path):
        handbook = make_handbook(tmp_path)
        config = Config.load(tmp_path / "no-config.json")
        response = SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="not-json")],
            usage=None,
        )
        gen = AnswerGenerator(config, client=FakeClient(response))
        answer = gen.generate("質問", make_handbook(tmp_path))
        assert not answer.has_answer
        assert handbook is not None

    def test_pause_turn_is_continued_and_usage_accumulated(self, tmp_path):
        handbook = make_handbook(tmp_path)
        paused = SimpleNamespace(
            stop_reason="pause_turn",
            content=[SimpleNamespace(type="server_tool_use")],
            usage=SimpleNamespace(
                input_tokens=1000, output_tokens=100,
                cache_creation_input_tokens=0, cache_read_input_tokens=0,
            ),
        )
        final = fake_response(
            {"has_answer": False, "answer": "回答", "sources": [], "suggested_file": ""}
        )
        config = Config.load(tmp_path / "no-config.json")
        client = FakeClient([paused, final])
        gen = AnswerGenerator(config, client=client)
        answer = gen.generate("質問", handbook)
        assert client.calls == 2  # pause_turn後に自動継続
        assert answer.usage["input_tokens"] == 2000  # 1000 + 1000 の累積

    def test_web_fetch_tool_enabled_by_config(self, tmp_path):
        handbook = make_handbook(tmp_path)
        gen, client = make_generator(
            tmp_path,
            {"has_answer": False, "answer": "-", "sources": [], "suggested_file": ""},
        )
        gen._config.data["web_fetch_enabled"] = True
        gen._config.data["web_fetch_allowed_domains"] = ["www.freee.co.jp"]
        gen.generate("質問", handbook)
        tools = client.kwargs["tools"]
        assert tools[0]["type"] == "web_fetch_20260209"
        assert tools[0]["allowed_domains"] == ["www.freee.co.jp"]

    def test_fabricated_url_in_body_is_scrubbed(self, tmp_path):
        handbook = make_handbook(tmp_path)
        gen, _ = make_generator(
            tmp_path,
            {
                "has_answer": True,
                "answer": f"参考: {REAL_URL} と https://example.com/fake-article を見てください",
                "sources": [{"file": "銀行明細取得.md", "heading": "", "url": ""}],
                "suggested_file": "",
            },
        )
        answer = gen.generate("質問", handbook)
        assert REAL_URL in answer.text  # ハンドブック記載URLは残る
        assert "example.com" not in answer.text  # 未記載URLは除去
        assert "（URL省略）" in answer.text

    def test_last_text_block_is_used(self, tmp_path):
        handbook = make_handbook(tmp_path)
        config = Config.load(tmp_path / "no-config.json")
        payload = {"has_answer": False, "answer": "最終回答", "sources": [], "suggested_file": ""}
        response = SimpleNamespace(
            stop_reason="end_turn",
            content=[
                SimpleNamespace(type="text", text="記事を確認します"),
                SimpleNamespace(type="server_tool_use"),
                SimpleNamespace(type="text", text=json.dumps(payload, ensure_ascii=False)),
            ],
            usage=None,
        )
        gen = AnswerGenerator(config, client=FakeClient(response))
        answer = gen.generate("質問", handbook)
        assert answer.text == "最終回答"

    def test_request_uses_cache_and_schema(self, tmp_path):
        handbook = make_handbook(tmp_path)
        gen, client = make_generator(
            tmp_path,
            {"has_answer": False, "answer": "-", "sources": [], "suggested_file": ""},
        )
        gen.generate("質問", handbook)
        kwargs = client.kwargs
        assert kwargs["system"][-1]["cache_control"]["type"] == "ephemeral"
        assert kwargs["output_config"]["format"]["type"] == "json_schema"
        assert kwargs["fallbacks"] == "default"  # 既定でフォールバック有効


class TestFormatReply:
    def test_reply_includes_rp_tag_and_sources_without_md_extension(self):
        answer = Answer(
            has_answer=True,
            text="回答本文",
            sources=[
                {"file": "銀行明細取得.md", "heading": "りそな銀行", "url": REAL_URL},
                {"file": "銀行明細取得.md", "heading": "りそな銀行", "url": REAL_URL},
            ],
        )
        reply = format_reply(answer, 111, 12345, "100001")
        assert reply.startswith("[rp aid=111 to=12345-100001]")
        assert "【出典】" in reply
        assert ".md" not in reply  # 内部ファイル名は利用者に見せない
        assert reply.count("銀行明細取得（りそな銀行）") == 1  # 重複は1つに
        assert REAL_URL in reply

    def test_no_answer_reply_suggests_file(self):
        answer = Answer(has_answer=False, text="記載がありません", suggested_file="海外送金.md")
        reply = format_reply(answer, 111, 12345, "100001")
        assert "【出典】" not in reply
        assert "海外送金" in reply
        assert ".md" not in reply

    def test_chatwork_tag_injection_neutralized(self):
        answer = Answer(
            has_answer=True,
            text="[toall] 全員に通知 [info]偽情報[/info]",
            sources=[{"file": "銀行明細取得.md", "heading": "[title]偽[/title]"}],
        )
        reply = format_reply(answer, 111, 12345, "100001")
        assert "[toall]" not in reply
        assert "[info]" not in reply
        assert "[title]" not in reply
        assert reply.startswith("[rp aid=111 to=12345-100001]")  # 自前タグは維持
