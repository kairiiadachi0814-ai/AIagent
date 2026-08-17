import io
import json
import zipfile
from types import SimpleNamespace

from raizuinu.config import Config
from raizuinu.doctask import (
    DocTaskRunner,
    decode_text,
    extract_docx_text,
    find_document,
)

GOOGLE_DOC_URL = "https://docs.google.com/document/d/1AbcDef_GHI-jkl/edit?tab=t.0"


def make_docx_raw(xml_body: str) -> bytes:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{xml_body}</w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


def make_docx(paragraphs: list[str]) -> bytes:
    return make_docx_raw(
        "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    )


class TestFindDocument:
    def test_file_in_mention_body(self):
        body = "[To:999] これ議事録にして [info][download:777]会議.docx (10 KB)[/download][/info]"
        found = find_document(body, [], "これ議事録にして")
        assert found["kind"] == "chatwork_file"
        assert found["files"] == [{"file_id": 777, "filename": "会議.docx"}]

    def test_multiple_files_collected(self):
        body = (
            "[To:999] まとめて "
            "[download:1]前半.docx (10 KB)[/download]"
            "[download:2]後半.txt (5 KB)[/download]"
            "[download:3]写真.png (2 MB)[/download]"
        )
        found = find_document(body, [], "まとめて")
        assert [e["file_id"] for e in found["files"]] == [1, 2]  # 対応形式のみ

    def test_google_url_in_mention_body(self):
        body = f"[To:999] 要約お願い {GOOGLE_DOC_URL}"
        found = find_document(body, [], "要約お願い")
        assert found["kind"] == "google"
        assert found["doc_type"] == "document"

    def test_spreadsheet_gid_captured(self):
        url = "https://docs.google.com/spreadsheets/d/1AbcDef_GHI-jkl/edit#gid=123456"
        found = find_document(f"[To:999] 要約して {url}", [], "要約して")
        assert found["doc_type"] == "spreadsheets"
        assert found["gid"] == "123456"

    def test_handbook_manual_url_is_excluded(self):
        # ボット回答に頻出する原本マニュアルURLは文書タスクにしない（Q&Aで扱う）
        found = find_document(
            f"[To:999] これ要約して {GOOGLE_DOC_URL}", [], "これ要約して",
            handbook_url_check=lambda url: True,
        )
        assert found is None

    def test_quoted_block_is_ignored(self):
        body = (
            f"[qt][qtmeta aid=999]出典 {GOOGLE_DOC_URL} [download:11]会議.docx[/download][/qt]"
            "この出典の見出しを教えて"
        )
        assert find_document(body, [], "この出典の見出しを教えて") is None

    def test_unsupported_attachment_with_question_falls_to_qa(self):
        # 画像添付＋通常質問はQ&Aへ（Noneを返す）。依頼が明確なときだけ形式案内
        body = "[To:999] この処理どうやるの？ [download:9]請求書.png (1 MB)[/download]"
        assert find_document(body, [], "この処理どうやるの？") is None
        body2 = "[To:999] これ議事録にして [download:9]会議録.pdf (1 MB)[/download]"
        found = find_document(body2, [], "これ議事録にして")
        assert found["kind"] == "unsupported_file"

    def test_recent_message_fallback_requires_keyword(self):
        recent = [{"body": "[download:555]会議.docx (10 KB)[/download]"}]
        found = find_document(
            "[To:999] さっきのファイルを議事録にして", recent, "さっきのファイルを議事録にして"
        )
        assert found["files"][0]["file_id"] == 555
        # キーワードが無い通常質問は、過去の添付に引きずられない
        assert find_document("[To:999] 経費精算の締め日は？", recent, "経費精算の締め日は？") is None

    def test_recent_message_fallback_ignores_bot_and_google_urls(self):
        recent = [
            {"account": {"account_id": 999},
             "body": f"回答です。詳細: {GOOGLE_DOC_URL}\n[download:888]まとめ.docx[/download]"},
            {"account": {"account_id": 111}, "body": f"共有します {GOOGLE_DOC_URL}"},
        ]
        # ボット自身の発言中の添付・URLは拾わず、メンバー発言のGoogle URLも過去分は拾わない
        assert find_document(
            "[To:999] 経費の流れを要約して", recent, "経費の流れを要約して",
            bot_account_id=999,
        ) is None

    def test_howto_question_does_not_scan_history(self):
        recent = [{"body": "[download:555]請求書控.docx (10 KB)[/download]"}]
        assert find_document(
            "[To:999] 議事録の書き方を教えて", recent, "議事録の書き方を教えて"
        ) is None

    def test_history_scan_requires_doc_reference_word(self):
        recent = [{"body": "[download:555]会議.docx (10 KB)[/download]"}]
        # 「要約して」だけでは遡らない（貼り付け素材や一般質問の横取り防止）
        assert find_document("[To:999] 経費の流れを要約して", recent, "経費の流れを要約して") is None

    def test_pasted_material_does_not_scan_history(self):
        recent = [{"body": "[download:555]会議.docx (10 KB)[/download]"}]
        pasted = "さっきの会議まとめて\n発言1\n発言2\n発言3\n発言4"
        # 本文に素材が貼られていれば遡らずtaskフロー（Q&A側）で処理する
        assert find_document(f"[To:999] {pasted}", recent, pasted) is None

    def test_no_document(self):
        assert find_document("[To:999] 議事録の書き方を教えて", [], "議事録の書き方を教えて") is None


class TestExtraction:
    def test_docx_paragraphs_to_text(self):
        data = make_docx(["経理定例会議", "決定事項: 締め日を5日に変更", "宿題: 坂田さんが&amp;確認"])
        text = extract_docx_text(data)
        assert "経理定例会議" in text
        assert "決定事項: 締め日を5日に変更" in text
        assert "坂田さんが&確認" in text  # HTMLエンティティを戻す

    def test_cp932_text_decoded(self):
        assert decode_text("会議メモ".encode("cp932")) == "会議メモ"

    def test_tracked_changes_field_codes_and_textbox_dupes_excluded(self):
        data = make_docx_raw(
            "<w:p><w:r><w:t>決定A</w:t></w:r>"
            '<w:del w:id="1" w:author="x"><w:r><w:delText>旧金額100万円</w:delText></w:r></w:del>'
            "</w:p>"
            '<w:p><w:pPr><w:rPr><w:del w:id="2"/></w:rPr></w:pPr>'
            "<w:r><w:t>生きている本文</w:t></w:r></w:p>"
            '<w:p><w:r><w:instrText xml:space="preserve"> TOC \\o </w:instrText>'
            "<w:t>現行金額200万円</w:t></w:r></w:p>"
            "<w:p><mc:AlternateContent><mc:Choice><w:r><w:t>箱の本文</w:t></w:r></mc:Choice>"
            "<mc:Fallback><w:r><w:t>箱の本文</w:t></w:r></mc:Fallback>"
            "</mc:AlternateContent></w:p>"
        )
        text = extract_docx_text(data)
        assert "旧金額100万円" not in text  # 変更履歴で削除済みの本文は含めない
        assert "決定A" in text
        assert "生きている本文" in text  # 自己完結<w:del/>が生きた本文を巻き込まない
        assert "TOC" not in text  # フィールドコードは除去
        assert "現行金額200万円" in text
        assert text.count("箱の本文") == 1  # テキストボックスの互換用複製は1回だけ


def fake_client(reply_text="■決定事項\n・締め日を5日に変更", stop_reason="end_turn"):
    response = SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="text", text=reply_text)],
        usage=SimpleNamespace(
            input_tokens=20000, output_tokens=800,
            cache_creation_input_tokens=0, cache_read_input_tokens=0,
        ),
    )
    client = SimpleNamespace(kwargs=None)

    def create(**kwargs):
        client.kwargs = kwargs
        return response

    client.messages = SimpleNamespace(create=create)
    return client


class FakeChatworkFiles:
    def __init__(self, filename="会議文字起こし.docx", filesize=1024):
        self.filename = filename
        self.filesize = filesize

    def get_file_info(self, room_id, file_id):
        return {
            "filename": self.filename,
            "filesize": self.filesize,
            "download_url": "https://files.example/download",
        }


def make_runner(tmp_path, docx_data=None, filename="会議文字起こし.docx",
                filesize=1024, client=None, http_status=200, http_final_url=None):
    config = Config.load(tmp_path / "no-config.json")
    config.data["doc_task"] = {"enabled": True, "max_file_mb": 20, "max_text_chars": 120000}
    data = docx_data if docx_data is not None else make_docx(["会議の内容です。" * 5])

    def http_get(url, timeout=60):
        return http_status, http_final_url or url, data

    client = client or fake_client()
    runner = DocTaskRunner(
        config, FakeChatworkFiles(filename, filesize), client=client, http_get=http_get
    )
    return runner, client


def file_doc(file_id=1, filename="会議文字起こし.docx"):
    return {"kind": "chatwork_file", "files": [{"file_id": file_id, "filename": filename}],
            "total_files": 1}


class TestDocTaskRunner:
    def test_docx_happy_path(self, tmp_path):
        runner, client = make_runner(tmp_path)
        reply, meta, usage = runner.run("議事録にまとめて", file_doc(), 12345)
        assert "■決定事項" in reply
        assert "会議文字起こし.docx" in reply  # どの文書に基づいたかを明示
        assert meta["label"] == "会議文字起こし.docx"
        assert usage["input_tokens"] == 20000
        content = client.kwargs["messages"][0]["content"]
        assert "会議の内容です。" in content
        assert "議事録にまとめて" in content
        assert "===文書" in content  # 文書本文は区切りで囲む（指示と混同させない）

    def test_prompt_requires_conversational_opening(self, tmp_path):
        # 成果物をいきなり出さず、依頼メッセージに一言応じてから返すこと
        runner, client = make_runner(tmp_path)
        runner.run("さっきは添付を忘れました。議事録にまとめて", file_doc(), 12345)
        system = client.kwargs["system"]
        assert "依頼者のメッセージに一言応じることから始める" in system
        # 依頼文そのものもモデルへ渡っている（相手の言葉に応答できる）
        assert "さっきは添付を忘れました" in client.kwargs["messages"][0]["content"]

    def test_unsupported_kind_returns_guidance_without_api_call(self, tmp_path):
        runner, client = make_runner(tmp_path)
        reply, meta, usage = runner.run(
            "まとめて",
            {"kind": "unsupported_file", "files": [{"file_id": 9, "filename": "会議録.pdf"}]},
            12345,
        )
        assert ".docx" in reply
        assert usage == {}
        assert client.kwargs is None  # API呼び出しなし

    def test_unsupported_extension_from_api_returns_guidance(self, tmp_path):
        runner, _ = make_runner(tmp_path, filename="会議録.pdf")
        reply, meta, usage = runner.run("まとめて", file_doc(filename="会議録.pdf"), 12345)
        assert ".docx" in reply
        assert meta.get("error") == "load_failed"
        assert usage == {}

    def test_oversized_file_rejected(self, tmp_path):
        runner, _ = make_runner(tmp_path, filesize=25 * 1024 * 1024)
        reply, meta, usage = runner.run("まとめて", file_doc(), 12345)
        assert "20MB" in reply
        assert usage == {}

    def test_google_doc_permission_error(self, tmp_path):
        runner, _ = make_runner(
            tmp_path, http_final_url="https://accounts.google.com/signin"
        )
        reply, meta, usage = runner.run(
            "要約して",
            {"kind": "google", "doc_type": "document", "url": GOOGLE_DOC_URL,
             "doc_id": "1AbcDef_GHI-jkl", "gid": ""},
            12345,
        )
        assert "共有設定" in reply
        assert usage == {}

    def test_spreadsheet_gid_used_in_export_url(self, tmp_path):
        seen_urls = []
        config = Config.load(tmp_path / "no-config.json")
        config.data["doc_task"] = {"enabled": True, "max_file_mb": 20, "max_text_chars": 120000}

        def http_get(url, timeout=60):
            seen_urls.append(url)
            return 200, url, "セルA,セルB".encode("utf-8")

        runner = DocTaskRunner(config, FakeChatworkFiles(), client=fake_client(), http_get=http_get)
        reply, meta, usage = runner.run(
            "要約して",
            {"kind": "google", "doc_type": "spreadsheets", "url": "x",
             "doc_id": "1AbcDef_GHI-jkl", "gid": "123456"},
            12345,
        )
        assert seen_urls[0].endswith("export?format=csv&gid=123456")
        assert "指定のシート" in meta["label"]

    def test_truncated_note_added_for_long_document(self, tmp_path):
        runner, client = make_runner(
            tmp_path, docx_data=make_docx(["長い議事録です。" * 200])
        )
        runner._config.data["doc_task"]["max_text_chars"] = 200
        reply, meta, usage = runner.run("まとめて", file_doc(), 12345)
        assert meta.get("truncated") is True
        content = client.kwargs["messages"][0]["content"]
        assert "途中まで" in content
        assert content.index("途中まで") < content.index("===文書")  # 注意書きは文書より前


CONTRACT_TEXT = (
    "業務委託基本契約書\n"
    "株式会社ライズクリエイション（以下「甲」という）と株式会社◯◯（以下「乙」という）は、"
    "次のとおり本契約を締結する。\n"
    "第1条（目的）本契約は、甲が乙に業務を委託することを定める。\n"
    "第2条（委託料）月額50万円とし、翌月末日までに支払う。\n"
    "第3条（契約期間）本契約の有効期間は1年とし、異議なき場合は自動更新する。\n"
)


class TestContractMode:
    def test_contract_detection(self):
        from raizuinu.doctask import looks_like_contract

        assert looks_like_contract(CONTRACT_TEXT)
        assert not looks_like_contract("経理定例会議 2026-08-14\n参加者: 坂田、田中\n決定事項: なし")

    def test_stamp_table_urls_read_from_link_collection(self, tmp_path):
        from raizuinu.doctask import stamp_table_urls

        u1 = "https://www.nta.go.jp/taxes/shiraberu/taxanswer/inshi/7140.htm"
        u2 = "https://www.nta.go.jp/taxes/shiraberu/taxanswer/inshi/7141.htm"
        (tmp_path / "税法リンク集_国税庁.md").write_text(
            f"# 税法\n| No.7140 印紙税額の一覧表（その1）第1号文書から | 印紙 | {u1} |\n"
            f"| No.7141 印紙税額の一覧表（その2）第5号文書から | 印紙 | {u2} |\n"
            "| No.7105 領収書 | 領収書 | https://www.nta.go.jp/x.htm |\n",
            encoding="utf-8",
        )
        config = Config.load(tmp_path / "no-config.json")
        config.base_dir = tmp_path
        config.data["handbook"] = {"roots": ["."], "include": ["*.md"], "exclude": []}
        assert stamp_table_urls(config) == [u1, u2]

    def test_contract_summary_forbids_legal_judgement(self, tmp_path):
        runner, client = make_runner(tmp_path, docx_data=make_docx([CONTRACT_TEXT]))
        runner.run("この契約書の要点をまとめて", file_doc(filename="業務委託契約書.docx"), 12345)
        system = client.kwargs["system"]
        assert "契約期間・自動更新の有無" in system  # 契約書用の構成を指示
        assert "法的な妥当性・有利不利・リスクの評価は行わない" in system
        assert "tools" not in client.kwargs  # 印紙の依頼でなければ取得しない

    def test_stamp_duty_request_enables_nta_fetch(self, tmp_path):
        u1 = "https://www.nta.go.jp/taxes/shiraberu/taxanswer/inshi/7140.htm"
        (tmp_path / "税法リンク集_国税庁.md").write_text(
            f"# 税法\n| No.7140 印紙税額の一覧表（その1） | 印紙 | {u1} |\n", encoding="utf-8"
        )
        runner, client = make_runner(tmp_path, docx_data=make_docx([CONTRACT_TEXT]))
        runner._config.base_dir = tmp_path
        runner._config.data["handbook"] = {"roots": ["."], "include": ["*.md"], "exclude": []}
        runner._config.data["web_fetch_enabled"] = True
        runner.run("この契約書の印紙はいくら？", file_doc(filename="業務委託契約書.docx"), 12345)
        assert client.kwargs["tools"][0]["allowed_domains"] == ["www.nta.go.jp"]
        assert u1 in client.kwargs["messages"][0]["content"]  # 表のURLを渡す
        system = client.kwargs["system"]
        assert "号文書" in system
        assert "丸めたり概算にしたりしない" in system

    def test_stamp_request_without_link_collection_skips_fetch(self, tmp_path):
        runner, client = make_runner(tmp_path, docx_data=make_docx([CONTRACT_TEXT]))
        runner._config.base_dir = tmp_path
        runner._config.data["handbook"] = {"roots": ["."], "include": ["*.md"], "exclude": []}
        runner._config.data["web_fetch_enabled"] = True
        runner.run("印紙はいくら？", file_doc(filename="契約書.docx"), 12345)
        assert "tools" not in client.kwargs  # リンク集が無ければ取得しない（推測で答えさせない）


class TestDocumentRequestDetection:
    def test_looks_like_document_request(self):
        from raizuinu.doctask import looks_like_document_request

        assert looks_like_document_request("さっきのファイルを議事録にして")
        assert looks_like_document_request("添付の内容を要約して")
        assert looks_like_document_request("先ほど送った議事録を整理して")
        assert not looks_like_document_request("議事録の書き方を教えて")  # 手順Q&A
        assert not looks_like_document_request("経費精算の締め日は？")
        assert not looks_like_document_request("添付書類の提出先はどこ？")  # 要約等の依頼でない

    def test_ordinary_questions_are_not_hijacked(self):
        # ハンドブックで答えるべき質問が案内文・文書探索に横取りされないこと
        from raizuinu.doctask import _should_scan_history, looks_like_document_request

        ordinary = [
            "請求書ファイルの保管ルールを整理して",
            "経費精算の資料をまとめて教えて",
            "決算資料の作り方を整理して",
            "5万円以上の資産計上ルールを整理して",  # 「以上の」が「上の」に部分一致しない
            "10万円以上の備品の処理をまとめて教えて",
            "以前の規定との違いをまとめて教えて",  # 「以前の」が「前の」に部分一致しない
            "領収書ファイルの整理はどうすればいい",
            "振込データのアップロード手順をまとめて",  # 「アップロード手順」が「アップ」に一致しない
        ]
        for q in ordinary:
            assert not looks_like_document_request(q), q
            assert not _should_scan_history(q), q

    def test_pasted_material_is_not_treated_as_missing_file(self):
        # 本文に文字起こしを貼った依頼を「添付が見つかりません」で握り潰さない
        from raizuinu.doctask import looks_like_document_request

        pasted = (
            "先ほどの打ち合わせの文字起こしを議事録にまとめてください\n"
            "田中: 前期の資料を確認しました\n坂田: 締め日は5営業日以内です\n"
            "田中: 承知しました\n" + "以下は詳細な発言記録です。" * 20
        )
        assert not looks_like_document_request(pasted)


class TestHandlerIntegration:
    def test_doc_task_routes_before_qa(self, tmp_path, monkeypatch):
        from tests.test_handler import FakeAudit, FakeChatwork, FakeGenerator, make_handler
        import base64, hashlib, hmac

        from raizuinu.handler import RaizuinuHandler
        from raizuinu.handbook import HandbookLoader

        token = base64.b64encode(b"doc-test-key").decode()
        monkeypatch.setenv("CHATWORK_WEBHOOK_TOKEN", token)
        (tmp_path / "銀行明細取得.md").write_text("# 銀行明細取得\n手順\n", encoding="utf-8")
        config = Config.load(tmp_path / "no-config.json")
        config.data["allowed_room_ids"] = [12345]
        config.data["webhook_async"] = False
        config.data["state_dir"] = str(tmp_path / "state")
        config.data["audit_log_dir"] = str(tmp_path / "logs")
        config.data["doc_task"] = {"enabled": True, "max_file_mb": 20, "max_text_chars": 120000}
        config.base_dir = tmp_path

        chatwork = FakeChatwork()
        chatwork.get_file_info = lambda room_id, file_id: {
            "filename": "会議.docx", "filesize": 100,
            "download_url": "https://files.example/dl",
        }
        audit = FakeAudit()
        generator = FakeGenerator(None)  # 呼ばれないはず
        runner = DocTaskRunner(
            config, chatwork, client=fake_client("■要点\n・テスト"),
            http_get=lambda url, timeout=60: (200, url, make_docx(["本文"])),
        )
        handler = RaizuinuHandler(
            config, chatwork=chatwork, generator=generator, audit=audit,
            handbook_loader=HandbookLoader([tmp_path], ["*.md"], [], 300),
            doc_task=runner,
        )

        body = json.dumps({
            "webhook_event_type": "mention_to_me",
            "webhook_event": {
                "from_account_id": 111, "to_account_id": 999,
                "room_id": 12345, "message_id": "300001",
                "body": "[To:999] 議事録にして [download:42]会議.docx[/download]",
                "send_time": 1700000000,
            },
        }).encode()
        digest = hmac.new(base64.b64decode(token), body, hashlib.sha256).digest()
        result = handler.handle_webhook(body, base64.b64encode(digest).decode())
        assert result.status == 200
        assert generator.calls == []  # Q&Aフローは走らない
        assert len(chatwork.sent) == 1
        room, sent_body = chatwork.sent[0]
        assert "■要点" in sent_body
        assert "[rp aid=111" in sent_body
        assert audit.records[0]["type"] == "doc_task"

    def test_history_upload_by_member_is_found_and_processed(self, tmp_path, monkeypatch):
        # 「さっきのファイルを議事録にして」→ 直近のメンバー投稿の添付を拾って処理する
        handler, chatwork, generator, audit, token = self._make(tmp_path, monkeypatch)
        chatwork.messages = [
            {"message_id": "1", "account": {"account_id": 111, "name": "坂田"},
             "body": "[info][title][dtext:file_uploaded][/title]"
                     "[download:42]会議文字起こし.txt (1 KB)[/download][/info]"},
        ]
        self._send(handler, token, "[To:999]さっきのファイルを議事録にして。")
        assert generator.calls == []  # Q&Aへ流れない
        assert "■要点" in chatwork.sent[0][1]
        assert audit.records[0]["type"] == "doc_task"

    def test_handbook_url_request_goes_to_qa_not_guidance(self, tmp_path, monkeypatch):
        # ハンドブック原本URL付きの依頼は「見つかりません」ではなくQ&Aへ回す
        handler, chatwork, generator, audit, token = self._make(tmp_path, monkeypatch)
        (tmp_path / "マニュアルリンク集.md").write_text(
            f"# リンク集\n- 経費: {GOOGLE_DOC_URL}\n", encoding="utf-8"
        )
        from raizuinu.answer import Answer

        generator._answer = Answer(
            has_answer=True, text="手順はこうです",
            sources=[{"file": "銀行明細取得.md", "heading": "", "url": ""}],
            usage={"input_tokens": 1, "output_tokens": 1},
        )
        self._send(handler, token, f"[To:999]さっきの資料を要約して {GOOGLE_DOC_URL}")
        assert len(generator.calls) == 1  # Q&Aフローが処理する
        assert "見つかりませんでした" not in chatwork.sent[0][1]

    def test_document_request_without_file_returns_guidance(self, tmp_path, monkeypatch):
        # 見つからないときは「機能がない」ではなく、依頼の仕方を案内する（API消費なし）
        handler, chatwork, generator, audit, token = self._make(tmp_path, monkeypatch)
        chatwork.messages = [
            {"message_id": "1", "account": {"account_id": 999, "name": "経理財務アシスタント"},
             "body": "[download:42]ボットが上げたファイル.txt (1 KB)[/download]"},
        ]
        self._send(handler, token, "[To:999]さっきのファイルを議事録にして。")
        assert generator.calls == []
        sent = chatwork.sent[0][1]
        assert "添付ファイルが見つかりませんでした" in sent
        assert ".docx" in sent  # 依頼の仕方を案内
        assert audit.records[0]["type"] == "doc_task_not_found"

    # --- ヘルパー ---

    def _make(self, tmp_path, monkeypatch):
        from tests.test_handler import FakeAudit, FakeChatwork, FakeGenerator
        import base64

        from raizuinu.handbook import HandbookLoader
        from raizuinu.handler import RaizuinuHandler

        token = base64.b64encode(b"doc-test-key2").decode()
        monkeypatch.setenv("CHATWORK_WEBHOOK_TOKEN", token)
        (tmp_path / "銀行明細取得.md").write_text("# 銀行明細取得\n手順\n", encoding="utf-8")
        config = Config.load(tmp_path / "no-config.json")
        config.data.update({
            "allowed_room_ids": [12345], "webhook_async": False,
            "state_dir": str(tmp_path / "state"), "audit_log_dir": str(tmp_path / "logs"),
            "doc_task": {"enabled": True, "max_file_mb": 20, "max_text_chars": 120000,
                         "search_message_count": 60},
        })
        config.base_dir = tmp_path

        chatwork = FakeChatwork()
        chatwork.messages = []
        chatwork.get_recent_messages = lambda room_id, limit=20: chatwork.messages
        chatwork.get_file_info = lambda room_id, file_id: {
            "filename": "会議文字起こし.txt", "filesize": 100,
            "download_url": "https://files.example/dl",
        }
        audit = FakeAudit()
        generator = FakeGenerator(None)
        runner = DocTaskRunner(
            config, chatwork, client=fake_client("■要点\n・テスト"),
            http_get=lambda url, timeout=60: (200, url, "会議の本文".encode("utf-8")),
        )
        handler = RaizuinuHandler(
            config, chatwork=chatwork, generator=generator, audit=audit,
            handbook_loader=HandbookLoader([tmp_path], ["*.md"], [], 300),
            doc_task=runner,
        )
        return handler, chatwork, generator, audit, token

    @staticmethod
    def _send(handler, token, body_text):
        import base64, hashlib, hmac

        body = json.dumps({
            "webhook_event_type": "mention_to_me",
            "webhook_event": {
                "from_account_id": 111, "to_account_id": 999,
                "room_id": 12345, "message_id": "300002",
                "body": body_text, "send_time": 1700000000,
            },
        }).encode()
        digest = hmac.new(base64.b64decode(token), body, hashlib.sha256).digest()
        return handler.handle_webhook(body, base64.b64encode(digest).decode())
