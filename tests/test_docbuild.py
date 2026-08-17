import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from raizuinu.chatwork import _multipart
from raizuinu.config import Config
from raizuinu.docbuild import (
    CONTRACT_GUIDANCE,
    DocBuildRunner,
    looks_like_document_build_request,
    wants_contract_template,
)
from raizuinu.templatefill import TemplateError, render_docx, render_xlsx

REPO_TEMPLATES = "templates"


class TestRequestDetection:
    @pytest.mark.parametrize(
        "question",
        [
            "南都銀行あての書類送付状を作って",
            "りそな銀行あてのFAX送付状を作成してください",
            "大塚商会あての送付状、契約書1部で作成して",
            "ヤマトライジングの送付状お願いします",
        ],
    )
    def test_build_requests(self, question):
        assert looks_like_document_build_request(question) is True

    @pytest.mark.parametrize(
        "question",
        [
            "送付状の書き方を教えて",
            "送付状のひな形はどこにありますか",
            "書類送付状の保管ルールは？",
            "経費精算の締め日は？",
            "会議の文字起こしを議事録にまとめて",
        ],
    )
    def test_not_build_requests(self, question):
        assert looks_like_document_build_request(question) is False

    def test_contract_template_request_is_separated(self):
        # 契約書そのものの作成はひな形URL案内へ倒す（リーガルチェック必須のため）
        assert wants_contract_template("業務委託契約書を作って") is True
        # 送付状の同封物として契約書に触れるだけなら送付状の作成依頼
        assert wants_contract_template("契約書2部を入れる送付状を作って") is False


def make_config(tmp_path):
    config = Config.load(tmp_path / "no-config.json")
    config.data["doc_build"] = {
        "enabled": True,
        "templates_dir": REPO_TEMPLATES,
        "allowed_templates": [
            "書類送付状_ライズ",
            "書類送付状_ヤマトライジング",
            "書類送付状_楽天軒",
            "FAX送付状",
        ],
        "max_items": 20,
        "attach_to_chatwork": True,
    }
    config.base_dir = __import__("pathlib").Path(__file__).resolve().parents[1]
    return config


def fake_client(fields):
    response = SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=json.dumps(fields, ensure_ascii=False))],
        usage=SimpleNamespace(
            input_tokens=1200, output_tokens=300,
            cache_creation_input_tokens=0, cache_read_input_tokens=0,
        ),
    )
    client = SimpleNamespace(kwargs=None)

    def create(**kwargs):
        client.kwargs = kwargs
        return response

    client.messages = SimpleNamespace(create=create)
    return client


def docx_texts(data: bytes) -> list[str]:
    import re

    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    return [t for t in re.findall(r"<w:t[^>]*>([^<]*)</w:t>", xml)]


class TestSoufujo:
    def test_docx_is_built_and_attached(self, tmp_path):
        client = fake_client(
            {
                "template_id": "書類送付状_ライズ",
                "date": "2026年8月17日",
                "to_lines": ["株式会社大塚商会", "販売２課　濵野 康一　様"],
                "staff": "足立",
                "items": [
                    {"name": "業務委託基本契約書", "qty": "2部"},
                    {"name": "返信用封筒", "qty": "1枚"},
                ],
                "missing": [],
                "opening": "承知しました。送付状の下書きを作りますね。",
            }
        )
        runner = DocBuildRunner(make_config(tmp_path), client=client)
        reply, meta, usage = runner.run("大塚商会あての送付状を作って")

        filename, data = meta["artifact"]
        assert filename == "書類送付状_株式会社大塚商会_20260817.docx"
        texts = docx_texts(data)
        joined = "".join(texts)
        assert "株式会社大塚商会" in texts
        assert "販売２課　濵野 康一　様" in texts  # 宛先は行数ぶん複製される
        assert "■業務委託基本契約書" in joined and "■返信用封筒" in joined
        assert joined.count("■") == 2  # 明細は依頼された件数ぶんだけ
        assert "担当： 経理財務部　足立" in joined
        assert "{{" not in joined  # 差し込み漏れがない
        assert "https://app.box.com/file/1529686391255" in reply  # 出典（ひな形）
        assert "下書き" in reply
        assert usage["input_tokens"] == 1200

    def test_missing_recipient_asks_instead_of_building(self, tmp_path):
        client = fake_client(
            {
                "template_id": "書類送付状_ライズ",
                "date": "2026年8月17日",
                "to_lines": [],
                "staff": "",
                "items": [{"name": "契約書", "qty": "1部"}],
                "missing": [],
                "opening": "送付状ですね。",
            }
        )
        runner = DocBuildRunner(make_config(tmp_path), client=client)
        reply, meta, _ = runner.run("送付状を作って")
        assert "artifact" not in meta  # 宛先を推測で埋めない
        assert "宛先" in reply

    def test_contract_request_returns_guidance_without_api(self, tmp_path):
        client = fake_client({})
        runner = DocBuildRunner(make_config(tmp_path), client=client)
        reply, meta, usage = runner.run("秘密保持契約書を作って")
        assert reply == CONTRACT_GUIDANCE
        assert usage == {}
        assert client.kwargs is None  # API呼び出しなし
        assert "LegalForce" in reply

    def test_template_outside_allowlist_is_refused(self, tmp_path):
        config = make_config(tmp_path)
        config.data["doc_build"]["allowed_templates"] = ["FAX送付状"]
        client = fake_client(
            {
                "template_id": "書類送付状_ライズ",
                "date": "2026年8月17日",
                "to_lines": ["株式会社A"],
                "staff": "",
                "items": [{"name": "契約書", "qty": "1部"}],
                "missing": [],
                "opening": "",
            }
        )
        runner = DocBuildRunner(config, client=client)
        reply, meta, _ = runner.run("送付状を作って")
        assert "artifact" not in meta
        assert "FAX送付状" in reply


class TestFaxSoufujo:
    def test_xlsx_is_built_with_directory_lookup(self, tmp_path):
        client = fake_client(
            {
                "template_id": "FAX送付状",
                "date": "",
                "to_lines": ["三十三銀行　奈良支店", "ロクガワ様"],
                "staff": "足立",
                "items": [],
                "subject": "海外送金添付資料の件",
                "pages": 3,
                "to_fax": "0742-36-1555",
                "to_tel": "0742-36-1333",
                "body_lines": ["お手数をおかけしますが、よろしくお願いいたします。"],
                "missing": [],
                "opening": "FAX送付状ですね、お作りします。",
            }
        )
        runner = DocBuildRunner(make_config(tmp_path), client=client)
        reply, meta, _ = runner.run("三十三銀行あてのFAX送付状を作って")

        filename, data = meta["artifact"]
        assert filename.startswith("FAX送付状_三十三銀行奈良支店_")
        assert filename.endswith(".xlsx")
        openpyxl = pytest.importorskip("openpyxl")
        ws = openpyxl.load_workbook(io.BytesIO(data))["汎用"]
        assert ws["D10"].value == "三十三銀行　奈良支店"
        assert ws["D12"].value == "ロクガワ様"
        assert ws["D14"].value == "0742-36-1555"
        assert ws["E19"].value == "海外送金添付資料の件"
        assert ws["N6"].value == 3
        assert ws["M10"].value == "株式会社ライズクリエイション"  # 自社側は原本のまま
        assert "https://app.box.com/file/1529688805714" in reply

    def test_fax_directory_is_given_to_the_model(self, tmp_path):
        client = fake_client(
            {
                "template_id": "FAX送付状", "date": "", "to_lines": ["三十三銀行"],
                "staff": "", "items": [], "subject": "件名", "missing": [], "opening": "",
            }
        )
        runner = DocBuildRunner(make_config(tmp_path), client=client)
        runner.run("三十三銀行あてのFAX送付状を作って")
        prompt = client.kwargs["messages"][0]["content"]
        assert "0742-36-1555" in prompt  # 台帳のFAX番号を渡している
        assert "推測で作らない" in client.kwargs["system"]


class TestTemplateFill:
    def test_leftover_placeholder_is_an_error(self, tmp_path):
        import pathlib

        data = (pathlib.Path(REPO_TEMPLATES) / "書類送付状_ライズ.docx").read_bytes()
        with pytest.raises(TemplateError):
            render_docx(data, {"date": "2026年8月17日"}, {"to_line": [], "item_name": []})

    def test_unknown_sheet_is_an_error(self, tmp_path):
        import pathlib

        data = (pathlib.Path(REPO_TEMPLATES) / "FAX送付状.xlsx").read_bytes()
        with pytest.raises(TemplateError):
            render_xlsx(data, "存在しないシート", {"D10": "x"})

    def test_xlsx_keeps_form_controls_and_print_settings(self):
        import pathlib

        data = (pathlib.Path(REPO_TEMPLATES) / "FAX送付状.xlsx").read_bytes()
        out = render_xlsx(data, "汎用", {"D10": "株式会社テスト"})
        before = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
        after = set(zipfile.ZipFile(io.BytesIO(out)).namelist())
        # 差分は計算チェーン（Excelが作り直す）だけ。図形・フォームコントロール・
        # 印刷設定を落とさない
        assert before - after == {"xl/calcChain.xml"}
        assert any(n.startswith("xl/ctrlProps/") for n in after)
        assert any(n.startswith("xl/printerSettings/") for n in after)


class TestMultipart:
    def test_japanese_filename_is_encoded_both_ways(self):
        body, content_type = _multipart("書類送付状_南都銀行.docx", b"data", "本文です")
        assert content_type.startswith("multipart/form-data; boundary=")
        text = body.decode("utf-8", "replace")
        assert "filename*=UTF-8''%E6%9B%B8%E9%A1%9E" in text  # 日本語名の文字化け対策
        assert 'filename="' in text  # ASCII代替名も併記
        assert 'name="message"' in text and "本文です" in text
        assert body.endswith(b"--\r\n")


class FakeDocBuild:
    """依頼文だけ受け取り、決まった成果物を返すひな形差し込み役。"""

    def __init__(self, artifact=("送付状.docx", b"WORD")):
        self.calls = []
        self._artifact = artifact

    def run(self, instruction, context="", requester_name=""):
        self.calls.append(
            {"instruction": instruction, "context": context, "requester": requester_name}
        )
        meta = {"template": "書類送付状_ライズ"}
        if self._artifact:
            meta["artifact"] = self._artifact
            meta["output_filename"] = self._artifact[0]
        return "下書きです。", meta, {"input_tokens": 100, "output_tokens": 50}


class FakeChatworkWithUpload:
    def __init__(self):
        self.sent = []
        self.uploads = []

    def send_message(self, room_id, body):
        self.sent.append((room_id, body))
        return "1"

    def upload_file(self, room_id, filename, data, message=""):
        self.uploads.append((room_id, filename, data, message))
        return "777"

    def get_recent_messages(self, room_id, limit=20):
        return [
            {"message_id": "1", "account": {"account_id": 111, "name": "坂田"}, "body": "前の会話"},
        ]


def build_handler(tmp_path, monkeypatch, body, doc_task=None, doc_build=None):
    from tests.test_handler import TOKEN, FakeAudit, FakeGenerator, sign
    from raizuinu.answer import Answer
    from raizuinu.handbook import HandbookLoader
    from raizuinu.handler import RaizuinuHandler

    monkeypatch.setenv("CHATWORK_WEBHOOK_TOKEN", TOKEN)
    (tmp_path / "銀行明細取得.md").write_text("# 銀行明細取得\n手順\n", encoding="utf-8")
    config = Config.load(tmp_path / "no-config.json")
    config.data["allowed_room_ids"] = [12345]
    config.data["webhook_async"] = False
    config.data["state_dir"] = str(tmp_path / "state")
    config.data["audit_log_dir"] = str(tmp_path / "logs")
    config.base_dir = tmp_path

    chatwork = FakeChatworkWithUpload()
    audit = FakeAudit()
    handler = RaizuinuHandler(
        config,
        chatwork=chatwork,
        generator=FakeGenerator(Answer(has_answer=True, text="回答", sources=[], usage={})),
        audit=audit,
        handbook_loader=HandbookLoader([tmp_path], ["*.md"], [], 300),
        doc_task=doc_task,
        doc_build=doc_build,
    )
    raw = json.dumps(
        {
            "webhook_event_type": "mention_to_me",
            "webhook_event": {
                "from_account_id": 111, "to_account_id": 999, "room_id": 12345,
                "message_id": "300001", "body": body, "send_time": 1700000000,
            },
        }
    ).encode()
    return handler, chatwork, audit, raw, sign(raw)


class TestHandlerIntegration:
    def test_build_request_is_uploaded_and_audited(self, tmp_path, monkeypatch):
        doc_build = FakeDocBuild()
        handler, chatwork, audit, raw, signature = build_handler(
            tmp_path, monkeypatch, "[To:999] 南都銀行あての書類送付状を作って", doc_build=doc_build
        )
        result = handler.handle_webhook(raw, signature)
        assert result.status == 200
        assert chatwork.uploads and not chatwork.sent  # 添付1件で返す（本文は同送）
        room_id, filename, data, message = chatwork.uploads[0]
        assert (room_id, filename, data) == (12345, "送付状.docx", b"WORD")
        assert message.startswith("[rp aid=111 to=12345-300001]")
        assert "下書きです。" in message
        assert doc_build.calls[0]["requester"] == "坂田"  # 差出人の担当者に使う
        record = audit.records[-1]
        assert record["type"] == "doc_build"
        assert record["uploaded_file_id"] == "777"
        assert record["template"] == "書類送付状_ライズ"

    def test_attached_template_does_not_go_to_doc_task(self, tmp_path, monkeypatch):
        # ひな形を添付して「これで作って」と言われても議事録フローへ吸われない
        class ExplodingDocTask:
            def run(self, *args, **kwargs):
                raise AssertionError("文書タスクへ回してはいけない")

        doc_build = FakeDocBuild()
        body = (
            "[To:999] [download:5555]書類送付状_ライズ.docx (30 KB)[/download] "
            "このひな形で南都銀行あての送付状を作って"
        )
        handler, chatwork, _, raw, signature = build_handler(
            tmp_path, monkeypatch, body, doc_task=ExplodingDocTask(), doc_build=doc_build
        )
        handler.handle_webhook(raw, signature)
        assert chatwork.uploads and doc_build.calls

    def test_question_still_goes_to_qa(self, tmp_path, monkeypatch):
        doc_build = FakeDocBuild()
        handler, chatwork, audit, raw, signature = build_handler(
            tmp_path, monkeypatch, "[To:999] 送付状の書き方を教えて", doc_build=doc_build
        )
        handler.handle_webhook(raw, signature)
        assert not doc_build.calls  # 作成依頼ではないのでQ&Aへ
        assert chatwork.sent

    def test_guidance_only_reply_is_not_uploaded(self, tmp_path, monkeypatch):
        doc_build = FakeDocBuild(artifact=None)
        handler, chatwork, audit, raw, signature = build_handler(
            tmp_path, monkeypatch, "[To:999] 送付状を作って", doc_build=doc_build
        )
        handler.handle_webhook(raw, signature)
        assert not chatwork.uploads and chatwork.sent  # 成果物が無いときは本文だけ
        assert audit.records[-1]["type"] == "doc_build_not_ready"
