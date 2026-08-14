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
