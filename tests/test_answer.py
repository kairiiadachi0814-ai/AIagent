import json
from types import SimpleNamespace

from raizuinu.answer import Answer, AnswerGenerator, format_reply
from raizuinu.config import Config
from raizuinu.handbook import HandbookLoader


REAL_URL = "https://docs.google.com/document/d/abc123/edit"
FREEE_URL = "https://www.freee.co.jp/kb/kb-accounting/entertainment-expenses/"
MIRASAPO_URL = "https://mirasapo-plus.go.jp/subsidy/ithojo/"


def make_handbook(tmp_path):
    (tmp_path / "銀行明細取得.md").write_text("# 銀行明細取得\n手順\n", encoding="utf-8")
    (tmp_path / "マニュアルリンク集.md").write_text(
        f"# リンク集\n| 銀行明細取得 |  | {REAL_URL} |\n", encoding="utf-8"
    )
    (tmp_path / "会計基礎知識リンク集_freee.md").write_text(
        f"# 会計リンク集\n| 交際費 | {FREEE_URL} |\n", encoding="utf-8"
    )
    (tmp_path / "補助金リンク集_ミラサポplus.md").write_text(
        f"# 補助金リンク集\n- デジタル化・AI導入補助金: {MIRASAPO_URL}\n", encoding="utf-8"
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
            {"file": "銀行明細取得.md", "heading": "銀行明細取得", "url": REAL_URL,
             "title": "銀行明細取得"}
        ]
        assert answer.usage["input_tokens"] == 1000

    def test_missing_source_url_is_filled_from_link_collection(self, tmp_path):
        # モデルがurlを埋め忘れても、リンク集から機械的に補完する
        (tmp_path / "Amazonモール売上明細取得.md").write_text(
            "# Amazonモール売上明細取得\n手順\n", encoding="utf-8"
        )
        amazon_url = "https://docs.google.com/document/d/AMAZON-DOC/edit"
        (tmp_path / "マニュアルリンク集.md").write_text(
            f"# リンク集\n| 銀行明細取得 |  | {REAL_URL} |\n"
            f"| Amazon モール売上明細取得 | リモートのため注意 | {amazon_url} |\n",
            encoding="utf-8",
        )
        handbook = HandbookLoader([tmp_path], ["*.md"], [], 300).load()
        gen, _ = make_generator(
            tmp_path,
            {
                "has_answer": True,
                "answer": "手順はこうです",
                "sources": [{"file": "Amazonモール売上明細取得.md", "heading": "", "url": ""}],
                "suggested_file": "",
            },
        )
        answer = gen.generate("Amazonの売上データ取得は？", handbook)
        assert answer.sources[0]["url"] == amazon_url  # 表記ゆれ（半角空白）を吸収
        assert amazon_url in format_reply(answer, 1, 2, "3")

    def test_source_url_prefers_own_header_and_rejects_other_manuals_url(self, tmp_path):
        # 各マニュアル冒頭の「- 出典：」URLを最優先で使い、
        # 別マニュアルのURLをモデルが挙げても採用しない（原本の取り違え防止）
        url_a = "https://docs.google.com/document/d/AAAAAAAAAAAA/edit"
        url_b = "https://docs.google.com/document/d/BBBBBBBBBBBB/edit"
        (tmp_path / "給与振込処理_ライズ.md").write_text(
            f"# 給与振込処理（ライズ）\n\n- 出典：Googleドキュメント「給与振込処理（ライズ）」\n  {url_a}\n- 転記日：2026-07-17\n",
            encoding="utf-8",
        )
        (tmp_path / "給与振込処理_楽天軒.md").write_text(
            f"# 給与振込処理（楽天軒）\n\n- 出典：Googleドキュメント「給与振込処理（楽天軒）」\n  {url_b}\n",
            encoding="utf-8",
        )
        handbook = HandbookLoader([tmp_path], ["*.md"], [], 300).load()
        gen, _ = make_generator(
            tmp_path,
            {
                "has_answer": True,
                "answer": "手順はこうです",
                # 楽天軒のURLを誤って添えたケース（リンク集の行ずれ相当）
                "sources": [{"file": "給与振込処理_ライズ.md", "heading": "", "url": url_b}],
                "suggested_file": "",
            },
        )
        answer = gen.generate("ライズの給与振込は？", handbook)
        assert answer.sources[0]["url"] == url_a  # 自分自身の原本URLに直る

    def test_source_url_not_taken_from_template_or_article_collections(self, tmp_path):
        # ひな形・freee等のリンク集は社内マニュアルの原本ではないため補完に使わない
        from raizuinu.answer import _lookup_manual_url

        (tmp_path / "経費精算書作成.md").write_text("# 経費精算書作成\n手順\n", encoding="utf-8")
        (tmp_path / "ひな形リンク集_Box.md").write_text(
            "# ひな形\n- 経費精算書: https://app.box.com/file/123\n", encoding="utf-8"
        )
        (tmp_path / "会計基礎知識リンク集_freee.md").write_text(
            "# 会計\n| 経費精算書作成の基礎 | https://www.freee.co.jp/kb/kb-accounting/x/ |\n",
            encoding="utf-8",
        )
        handbook = HandbookLoader([tmp_path], ["*.md"], [], 300).load()
        assert _lookup_manual_url("経費精算書作成.md", handbook) == ""

    def test_source_url_lookup_does_not_guess_when_ambiguous(self, tmp_path):
        # 候補が絞れない場合はURLを付けない（誤った原本へ誘導しない）
        from raizuinu.answer import _lookup_manual_url

        (tmp_path / "給与振込処理.md").write_text("# 給与振込処理\n手順\n", encoding="utf-8")
        (tmp_path / "マニュアルリンク集.md").write_text(
            "# リンク集\n"
            "| 給与振込処理 ライズ | | https://docs.google.com/document/d/A/edit |\n"
            "| 給与振込処理 楽天軒 | | https://docs.google.com/document/d/B/edit |\n",
            encoding="utf-8",
        )
        handbook = HandbookLoader([tmp_path], ["*.md"], [], 300).load()
        assert _lookup_manual_url("給与振込処理.md", handbook) == ""

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
        # 捏造URLは破棄され、リンク集の正しい原本URLに置き換わる
        assert "FAKE-NOT-IN-HANDBOOK" not in answer.sources[0]["url"]
        assert answer.sources[0]["url"] == REAL_URL

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
        # 実在しない見出しは落とし、原本URLはリンク集から補完される
        assert answer.sources == [
            {"file": "銀行明細取得.md", "heading": "", "url": REAL_URL,
             "title": "銀行明細取得"}
        ]

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

    def _general_knowledge_stage1(self):
        return {
            "intent": "general_knowledge",
            "reference_url": FREEE_URL,
            "has_answer": False,
            "answer": f"社内ハンドブックには記載がありません。参考記事: {FREEE_URL}",
            "sources": [],
            "suggested_file": "",
            "reported_manuals": [],
        }

    @staticmethod
    def _stage2_response(text):
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(
                input_tokens=5000, output_tokens=200,
                cache_creation_input_tokens=0, cache_read_input_tokens=0,
            ),
        )

    def test_general_knowledge_two_stage_summary(self, tmp_path):
        handbook = make_handbook(tmp_path)
        config = Config.load(tmp_path / "no-config.json")
        config.data["web_fetch_enabled"] = True
        config.data["web_fetch_allowed_domains"] = ["www.freee.co.jp"]
        summary = "限度額は年800万円です。※社外の一般解説（freee会計）に基づく情報です。"
        client = FakeClient(
            [fake_response(self._general_knowledge_stage1()), self._stage2_response(summary)]
        )
        gen = AnswerGenerator(config, client=client)
        answer = gen.generate("接待交際費の限度は？", handbook)
        assert client.calls == 2
        assert answer.has_answer
        assert "800万円" in answer.text
        assert answer.sources == [
            {"file": "会計基礎知識リンク集_freee.md", "heading": "", "url": FREEE_URL}
        ]
        # 2段目にはweb_fetchツールが付き、URLがユーザーメッセージに入る
        assert client.kwargs["tools"][0]["type"] == "web_fetch_20260209"
        assert FREEE_URL in client.kwargs["messages"][0]["content"]
        assert answer.usage["input_tokens"] == 6000  # 両段の累積

    def test_subsidy_two_stage_summary_via_mirasapo(self, tmp_path):
        handbook = make_handbook(tmp_path)
        config = Config.load(tmp_path / "no-config.json")
        config.data["web_fetch_enabled"] = True
        config.data["web_fetch_allowed_domains"] = ["www.freee.co.jp", "mirasapo-plus.go.jp"]
        stage1 = {
            "intent": "general_knowledge",
            "reference_url": MIRASAPO_URL,
            "has_answer": False,
            "answer": f"社内ハンドブックには記載がありません。参考: {MIRASAPO_URL}",
            "sources": [],
            "suggested_file": "",
            "reported_manuals": [],
        }
        summary = "ITツール導入を支援する補助金です。※ミラサポplus（経済産業省 中小企業庁）の掲載情報に基づきます。"
        client = FakeClient([fake_response(stage1), self._stage2_response(summary)])
        gen = AnswerGenerator(config, client=client)
        answer = gen.generate("IT導入補助金って何？", handbook)
        assert client.calls == 2  # ミラサポplusドメインも2段目の取得対象
        assert answer.has_answer
        assert "ITツール" in answer.text
        assert answer.sources == [
            {"file": "補助金リンク集_ミラサポplus.md", "heading": "", "url": MIRASAPO_URL}
        ]
        # 2段目のシステム指示には補助金向け免責文（公募要領の確認）が入る
        assert "公募要領" in client.kwargs["system"]
        assert "税理士" not in client.kwargs["system"]

    def test_disclaimer_selection_by_domain(self):
        from raizuinu.answer import _disclaimer_for_url

        assert "公募要領" in _disclaimer_for_url(MIRASAPO_URL)
        assert "税理士" in _disclaimer_for_url(FREEE_URL)
        assert "税理士" in _disclaimer_for_url("")  # 不明時は従来の免責文
        # 偽装ドメインはミラサポ扱いにしない
        assert "税理士" in _disclaimer_for_url("https://mirasapo-plus.go.jp.evil.com/x/")

    def test_subsidy_stage2_prompt_requires_acceptance_status(self, tmp_path):
        handbook = make_handbook(tmp_path)
        config = Config.load(tmp_path / "no-config.json")
        config.data["web_fetch_enabled"] = True
        config.data["web_fetch_allowed_domains"] = ["mirasapo-plus.go.jp"]
        stage1 = {
            "intent": "general_knowledge",
            "reference_url": MIRASAPO_URL,
            "has_answer": False,
            "answer": f"参考: {MIRASAPO_URL}",
            "sources": [],
            "suggested_file": "",
            "reported_manuals": [],
        }
        client = FakeClient([fake_response(stage1), self._stage2_response("要約です。")])
        gen = AnswerGenerator(config, client=client)
        answer = gen.generate("IT導入補助金の上限額は？", handbook)
        # 補助金ページでは受付状況の言及を必須にし、行数上限も2〜8行へ緩和
        assert "受付状況" in client.kwargs["system"]
        assert "2〜8行" in client.kwargs["system"]
        assert answer.stage2 == "ok"

    def test_stage2_status_recorded_on_fetch_failure(self, tmp_path):
        handbook = make_handbook(tmp_path)
        config = Config.load(tmp_path / "no-config.json")
        config.data["web_fetch_enabled"] = True
        config.data["web_fetch_allowed_domains"] = ["www.freee.co.jp"]
        client = FakeClient(
            [fake_response(self._general_knowledge_stage1()), self._stage2_response("FETCH_FAILED")]
        )
        gen = AnswerGenerator(config, client=client)
        answer = gen.generate("接待交際費の限度は？", handbook)
        assert answer.stage2 == "fetch_failed"  # 監査ログでリンク切れを検知できる

    def test_chat_intent_allows_answer_without_sources(self, tmp_path):
        handbook = make_handbook(tmp_path)
        gen, _ = make_generator(
            tmp_path,
            {
                "intent": "chat",
                "has_answer": True,
                "answer": "おはようございます！今日もよろしくお願いします。",
                "sources": [],
                "suggested_file": "",
                "reference_url": "",
                "reported_manuals": [],
            },
        )
        answer = gen.generate("おはよう！", handbook)
        assert answer.has_answer  # 雑談は出典なしで成立する
        assert answer.intent == "chat"
        reply = format_reply(answer, 1, 2, "3")
        assert "【出典】" not in reply

    def test_task_intent_allows_deliverable_without_sources(self, tmp_path):
        handbook = make_handbook(tmp_path)
        gen, _ = make_generator(
            tmp_path,
            {
                "intent": "task",
                "has_answer": True,
                "answer": "件名: 締め日変更のお知らせ\n本文: 皆さま…",
                "sources": [],
                "suggested_file": "",
                "reference_url": "",
                "reported_manuals": [],
            },
        )
        answer = gen.generate("周知文を作って", handbook)
        assert answer.has_answer
        assert "締め日変更のお知らせ" in answer.text

    def test_question_without_sources_still_downgraded(self, tmp_path):
        # 社内手順の質問（question）は従来どおり出典必須のまま
        handbook = make_handbook(tmp_path)
        gen, _ = make_generator(
            tmp_path,
            {
                "intent": "question",
                "has_answer": True,
                "answer": "出典なしの回答",
                "sources": [],
                "suggested_file": "",
                "reference_url": "",
                "reported_manuals": [],
            },
        )
        answer = gen.generate("手順は？", handbook)
        assert not answer.has_answer

    def test_general_knowledge_direct_answer_requires_disclaimer(self, tmp_path):
        handbook = make_handbook(tmp_path)
        base = {
            "intent": "general_knowledge",
            "has_answer": True,
            "sources": [],
            "suggested_file": "",
            "reference_url": "",
            "reported_manuals": [],
        }
        # 規定の免責文つきの概念回答は許可
        gen, _ = make_generator(
            tmp_path,
            {**base, "answer": "借方は左側の記録です。※一般的な会計知識としての説明です。税理士にご確認ください。"},
        )
        answer = gen.generate("借方って何？", handbook)
        assert answer.has_answer
        # 免責文なしは従来どおり降格（社内ルールとの混同防止）
        gen2, _ = make_generator(tmp_path, {**base, "answer": "借方は左側の記録です。"})
        answer2 = gen2.generate("借方って何？", handbook)
        assert not answer2.has_answer
        # ※があっても規定の免責文でなければ降格（1文字チェックの悪用防止）
        gen3, _ = make_generator(
            tmp_path,
            {**base, "answer": "源泉徴収税率は10.21%です。※復興特別所得税を含みます。"},
        )
        answer3 = gen3.generate("源泉の税率は？", handbook)
        assert not answer3.has_answer
        # 補助金の概念質問には制度知識の免責文でも許可
        gen4, _ = make_generator(
            tmp_path,
            {**base, "answer": "補助金は返済不要の支援金です。※一般的な制度知識としての説明です。公募要領でご確認ください。"},
        )
        answer4 = gen4.generate("補助金と融資の違いは？", handbook)
        assert answer4.has_answer

    def test_prompt_contains_disclaimer_markers(self):
        # プロンプトの免責文と検証マーカーのドリフト防止
        from raizuinu.answer import SYSTEM_INSTRUCTIONS, _GENERAL_DISCLAIMER_MARKERS

        for marker in _GENERAL_DISCLAIMER_MARKERS:
            assert marker in SYSTEM_INSTRUCTIONS

    def test_task_with_fabricated_sources_is_downgraded(self, tmp_path):
        # 出典を主張したのに全て検証落ち → ハンドブック準拠を装った成果物は出さない
        handbook = make_handbook(tmp_path)
        gen, _ = make_generator(
            tmp_path,
            {
                "intent": "task",
                "has_answer": True,
                "answer": "周知文: 締め日は毎月25日です",
                "sources": [{"file": "実在しないルール.md", "heading": "", "url": ""}],
                "suggested_file": "",
                "reference_url": "",
                "reported_manuals": [],
            },
        )
        answer = gen.generate("締め日の周知文を作って", handbook)
        assert not answer.has_answer

    def test_task_without_sources_gets_no_handbook_note(self, tmp_path):
        # 出典なしの純作業成果物には「ハンドブック根拠ではない」注記が自動で付く
        handbook = make_handbook(tmp_path)
        gen, _ = make_generator(
            tmp_path,
            {
                "intent": "task",
                "has_answer": True,
                "answer": "1,000円×12か月=12,000円です",
                "sources": [],
                "suggested_file": "",
                "reference_url": "",
                "reported_manuals": [],
            },
        )
        answer = gen.generate("計算して", handbook)
        assert answer.has_answer
        assert "ハンドブックの記載を根拠にした回答ではありません" in answer.text

    def test_task_can_echo_user_provided_url(self, tmp_path):
        # 利用者が依頼文に貼ったURLは成果物への復唱を許す（新規URLの創作は従来どおり除去）
        handbook = make_handbook(tmp_path)
        user_url = "https://example.com/campaign/2026"
        gen, _ = make_generator(
            tmp_path,
            {
                "intent": "task",
                "has_answer": True,
                "answer": f"案内文: 詳細は {user_url} をご覧ください。",
                "sources": [],
                "suggested_file": "",
                "reference_url": "",
                "reported_manuals": [],
            },
        )
        answer = gen.generate(f"この案内文を丁寧にして {user_url}", handbook)
        assert user_url in answer.text

    def test_markdown_is_converted_for_chatwork(self):
        from raizuinu.answer import sanitize_for_chatwork

        text = (
            "- **圧縮できる限度額**：補助金8.6億円が上限です。\n"
            "  - `直接減額方式` の場合\n"
            "* やり方：__費用計上__します\n"
            "## 注意点\n"
            "マイナス表記の -100円 はそのまま。\n"
            "-123円が行頭でも空白が続かなければ触らない"
        )
        result = sanitize_for_chatwork(text)
        assert "・圧縮できる限度額：補助金8.6億円が上限です。" in result
        assert "  ・直接減額方式 の場合" in result
        assert "・やり方：費用計上します" in result
        assert "注意点" in result and "##" not in result
        assert "**" not in result
        assert "-100円" in result  # 行頭以外は触らない
        assert "-123円が行頭でも" in result  # 記号の直後に空白が無ければ箇条書きとみなさない

    def test_underscore_urls_not_broken_by_markdown_strip(self):
        from raizuinu.answer import sanitize_for_chatwork

        # ハンドブック実在のGoogle Docs URL（IDに__を含む）。同一行に2つ並んでも壊さない
        u1 = "https://docs.google.com/document/d/1NhwGLpX1QkSyO5QTt__8QgfGoonD0aMvmpj9MCoOx7Y/edit?tab=t.0"
        u2 = "https://docs.google.com/document/d/1yni68-r7QaGL8__KIgem-a6eciShRxsfpDjf-23Zgs0/edit"
        text = f"・明細: {u1}\n・給与: {u2}\n同一行: {u1} と {u2} を参照。__強調__も除去。"
        result = sanitize_for_chatwork(text)
        assert result.count(u1) == 2
        assert result.count(u2) == 2
        assert "__" not in result.replace(u1, "").replace(u2, "")  # URL外の__記法は除去

    def test_markdown_wrapped_url_survives_scrub(self, tmp_path):
        handbook = make_handbook(tmp_path)
        gen, _ = make_generator(
            tmp_path,
            {
                "has_answer": True,
                "answer": f"詳しくは **{REAL_URL}** をご覧ください",
                "sources": [{"file": "銀行明細取得.md", "heading": "", "url": ""}],
                "suggested_file": "",
            },
        )
        answer = gen.generate("質問", handbook)
        assert REAL_URL in answer.text  # **で囲まれても検証済みURLは（URL省略）にしない
        reply = format_reply(answer, 1, 2, "3")
        assert REAL_URL in reply
        assert "**" not in reply  # 囲みの**は最終出力までに除去される

    def test_format_reply_output_has_no_markdown(self, tmp_path):
        answer = Answer(
            has_answer=True,
            text="- **要点**：こうです",
            sources=[{"file": "銀行明細取得.md", "heading": "", "url": ""}],
        )
        reply = format_reply(answer, 111, 222, "333")
        assert "・要点：こうです" in reply
        assert "**" not in reply

    def test_fabricated_mirasapo_url_in_body_is_scrubbed(self, tmp_path):
        handbook = make_handbook(tmp_path)
        fake_url = "https://mirasapo-plus.go.jp/subsidy/not-a-real-page/"
        gen, _ = make_generator(
            tmp_path,
            {
                "has_answer": True,
                "answer": f"詳しくは {fake_url} をご覧ください",
                "sources": [{"file": "銀行明細取得.md", "heading": "", "url": ""}],
                "suggested_file": "",
            },
        )
        answer = gen.generate("質問", handbook)
        # リンク集に無いミラサポURLの創作は許可しない（完全一致検証のみ）
        assert fake_url not in answer.text
        assert "（URL省略）" in answer.text

    def test_reference_url_recovered_from_body_when_field_empty(self, tmp_path):
        # 実測の不具合: 参考URLをreference_urlに入れず本文にだけ書くと
        # ページ取得が飛ばされ、記憶に頼った薄い回答になっていた
        handbook = make_handbook(tmp_path)
        config = Config.load(tmp_path / "no-config.json")
        config.data["web_fetch_enabled"] = True
        config.data["web_fetch_allowed_domains"] = ["mirasapo-plus.go.jp"]
        stage1 = {
            "intent": "general_knowledge",
            "reference_url": "",  # フィールドは空
            "has_answer": True,
            "answer": f"補助率は公募回で変わります。参考: {MIRASAPO_URL}\n※一般的な制度知識としての説明です。",
            "sources": [],
            "suggested_file": "",
            "reported_manuals": [],
        }
        summary = "補助率は1/2〜2/3、上限4,000万円です。※ミラサポplus（経済産業省 中小企業庁）の掲載情報に基づきます。"
        client = FakeClient([fake_response(stage1), self._stage2_response(summary)])
        gen = AnswerGenerator(config, client=client)
        answer = gen.generate("ものづくり補助金の補助率と上限額は？", handbook)
        assert client.calls == 2  # 本文のURLを拾って2段目を実行する
        assert "4,000万円" in answer.text
        assert answer.stage2 == "ok"
        assert answer.reference_url == MIRASAPO_URL

    def test_body_url_recovery_ignores_unfetchable_urls(self, tmp_path):
        # ハンドブックに無いURLや許可外ドメインは拾わない（捏造URLでの取得防止）
        handbook = make_handbook(tmp_path)
        config = Config.load(tmp_path / "no-config.json")
        config.data["web_fetch_enabled"] = True
        config.data["web_fetch_allowed_domains"] = ["mirasapo-plus.go.jp"]
        stage1 = {
            "intent": "general_knowledge",
            "reference_url": "",
            "has_answer": True,
            # ハンドブック未記載のミラサポURLと、許可外ドメインの実在URL
            "answer": f"参考: https://mirasapo-plus.go.jp/subsidy/not-real/ と {REAL_URL}\n※一般的な制度知識としての説明です。",
            "sources": [],
            "suggested_file": "",
            "reported_manuals": [],
        }
        client = FakeClient([fake_response(stage1)])
        gen = AnswerGenerator(config, client=client)
        answer = gen.generate("補助金について", handbook)
        assert client.calls == 1  # 2段目に進まない
        assert answer.stage2 == ""

    def test_program_name_lookup_when_no_url_anywhere(self, tmp_path):
        # reference_urlも本文URLも無い場合、制度名からリンク集を引いて必ず取得へ回す
        handbook = make_handbook(tmp_path)
        config = Config.load(tmp_path / "no-config.json")
        config.data["web_fetch_enabled"] = True
        config.data["web_fetch_allowed_domains"] = ["mirasapo-plus.go.jp"]
        stage1 = {
            "intent": "general_knowledge",
            "reference_url": "",
            "has_answer": True,
            "answer": "補助率は公募回によって変わります。※一般的な制度知識としての説明です。",
            "sources": [],
            "suggested_file": "",
            "reported_manuals": [],
        }
        client = FakeClient(
            [fake_response(stage1), self._stage2_response("上限4,000万円です。※")]
        )
        gen = AnswerGenerator(config, client=client)
        answer = gen.generate("デジタル化・AI導入補助金の上限額は？", handbook)
        assert client.calls == 2
        assert answer.reference_url == MIRASAPO_URL
        assert "4,000万円" in answer.text

    def test_program_lookup_skipped_for_generic_or_ambiguous_questions(self, tmp_path):
        from raizuinu.answer import _lookup_program_url

        (tmp_path / "補助金リンク集_ミラサポplus.md").write_text(
            "# 補助金リンク集\n"
            "- ものづくり補助金（設備投資）: https://mirasapo-plus.go.jp/subsidy/manufacturing/\n"
            "- 省力化投資補助金（省力化）: https://mirasapo-plus.go.jp/subsidy/shoryokuka/\n",
            encoding="utf-8",
        )
        handbook = HandbookLoader([tmp_path], ["*.md"], [], 300).load()
        hosts = {"mirasapo-plus.go.jp"}
        # 制度名が特定できる質問だけ引く
        assert _lookup_program_url("ものづくり補助金の上限は？", handbook, hosts).endswith(
            "/manufacturing/"
        )
        # 総覧的な質問・該当なしは引かない（的外れなページを読ませない）
        assert _lookup_program_url("補助金について教えて", handbook, hosts) == ""
        assert _lookup_program_url("キャリアアップ助成金の要件は？", handbook, hosts) == ""
        assert _lookup_program_url("経費精算の締め日は？", handbook, hosts) == ""

    def test_junk_tool_keywords_rejected(self):
        from raizuinu.answer import _is_placeholder_value

        for junk in ("no-op", "不要", "なし", "テスト", "placeholder", "サンプル"):
            assert _is_placeholder_value(junk), junk
        for real in ("下請代金 支払期日", "労働基準法", "印紙税"):
            assert not _is_placeholder_value(real), real

    def test_general_knowledge_fetch_failure_falls_back_to_link(self, tmp_path):
        handbook = make_handbook(tmp_path)
        config = Config.load(tmp_path / "no-config.json")
        config.data["web_fetch_enabled"] = True
        config.data["web_fetch_allowed_domains"] = ["www.freee.co.jp"]
        client = FakeClient(
            [fake_response(self._general_knowledge_stage1()), self._stage2_response("FETCH_FAILED")]
        )
        gen = AnswerGenerator(config, client=client)
        answer = gen.generate("接待交際費の限度は？", handbook)
        assert not answer.has_answer  # 1段目の案内文のまま
        assert FREEE_URL in answer.text

    def test_general_knowledge_disabled_skips_stage2(self, tmp_path):
        handbook = make_handbook(tmp_path)
        gen, client = make_generator(tmp_path, self._general_knowledge_stage1())
        answer = gen.generate("接待交際費の限度は？", handbook)  # 既定はweb_fetch無効
        assert client.calls == 1
        assert FREEE_URL in answer.text

    def test_general_knowledge_fabricated_reference_url_skips_stage2(self, tmp_path):
        handbook = make_handbook(tmp_path)
        config = Config.load(tmp_path / "no-config.json")
        config.data["web_fetch_enabled"] = True
        config.data["web_fetch_allowed_domains"] = ["www.freee.co.jp"]
        payload = self._general_knowledge_stage1()
        payload["reference_url"] = "https://www.freee.co.jp/kb/kb-accounting/not-in-handbook/"
        payload["answer"] = "記載がありません"
        client = FakeClient(fake_response(payload))
        gen = AnswerGenerator(config, client=client)
        answer = gen.generate("質問", handbook)
        assert client.calls == 1  # 実在検証に落ちたURLでは記事取得しない

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

    def test_legal_knowledge_tool_loop(self, tmp_path):
        handbook = make_handbook(tmp_path)
        config = Config.load(tmp_path / "no-config.json")
        config.data["legal_enabled"] = True
        config.data["body_url_allowlist"] = ["https://laws.e-gov.go.jp/"]

        class FakeEgov:
            def search(self, keyword):
                return [{"law_id": "129AC0000000089", "law_title": "民法",
                         "law_num": "明治二十九年法律第八十九号",
                         "url": "https://laws.e-gov.go.jp/law/129AC0000000089", "snippets": []}]

            def search_by_title(self, title):
                return []

            def get_article(self, law_id, article=None):
                return {"law_title": "民法", "law_id": law_id, "article": article or "",
                        "url": f"https://laws.e-gov.go.jp/law/{law_id}",
                        "text": "第五百二十二条 契約は、申込みに対して相手方が承諾をしたときに成立する。"}

        tool_use_response = SimpleNamespace(
            stop_reason="tool_use",
            content=[
                SimpleNamespace(type="tool_use", id="tu_1", name="search_law",
                                input={"keyword": "契約 成立"}),
            ],
            usage=SimpleNamespace(input_tokens=1000, output_tokens=50,
                                  cache_creation_input_tokens=0, cache_read_input_tokens=0),
        )
        final_payload = {
            "intent": "legal_knowledge",
            "reference_url": "",
            "has_answer": True,
            "answer": "契約は承諾により成立します（民法第522条）。https://laws.e-gov.go.jp/law/129AC0000000089 ※e-Gov法令検索の条文に基づく一般的な情報です。",
            "sources": [],
            "suggested_file": "",
            "reported_manuals": [],
        }
        client = FakeClient([tool_use_response, fake_response(final_payload)])
        gen = AnswerGenerator(config, client=client, egov=FakeEgov())
        answer = gen.generate("契約はいつ成立する？", handbook)

        assert client.calls == 2
        # ツール結果が2回目のリクエストに含まれる
        tool_result_msg = client.kwargs["messages"][-1]
        assert tool_result_msg["content"][0]["type"] == "tool_result"
        assert "民法" in tool_result_msg["content"][0]["content"]
        # legal_knowledgeはsources空でもhas_answer維持、e-Gov URLは除去されない
        assert answer.has_answer
        assert "laws.e-gov.go.jp" in answer.text

    def test_placeholder_tool_args_rejected_without_egov_call(self, tmp_path):
        # 一般会計知識の質問でモデルがplaceholder引数の退行的ツール呼び出しをした
        # 実測ケース。e-Govへは送らず、エラー文をツール結果として返して継続する
        handbook = make_handbook(tmp_path)
        config = Config.load(tmp_path / "no-config.json")
        config.data["legal_enabled"] = True

        class ExplodingEgov:
            def search(self, keyword):
                raise AssertionError("placeholder引数でe-Govを呼んではならない")

            def search_by_title(self, title):
                raise AssertionError("placeholder引数でe-Govを呼んではならない")

            def get_article(self, law_id, article=None):
                raise AssertionError("placeholder引数でe-Govを呼んではならない")

        tool_use_response = SimpleNamespace(
            stop_reason="tool_use",
            content=[
                SimpleNamespace(type="tool_use", id="tu_1", name="search_law",
                                input={"keyword": "placeholder"}),
                SimpleNamespace(type="tool_use", id="tu_2", name="get_law_article",
                                input={"law_id": "placeholder"}),
                SimpleNamespace(type="tool_use", id="tu_3", name="search_law",
                                input={"keyword": "a"}),  # 1文字キーワードも実測された退行
            ],
            usage=None,
        )
        final_payload = {
            "intent": "general_knowledge",
            "reference_url": "",
            "has_answer": False,
            "answer": "社内ハンドブックには記載がありません。",
            "sources": [],
            "suggested_file": "",
            "reported_manuals": [],
        }
        client = FakeClient([tool_use_response, fake_response(final_payload)])
        gen = AnswerGenerator(config, client=client, egov=ExplodingEgov())
        answer = gen.generate("貸倒引当金って何ですか？", handbook)

        assert client.calls == 2
        tool_results = client.kwargs["messages"][-1]["content"]
        assert len(tool_results) == 3
        assert all(r["content"].startswith("エラー:") for r in tool_results)
        assert answer.text == "社内ハンドブックには記載がありません。"

    def test_placeholder_only_tool_calls_do_not_ground_legal_answer(self, tmp_path):
        # placeholder引数で棄却された呼び出しは「ツール実行済み」に数えない。
        # 条文を取得していない法令回答はquestionへ降格され出典検証を通る
        handbook = make_handbook(tmp_path)
        config = Config.load(tmp_path / "no-config.json")
        config.data["legal_enabled"] = True

        class ExplodingEgov:
            def search(self, keyword):
                raise AssertionError("placeholder引数でe-Govを呼んではならない")

        tool_use_response = SimpleNamespace(
            stop_reason="tool_use",
            content=[
                SimpleNamespace(type="tool_use", id="tu_1", name="search_law",
                                input={"keyword": "placeholder"}),
            ],
            usage=None,
        )
        final_payload = {
            "intent": "legal_knowledge",
            "reference_url": "",
            "has_answer": True,
            "answer": "民法第522条により契約は承諾で成立します（条文未取得の断定）",
            "sources": [],
            "suggested_file": "",
            "reported_manuals": [],
        }
        client = FakeClient([tool_use_response, fake_response(final_payload)])
        gen = AnswerGenerator(config, client=client, egov=ExplodingEgov())
        answer = gen.generate("契約はいつ成立する？", handbook)
        assert answer.intent == "question"
        assert not answer.has_answer
        assert "断定" not in answer.text

    def test_general_knowledge_stage2_truncation_falls_back(self, tmp_path):
        handbook = make_handbook(tmp_path)
        config = Config.load(tmp_path / "no-config.json")
        config.data["web_fetch_enabled"] = True
        config.data["web_fetch_allowed_domains"] = ["www.freee.co.jp"]
        truncated = self._stage2_response("途中まで書かれた要約が突然")
        truncated.stop_reason = "max_tokens"
        client = FakeClient([fake_response(self._general_knowledge_stage1()), truncated])
        gen = AnswerGenerator(config, client=client)
        answer = gen.generate("接待交際費の限度は？", handbook)
        # 途中切れの要約（免責文なし）は採用せず、URL案内文にフォールバック
        assert not answer.has_answer
        assert FREEE_URL in answer.text
        assert "途中まで" not in answer.text

    def test_general_knowledge_wrong_domain_skips_stage2(self, tmp_path):
        handbook = make_handbook(tmp_path)
        config = Config.load(tmp_path / "no-config.json")
        config.data["web_fetch_enabled"] = True
        config.data["web_fetch_allowed_domains"] = ["www.freee.co.jp"]
        payload = self._general_knowledge_stage1()
        payload["reference_url"] = REAL_URL  # ハンドブックには実在するがfreee外ドメイン
        payload["answer"] = "社内ハンドブックには記載がありません。"  # 本文にも取得可URLなし
        client = FakeClient(fake_response(payload))
        gen = AnswerGenerator(config, client=client)
        gen.generate("質問", handbook)
        assert client.calls == 1  # 取得できないドメインへの2段目は呼ばない

    def test_wrong_domain_reference_recovers_from_body_url(self, tmp_path):
        # reference_urlが取得できないドメインでも、本文に正しい記事URLがあれば拾う
        handbook = make_handbook(tmp_path)
        config = Config.load(tmp_path / "no-config.json")
        config.data["web_fetch_enabled"] = True
        config.data["web_fetch_allowed_domains"] = ["www.freee.co.jp"]
        payload = self._general_knowledge_stage1()
        payload["reference_url"] = REAL_URL  # 取得できないドメイン
        client = FakeClient(
            [fake_response(payload), self._stage2_response("限度額は年800万円です。※")]
        )
        gen = AnswerGenerator(config, client=client)
        answer = gen.generate("質問", handbook)
        assert client.calls == 2
        assert answer.reference_url == FREEE_URL  # 本文にあったfreee記事へ差し替わる
        assert FREEE_URL in client.kwargs["messages"][0]["content"]

    def test_general_knowledge_guidance_survives_downgrade(self, tmp_path):
        handbook = make_handbook(tmp_path)
        config = Config.load(tmp_path / "no-config.json")
        config.data["web_fetch_enabled"] = True
        config.data["web_fetch_allowed_domains"] = ["www.freee.co.jp"]
        payload = self._general_knowledge_stage1()
        payload["has_answer"] = True  # モデルが原則から逸脱してtrueを返したケース
        client = FakeClient([fake_response(payload), self._stage2_response("FETCH_FAILED")])
        gen = AnswerGenerator(config, client=client)
        answer = gen.generate("質問", handbook)
        assert not answer.has_answer
        assert FREEE_URL in answer.text  # URL案内文は定型失敗文に置き換えられない

    def test_legal_intent_without_tool_use_is_downgraded(self, tmp_path):
        handbook = make_handbook(tmp_path)
        config = Config.load(tmp_path / "no-config.json")
        config.data["legal_enabled"] = True
        payload = {
            "intent": "legal_knowledge",
            "reference_url": "",
            "has_answer": True,
            "answer": "民法第522条により契約は承諾で成立します（ツール未使用の断定）",
            "sources": [],
            "suggested_file": "",
            "reported_manuals": [],
        }

        class NoopEgov:
            pass

        client = FakeClient(fake_response(payload))
        gen = AnswerGenerator(config, client=client, egov=NoopEgov())
        answer = gen.generate("契約はいつ成立する？", handbook)
        # ツール未実行の法令回答はquestion扱いに降格→出典なしとして却下される
        assert not answer.has_answer
        assert "断定" not in answer.text

    def test_tool_loop_exhaustion_returns_dedicated_error(self, tmp_path):
        handbook = make_handbook(tmp_path)
        config = Config.load(tmp_path / "no-config.json")
        config.data["legal_enabled"] = True

        calls = {"count": 0}

        class CountingEgov:
            def search(self, keyword):
                calls["count"] += 1
                return [{"law_id": "129AC0000000089", "law_title": "民法",
                         "law_num": "", "url": "https://laws.e-gov.go.jp/law/129AC0000000089",
                         "snippets": []}]

            def search_by_title(self, title):
                return []

        def tool_use_response():
            return SimpleNamespace(
                stop_reason="tool_use",
                content=[SimpleNamespace(type="tool_use", id="tu", name="search_law",
                                         input={"keyword": "民法"})],
                usage=SimpleNamespace(input_tokens=10, output_tokens=1,
                                      cache_creation_input_tokens=0, cache_read_input_tokens=0),
            )

        from raizuinu.answer import _MAX_LOOP_STEPS

        client = FakeClient([tool_use_response() for _ in range(_MAX_LOOP_STEPS)])
        gen = AnswerGenerator(config, client=client, egov=CountingEgov())
        answer = gen.generate("質問", handbook)
        assert client.calls == _MAX_LOOP_STEPS
        assert calls["count"] == _MAX_LOOP_STEPS - 1  # 最終回はツールを実行しない
        assert not answer.has_answer
        assert "上限" in answer.text

    def test_truncated_url_fails_validation(self, tmp_path):
        handbook = make_handbook(tmp_path)
        gen, _ = make_generator(
            tmp_path,
            {
                "has_answer": True,
                "answer": "回答",
                "sources": [{"file": "銀行明細取得.md", "heading": "",
                             "url": REAL_URL[:-5]}],  # 実在URLの切り詰め
                "suggested_file": "",
            },
        )
        answer = gen.generate("質問", handbook)
        # 切り詰めURLは通さず（完全一致のみ）、正しい原本URLに補完される
        assert answer.sources[0]["url"] == REAL_URL

    def test_legal_tools_attached_when_enabled(self, tmp_path):
        handbook = make_handbook(tmp_path)
        config = Config.load(tmp_path / "no-config.json")
        config.data["legal_enabled"] = True

        class NoopEgov:
            pass

        client = FakeClient(
            fake_response({"has_answer": False, "answer": "-", "sources": [], "suggested_file": ""})
        )
        gen = AnswerGenerator(config, client=client, egov=NoopEgov())
        gen.generate("質問", handbook)
        names = [t["name"] for t in client.kwargs["tools"]]
        assert names == ["search_law", "get_law_article"]

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


class TestPlaceholderDetection:
    def test_placeholder_like_values_detected(self):
        from raizuinu.answer import _is_placeholder_value

        for value in ["placeholder", "Placeholder", "PLACEHOLDER", "", "   ",
                      "<keyword>", "<law_id>", "string", "n/a", "---", "…"]:
            assert _is_placeholder_value(value), value

    def test_real_values_pass(self):
        from raizuinu.answer import _is_placeholder_value

        for value in ["下請代金 支払期日", "貸倒引当金", "民法", "129AC0000000089"]:
            assert not _is_placeholder_value(value), value
