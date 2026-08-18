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
                "kind": "送付状", "company_id": "ライズクリエイション",
                "date": "2026年8月17日",
                "to_company": "株式会社大塚商会", "to_department": "販売２課", "to_person": "濵野 康一",
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
        reply, meta, usage = runner.run("大塚商会あての送付状を作って", requester_name="足立 海里")

        filename, data = meta["artifact"]
        assert filename == "書類送付状_株式会社大塚商会_20260817.docx"
        texts = docx_texts(data)
        joined = "".join(texts)
        # 1行目に会社名、2行目に部署と担当者（1字下げ）
        assert texts.index("株式会社大塚商会") + 1 == texts.index("　販売２課　濵野 康一様")
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
                "kind": "送付状", "company_id": "ライズクリエイション",
                "date": "2026年8月17日",
                "to_company": "",
                "staff": "",
                "items": [{"name": "契約書", "qty": "1部"}],
                "missing": [],
                "opening": "送付状ですね。",
            }
        )
        runner = DocBuildRunner(make_config(tmp_path), client=client)
        reply, meta, _ = runner.run("送付状を作って", requester_name="足立 海里")
        assert "artifact" not in meta  # 宛先を推測で埋めない
        assert "宛先" in reply

    def test_contract_request_returns_guidance_without_api(self, tmp_path):
        client = fake_client({})
        runner = DocBuildRunner(make_config(tmp_path), client=client)
        reply, meta, usage = runner.run("秘密保持契約書を作って", requester_name="足立 海里")
        assert reply == CONTRACT_GUIDANCE
        assert usage == {}
        assert client.kwargs is None  # API呼び出しなし
        assert "LegalForce" in reply

    def test_unknown_sender_company_is_asked_back_not_substituted(self, tmp_path):
        # 未登録の会社名で頼まれたとき、既定の会社で黙って作ると
        # 差出人が別会社の書類が出来上がる。作らずに聞き返す
        client = fake_client(
            {
                "kind": "送付状", "company_id": "",
                "sender_hint": "株式会社まだ登録していない商事",
                "date": "2026年8月17日",
                "to_company": "株式会社A",
                "staff": "",
                "items": [{"name": "契約書", "qty": "1部"}],
                "missing": [],
                "opening": "",
            }
        )
        runner = DocBuildRunner(make_config(tmp_path), client=client)
        reply, meta, _ = runner.run("イノベイト名義で株式会社Aあての送付状を作って", requester_name="足立 海里")
        assert "artifact" not in meta
        assert meta["error"] == "unknown_company"
        assert "株式会社イノベイト" in reply
        assert "株式会社ライズクリエイション" in reply  # 使える会社を示す

    def test_sender_company_switches_the_letterhead(self, tmp_path):
        client = fake_client(
            {
                "kind": "送付状", "company_id": "RAKUTENKEN",
                "sender_hint": "楽天軒",
                "date": "2026年8月17日",
                "to_company": "株式会社A",
                "staff": "足立",
                "items": [{"name": "契約書", "qty": "1部"}],
                "missing": [],
                "opening": "",
            }
        )
        runner = DocBuildRunner(make_config(tmp_path), client=client)
        reply, meta, _ = runner.run("楽天軒名義で株式会社Aあての送付状を作って", requester_name="足立 海里")
        joined = "".join(docx_texts(meta["artifact"][1]))
        assert "ＲＡＫＵＴＥＮＫＥＮ株式会社" in joined
        assert "0742-81-4930" in joined  # 会社ごとのTEL
        assert "株式会社ライズクリエイション" not in joined
        assert "{{" not in joined
        assert "ＲＡＫＵＴＥＮＫＥＮ株式会社" in reply  # どの会社名義かを返信に書く
        # 出典は会社ごとのBox原本を指す（ライズの原本を指すと突き合わせできない）
        assert "https://app.box.com/file/1681180574076" in reply


class TestRecipientLayout:
    """宛先は1行目に会社名（正式名称）、2行目に支店・部署・担当者。"""

    @pytest.mark.parametrize(
        "parts,expected",
        [
            (
                {"company": "南都銀行", "branch": "奈良支店"},
                ["南都銀行", "奈良支店　ご担当者様"],
            ),
            (
                {"company": "南都銀行", "branch": "奈良支店", "department": "営業課", "person": "田中"},
                ["南都銀行", "奈良支店　営業課　田中様"],
            ),
            (
                {"company": "株式会社ライズ", "branch": "大阪支店", "department": "営業第一課", "person": "木村"},
                ["株式会社ライズ", "大阪支店　営業第一課　木村様"],
            ),
            # 支店も部署も担当者も無ければ、会社名に御中を付けた1行だけ
            ({"company": "株式会社大塚商会"}, ["株式会社大塚商会　御中"]),
            # 個人あて（会社名なし）
            ({"person": "濵野 康一"}, ["濵野 康一様"]),
            ({}, []),
        ],
    )
    def test_two_line_layout(self, parts, expected):
        from raizuinu.docbuild import build_recipient

        assert build_recipient(parts) == expected

    def test_honorific_in_the_request_is_not_doubled(self):
        from raizuinu.docbuild import build_recipient

        assert build_recipient({"company": "株式会社A", "person": "田中 様"}) == [
            "株式会社A",
            "田中様",
        ]

    @pytest.mark.parametrize(
        "written,expected",
        [
            ("㈱ライズクリエイション", "株式会社ライズクリエイション"),
            ("ライズクリエイション㈱", "ライズクリエイション株式会社"),
            ("(株)大塚商会", "株式会社大塚商会"),
            ("（株）大塚商会", "株式会社大塚商会"),
            ("㈲山田商店", "有限会社山田商店"),
            ("（有）山田商店", "有限会社山田商店"),
            ("合同会社ohirome", "合同会社ohirome"),  # 正式名称はそのまま
            ("南都銀行", "南都銀行"),  # 法人格が書かれていなければ足さない
        ],
    )
    def test_abbreviated_legal_forms_are_spelled_out(self, written, expected):
        from raizuinu.docbuild import expand_legal_form

        assert expand_legal_form(written) == expected

    def test_the_document_puts_the_company_on_its_own_line(self, tmp_path):
        client = fake_client(
            {
                "kind": "送付状", "company_id": "ライズクリエイション", "sender_hint": "",
                "date": "2026年8月18日",
                "to_company": "㈱大塚商会", "to_branch": "大阪南支店",
                "to_department": "販売２課", "to_person": "木村",
                "items": [{"name": "契約書", "qty": "1部"}], "missing": [], "opening": "",
            }
        )
        runner = DocBuildRunner(make_config(tmp_path), client=client)
        _, meta, _ = runner.run("大塚商会あての送付状を作って", requester_name="足立 海里")
        texts = docx_texts(meta["artifact"][1])
        head = texts.index("株式会社大塚商会")  # ㈱ は正式名称に直る
        assert texts[head + 1] == "　大阪南支店　販売２課　木村様"

    def test_a_company_with_no_contact_gets_onchu_on_one_line(self, tmp_path):
        client = fake_client(
            {
                "kind": "送付状", "company_id": "ライズクリエイション", "sender_hint": "",
                "date": "2026年8月18日", "to_company": "株式会社大塚商会",
                "items": [{"name": "契約書", "qty": "1部"}], "missing": [], "opening": "",
            }
        )
        runner = DocBuildRunner(make_config(tmp_path), client=client)
        _, meta, _ = runner.run("大塚商会あての送付状を作って", requester_name="足立 海里")
        texts = docx_texts(meta["artifact"][1])
        assert "株式会社大塚商会　御中" in texts
        assert not any(t.startswith("　") and "様" in t for t in texts)


class TestCompanyDirectory:
    def build(self, tmp_path, company_id, hint=""):
        client = fake_client(
            {
                "kind": "送付状", "company_id": company_id, "sender_hint": hint,
                "date": "2026年8月18日", "to_company": "株式会社A", "staff": "足立",
                "items": [{"name": "契約書", "qty": "1部"}], "missing": [], "opening": "",
            }
        )
        runner = DocBuildRunner(make_config(tmp_path), client=client)
        _, meta, _ = runner.run(f"{hint or company_id}名義で送付状を作って", requester_name="足立 海里")
        return docx_texts(meta["artifact"][1])

    def test_company_without_a_phone_number_loses_the_tel_line(self, tmp_path):
        # 空欄のまま残すと「TEL：」だけの行が印字された送付状になる
        texts = self.build(tmp_path, "ライズホールディングス")
        assert "株式会社ライズホールディングス" in texts
        assert not any(t.startswith("TEL：") for t in texts)
        assert "コトモール2階" in texts

    def test_single_line_address_loses_the_second_line(self, tmp_path):
        texts = self.build(tmp_path, "イノベイト")
        assert "奈良市大安寺西３丁目８番地１４号" in texts
        assert "TEL：0742-81-3395" in texts
        assert "コトモール2階" not in texts  # ライズの住所2行目を引きずらない

    @pytest.mark.parametrize(
        "hint,expected",
        [
            ("ライズ", "株式会社ライズクリエイション"),
            ("ライズホールディングス", "株式会社ライズホールディングス"),
            ("ライズHD", "株式会社ライズホールディングス"),
            ("ヤマト", "株式会社ヤマトライジング"),
            ("楽天軒", "ＲＡＫＵＴＥＮＫＥＮ株式会社"),
            ("rakutenken", "ＲＡＫＵＴＥＮＫＥＮ株式会社"),  # 全角/半角・大小のゆれ
            ("OHIROME", "合同会社ohirome"),
            ("株式会社イノベイト", "株式会社イノベイト"),
        ],
    )
    def test_similar_company_names_are_not_confused(self, tmp_path, hint, expected):
        # 「ライズ」は「ライズホールディングス」にも含まれる。先に見つかったほうを
        # 採ると、持株会社あての依頼が事業会社名義で出来上がってしまう
        runner = DocBuildRunner(make_config(tmp_path), client=fake_client({}))
        assert runner._find_company("", hint)["name"] == expected

    def test_unregistered_company_stays_unmatched(self, tmp_path):
        runner = DocBuildRunner(make_config(tmp_path), client=fake_client({}))
        assert runner._find_company("", "株式会社大塚商会") is None

    def test_the_named_company_wins_over_the_models_guess(self, tmp_path):
        # 「ライズ」はライズクリエイションを指す。モデルが持株会社と取り違えても、
        # 依頼文に書かれていた社名のほうを採る
        runner = DocBuildRunner(make_config(tmp_path), client=fake_client({}))
        found = runner._find_company("ライズホールディングス", "ライズ")
        assert found["name"] == "株式会社ライズクリエイション"

    def test_naming_rule_is_given_to_the_model(self, tmp_path):
        client = fake_client(
            {
                "kind": "送付状", "company_id": "ライズクリエイション", "sender_hint": "",
                "date": "", "to_company": "", "staff": "", "items": [],
                "missing": [], "opening": "",
            }
        )
        DocBuildRunner(make_config(tmp_path), client=client).run("送付状を作って", requester_name="足立 海里")
        prompt = client.kwargs["messages"][0]["content"]
        assert "「ライズ」とだけ書かれていればこの会社" in prompt


class TestFaxSoufujo:
    def test_xlsx_is_built_with_directory_lookup(self, tmp_path):
        client = fake_client(
            {
                "kind": "FAX送付状", "company_id": "ライズクリエイション",
                "date": "",
                "to_company": "三十三銀行", "to_branch": "奈良支店", "to_person": "ロクガワ",
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
        reply, meta, _ = runner.run("三十三銀行あてのFAX送付状を作って", requester_name="足立 海里")

        filename, data = meta["artifact"]
        assert filename.startswith("FAX送付状_三十三銀行_")
        assert filename.endswith(".xlsx")
        openpyxl = pytest.importorskip("openpyxl")
        ws = openpyxl.load_workbook(io.BytesIO(data))["汎用"]
        assert ws["D10"].value == "三十三銀行"  # 会社名の欄には社名だけ
        assert ws["D12"].value == "奈良支店　ロクガワ様"  # 支店・担当者は担当者欄へ
        assert ws["D14"].value == "0742-36-1555"
        assert ws["E19"].value == "海外送金添付資料の件"
        assert ws["N6"].value == 3
        assert ws["M10"].value == "株式会社ライズクリエイション"  # 発信者欄
        assert ws["M12"].value == "経理財務部"
        assert ws["M14"].value == "0742-90-1051"
        assert ws["M16"].value == "0742-81-9671"
        assert "https://app.box.com/file/1529688805714" in reply

    def test_sender_company_switches_the_fax_header(self, tmp_path):
        client = fake_client(
            {
                "kind": "FAX送付状", "company_id": "RAKUTENKEN", "sender_hint": "楽天軒",
                "date": "", "to_company": "株式会社A", "staff": "足立",
                "items": [], "subject": "件名", "missing": [], "opening": "",
            }
        )
        runner = DocBuildRunner(make_config(tmp_path), client=client)
        _, meta, _ = runner.run("楽天軒名義で株式会社AあてのFAX送付状を作って", requester_name="足立 海里")
        openpyxl = pytest.importorskip("openpyxl")
        ws = openpyxl.load_workbook(io.BytesIO(meta["artifact"][1]))["汎用"]
        assert ws["M10"].value == "ＲＡＫＵＴＥＮＫＥＮ株式会社"
        assert ws["M14"].value == "0742-81-4931"  # 会社ごとのFAX番号
        assert ws["M16"].value == "0742-81-4930"
        assert ws["M12"].value in (None, "")  # 部署名を持たない会社は空欄

    def test_fax_directory_is_given_to_the_model(self, tmp_path):
        client = fake_client(
            {
                "kind": "FAX送付状", "company_id": "ライズクリエイション", "date": "", "to_company": "三十三銀行",
                "staff": "", "items": [], "subject": "件名", "missing": [], "opening": "",
            }
        )
        runner = DocBuildRunner(make_config(tmp_path), client=client)
        runner.run("三十三銀行あてのFAX送付状を作って", requester_name="足立 海里")
        prompt = client.kwargs["messages"][0]["content"]
        assert "0742-36-1555" in prompt  # 台帳のFAX番号を渡している
        assert "推測で作らない" in client.kwargs["system"]


class TestTemplateFill:
    def test_leftover_placeholder_is_an_error(self, tmp_path):
        import pathlib

        data = (pathlib.Path(REPO_TEMPLATES) / "送付状_標準.docx").read_bytes()
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
        # 図形・フォームコントロール・印刷設定・計算チェーンを1つも落とさない
        assert before == after
        assert any(n.startswith("xl/ctrlProps/") for n in after)
        assert any(n.startswith("xl/printerSettings/") for n in after)

    def test_overwriting_a_formula_drops_only_its_calcchain_entry(self):
        import pathlib

        data = (pathlib.Path(REPO_TEMPLATES) / "FAX送付状.xlsx").read_bytes()
        chain_before = zipfile.ZipFile(io.BytesIO(data)).read("xl/calcChain.xml").decode()
        assert '<c r="E6"' in chain_before  # =TODAY() が登録されている
        out = render_xlsx(data, "汎用", {"E6": "2026年9月1日"})
        z = zipfile.ZipFile(io.BytesIO(out))
        # 消した数式が計算チェーンに残るとExcelが壊れたファイルとして扱う。
        # 中身が空になる場合は、参照ごと外さないと今度は空タグが不正になる
        assert "xl/calcChain.xml" not in z.namelist()
        assert "calcChain" not in z.read("[Content_Types].xml").decode()
        assert "calcChain" not in z.read("xl/_rels/workbook.xml.rels").decode()


class TestMultipart:
    def test_japanese_filename_is_encoded_both_ways(self):
        body, content_type = _multipart("書類送付状_南都銀行.docx", b"data", "本文です")
        assert content_type.startswith("multipart/form-data; boundary=")
        text = body.decode("utf-8", "replace")
        assert "filename*=UTF-8''%E6%9B%B8%E9%A1%9E" in text  # RFC 5987の書き方
        assert 'filename="書類送付状_南都銀行.docx"' in text  # 生UTF-8も併記（どちらの解釈でも読める）
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


class TestReviewRegressions:
    """レビューで再現した欠陥。同じ壊れ方を繰り返さないための固定。"""

    def test_honorific_is_not_doubled_on_fax(self, tmp_path):
        # 「汎用」シートは会社名(D10)と敬称(H10)が別欄。敬称込みで入れると二重になる
        client = fake_client(
            {
                "kind": "FAX送付状", "company_id": "ライズクリエイション", "date": "", "to_company": "株式会社大塚商会",
                "staff": "坂田", "items": [], "subject": "請求書送付の件",
                "missing": [], "opening": "",
            }
        )
        runner = DocBuildRunner(make_config(tmp_path), client=client)
        _, meta, _ = runner.run("大塚商会あてのFAX送付状を作って", requester_name="足立 海里")
        openpyxl = pytest.importorskip("openpyxl")
        ws = openpyxl.load_workbook(io.BytesIO(meta["artifact"][1]))["汎用"]
        assert ws["D10"].value == "株式会社大塚商会"
        assert ws["H10"].value == "御中"
        assert "御中" not in meta["artifact"][0]  # ファイル名にも混ぜない

    def test_person_recipient_gets_sama(self, tmp_path):
        client = fake_client(
            {
                "kind": "FAX送付状", "company_id": "ライズクリエイション", "date": "", "to_person": "濵野 康一",
                "staff": "坂田", "items": [], "subject": "件名", "missing": [], "opening": "",
            }
        )
        runner = DocBuildRunner(make_config(tmp_path), client=client)
        _, meta, _ = runner.run("濵野様あてのFAX送付状を作って", requester_name="足立 海里")
        openpyxl = pytest.importorskip("openpyxl")
        ws = openpyxl.load_workbook(io.BytesIO(meta["artifact"][1]))["汎用"]
        assert (ws["D10"].value, ws["H10"].value) == ("濵野 康一", "様")

    @pytest.mark.parametrize(
        "requester,expected", [("足立 海里", "足立"), ("篠田", "篠田"), ("岩永　※9月入社", "岩永")]
    )
    def test_sender_staff_follows_the_requester_not_a_fixed_name(
        self, tmp_path, requester, expected
    ):
        # 誰が依頼しても同じ名前が入る（ひな形の既定名のまま出る）事故を防ぐ
        client = fake_client(
            {
                "kind": "FAX送付状", "company_id": "ライズクリエイション", "date": "", "to_company": "株式会社A",
                "items": [], "subject": "件名", "missing": [], "opening": "",
            }
        )
        runner = DocBuildRunner(make_config(tmp_path), client=client)
        _, meta, _ = runner.run("株式会社AあてのFAX送付状を作って", requester_name=requester)
        openpyxl = pytest.importorskip("openpyxl")
        ws = openpyxl.load_workbook(io.BytesIO(meta["artifact"][1]))["汎用"]
        assert ws["O12"].value == expected

    def test_template_carries_no_other_companies(self):
        # 1社あての下書きに他社の連絡先が付いてこないこと
        import json
        import pathlib

        raw = (pathlib.Path(REPO_TEMPLATES) / "FAX送付状.xlsx").read_bytes()
        blob = raw.decode("latin-1", "ignore")
        directory = json.loads(
            (pathlib.Path(REPO_TEMPLATES) / "fax_destinations.json").read_text(encoding="utf-8")
        )
        openpyxl = pytest.importorskip("openpyxl")
        # 残すのは白紙の「汎用」と、発信者ドロップダウンの参照先だけ
        assert openpyxl.load_workbook(io.BytesIO(raw)).sheetnames == ["プルダウンリスト", "汎用"]
        for entry in directory:
            for key in ("company", "person", "fax"):
                if entry.get(key):
                    assert entry[key] not in blob

    def test_templates_ship_without_any_sender_company_baked_in(self):
        # 差出人はグループ会社ごとに差し替える。ひな形に特定の会社を焼き付けたままだと、
        # 差し込みを取りこぼしたときに別会社名義の書類が出来上がってしまう
        import json
        import pathlib

        companies = json.loads(
            (pathlib.Path(REPO_TEMPLATES) / "companies.json").read_text(encoding="utf-8")
        )["companies"]
        openpyxl = pytest.importorskip("openpyxl")
        ws = openpyxl.load_workbook(pathlib.Path(REPO_TEMPLATES) / "FAX送付状.xlsx")["汎用"]
        for ref in ("M10", "M12", "M14", "M16", "O12"):
            assert ws[ref].value in (None, "")
        for name in ("送付状_標準.docx", "送付状_楽天軒型.docx"):
            joined = "".join(docx_texts((pathlib.Path(REPO_TEMPLATES) / name).read_bytes()))
            for company in companies:
                for key in ("name", "address1", "tel", "fax"):
                    if company.get(key):
                        assert company[key] not in joined

    def test_blank_recipient_lines_are_asked_for(self, tmp_path):
        # 空白だけの宛先を「あり」と数えると、宛先のない書類が出来てしまう
        client = fake_client(
            {
                "kind": "送付状", "company_id": "ライズクリエイション", "date": "2026年8月17日",
                "to_company": "  ", "staff": "足立",
                "items": [{"name": "", "qty": ""}], "missing": [], "opening": "",
            }
        )
        runner = DocBuildRunner(make_config(tmp_path), client=client)
        reply, meta, _ = runner.run("送付状を作って", requester_name="足立 海里")
        assert "artifact" not in meta
        assert "宛先" in reply and "送付する書類" in reply

    def test_assumed_quantity_is_stated_in_the_reply(self, tmp_path):
        client = fake_client(
            {
                "kind": "送付状", "company_id": "ライズクリエイション", "date": "2026年8月17日",
                "to_company": "株式会社A", "staff": "足立",
                "items": [{"name": "請求書", "qty": ""}, {"name": "契約書", "qty": "2部"}],
                "missing": [], "opening": "承知しました。",
            }
        )
        runner = DocBuildRunner(make_config(tmp_path), client=client)
        reply, meta, _ = runner.run("株式会社Aあての送付状を作って", requester_name="足立 海里")
        assert "「請求書」は1部としています" in reply  # 仮置きを黙って通さない
        assert "契約書" not in reply.split("ひな形:")[0].replace("承知しました。", "")

    def test_usage_is_kept_when_rendering_fails(self, tmp_path):
        from raizuinu.docbuild import DocumentBuildError

        client = fake_client(
            {
                "kind": "送付状", "company_id": "ライズクリエイション", "date": "2026年8月17日",
                "to_company": "株式会社A", "staff": "", "items": [{"name": "契約書", "qty": "1部"}],
                "missing": [], "opening": "",
            }
        )
        import pathlib
        import shutil

        config = make_config(tmp_path)
        # 会社台帳はあるがWordのひな形が無い状態にする
        shutil.copy(pathlib.Path(REPO_TEMPLATES) / "companies.json", tmp_path / "companies.json")
        config.data["doc_build"]["templates_dir"] = str(tmp_path)
        runner = DocBuildRunner(config, client=client)
        with pytest.raises((DocumentBuildError, OSError)) as exc:
            runner.run("株式会社Aあての送付状を作って", requester_name="足立 海里")
        if isinstance(exc.value, DocumentBuildError):
            assert exc.value.usage["input_tokens"] == 1200  # 消費済みトークンを捨てない

    @pytest.mark.parametrize(
        "question,body,expected",
        [
            ("ヤマトライジングの送付状お願いします", "", True),
            ("南都銀行あての送り状を作成して", "", True),
            ("いつもの手順で大塚商会あての送付状を作って", "", True),  # 「手順」で打ち消さない
            ("送付状って必要ですか？", "", False),
            ("送付状はどんな時に使いますか", "", False),
            ("送付状は誰が作るんですか", "", False),
            ("添付の送付状を要約してください", "[download:11]送付状.docx[/download]", False),
            ("送付状を要約してほしい", "", True),  # 添付が無ければ作成依頼のまま
        ],
    )
    def test_routing_edges(self, question, body, expected):
        assert looks_like_document_build_request(question, body) is expected

    def test_howto_marker_does_not_match_kotoha(self):
        # 「ことは」「あとは」が「とは」に部分一致して依頼を取りこぼしていた
        from raizuinu.doctask import find_document

        recent = [{"body": "[download:555]会議.docx (10 KB)[/download]"}]
        q = "細かいことは気にしなくていいので、さっきの文字起こしを議事録にまとめて"
        assert find_document(f"[To:999] {q}", recent, q)["files"][0]["file_id"] == 555


class TestReplyOpening:
    """成果物を渡す返信の書き出しが、依頼した側の言い方にならないこと。"""

    @pytest.mark.parametrize(
        "opening",
        [
            "よろしくお願いいたします。",
            "よろしくお願いします",
            "お願いいたします。",
            "ご対応のほどよろしくお願いします。",
            "お手数ですが、ご確認をお願いします。",
            "",
        ],
    )
    def test_asking_style_opening_is_replaced(self, tmp_path, opening):
        client = fake_client(
            {
                "kind": "送付状", "company_id": "ライズクリエイション", "date": "2026年8月17日",
                "to_company": "エムズステップ", "to_person": "南", "staff": "足立",
                "items": [{"name": "倉庫寄託契約書", "qty": "1通"}],
                "missing": [], "opening": opening,
            }
        )
        runner = DocBuildRunner(make_config(tmp_path), client=client)
        reply, _, _ = runner.run("エムズステップ南様あての送付状を作って", requester_name="足立 海里")
        head = reply.split("\n")[0]
        assert "お願い" not in head  # 依頼を受けた側の返事として噛み合わせる
        assert head == "承知しました。下書きを作成しました。"

    def test_delivering_opening_is_kept(self, tmp_path):
        client = fake_client(
            {
                "kind": "送付状", "company_id": "ライズクリエイション", "date": "2026年8月17日",
                "to_company": "エムズステップ", "to_person": "南", "staff": "足立",
                "items": [{"name": "倉庫寄託契約書", "qty": "1通"}],
                "missing": [],
                "opening": "エムズステップ　南様あての送付状ですね。倉庫寄託契約書1通で作成しました。",
            }
        )
        runner = DocBuildRunner(make_config(tmp_path), client=client)
        reply, _, _ = runner.run("エムズステップ南様あての送付状を作って", requester_name="足立 海里")
        assert reply.startswith("エムズステップ　南様あての送付状ですね。")

    def test_ask_back_opening_is_also_guarded(self, tmp_path):
        client = fake_client(
            {
                "kind": "送付状", "company_id": "ライズクリエイション", "date": "2026年8月17日",
                "to_company": "", "staff": "", "items": [],
                "missing": [], "opening": "よろしくお願いいたします。",
            }
        )
        runner = DocBuildRunner(make_config(tmp_path), client=client)
        reply, _, _ = runner.run("送付状を作って", requester_name="足立 海里")
        assert not reply.startswith("よろしく")


class TestSenderStaff:
    """差出人の担当者は、Chatworkで依頼してきた人の苗字にする。"""

    @pytest.mark.parametrize(
        "display_name,expected",
        [
            ("足立 海里", "足立"),
            ("足立　海里", "足立"),
            ("足立 海里　資料作成集中（急ぎ案件のみ対応可）※土日休", "足立"),  # 状況書きを落とす
            ("足立(経理)", "足立"),
            ("足立さん", "足立"),
            ("経理財務部 足立", "足立"),  # 部署が先に来る表示名
            ("株式会社ライズクリエイション 足立", "足立"),
            ("阿部 太郎", "阿部"),  # 「部」で終わる苗字を部署と誤らない
            ("服部 花子", "服部"),
            ("足立海里", "足立海里"),  # 区切りが無ければ切らない（誤った苗字より安全）
            ("", ""),
        ],
    )
    def test_surname_extraction(self, display_name, expected):
        from raizuinu.docbuild import surname

        assert surname(display_name) == expected

    def test_requester_surname_fills_the_staff_field(self, tmp_path):
        client = fake_client(
            {
                "kind": "送付状", "company_id": "ライズクリエイション", "date": "2026年8月17日",
                "to_company": "株式会社A", "staff": "", "items": [{"name": "契約書", "qty": "1部"}],
                "missing": [], "opening": "作成しました。",
            }
        )
        runner = DocBuildRunner(make_config(tmp_path), client=client)
        _, meta, _ = runner.run(
            "株式会社Aあての送付状を作って", requester_name="足立 海里　※土日休"
        )
        assert "担当： 経理財務部　足立" in "".join(docx_texts(meta["artifact"][1]))

    def test_full_name_from_the_model_is_reduced_to_surname(self, tmp_path):
        client = fake_client(
            {
                "kind": "FAX送付状", "company_id": "ライズクリエイション", "date": "", "to_company": "株式会社A",
                "staff": "足立 海里", "items": [], "subject": "件名",
                "missing": [], "opening": "作成しました。",
            }
        )
        runner = DocBuildRunner(make_config(tmp_path), client=client)
        _, meta, _ = runner.run("株式会社AあてのFAX送付状を作って", requester_name="足立 海里")
        openpyxl = pytest.importorskip("openpyxl")
        ws = openpyxl.load_workbook(io.BytesIO(meta["artifact"][1]))["汎用"]
        assert ws["O12"].value == "足立"

    def test_requester_surname_wins_over_any_name_the_model_returns(self, tmp_path):
        # 担当者名は依頼した人の苗字。モデルが宛先側の担当者名を差出人欄へ
        # 回してくることがあるため、依頼者の苗字で確定させる
        client = fake_client(
            {
                "kind": "送付状", "company_id": "ライズクリエイション", "date": "2026年8月17日",
                "to_company": "株式会社A", "to_department": "経理部", "to_person": "坂田", "staff": "坂田",
                "items": [{"name": "契約書", "qty": "1部"}],
                "missing": [], "opening": "作成しました。",
            }
        )
        runner = DocBuildRunner(make_config(tmp_path), client=client)
        _, meta, _ = runner.run(
            "株式会社Aの坂田様あてに送付状を作って", requester_name="足立 海里"
        )
        joined = "".join(docx_texts(meta["artifact"][1]))
        assert "担当： 経理財務部　足立" in joined
        assert "担当： 経理財務部　坂田" not in joined

    @pytest.mark.parametrize(
        "instruction",
        [
            "担当は岩永で株式会社Aあての送付状を作って",
            "岩永名義で株式会社Aあての送付状を作って",
            "岩永さん名義で株式会社Aあての送付状を作って",
            "担当者：岩永　で株式会社Aあての送付状を作って",
            "弊社担当は岩永、株式会社Aあての送付状を作って",
        ],
    )
    def test_named_staff_is_used_when_making_it_on_someone_elses_behalf(
        self, tmp_path, instruction
    ):
        # 代理で作る場面（足立が岩永名義の送付状を用意する）
        client = fake_client(
            {
                "kind": "送付状", "company_id": "ライズクリエイション", "sender_hint": "",
                "date": "2026年8月17日", "to_company": "株式会社A",
                "items": [{"name": "契約書", "qty": "1部"}], "missing": [], "opening": "",
            }
        )
        runner = DocBuildRunner(make_config(tmp_path), client=client)
        reply, meta, _ = runner.run(instruction, requester_name="足立 海里")
        assert "担当： 経理財務部　岩永" in "".join(docx_texts(meta["artifact"][1]))
        assert "担当者は「岩永」で作成しています" in reply  # 本人以外の名前は黙って通さない

    def test_a_name_only_hint_is_not_mistaken_for_an_unregistered_company(self, tmp_path):
        # 「岩永名義で」は担当者の名指し。会社の名指しと同じ言い方になるため、
        # 社名として扱うと未登録の会社を聞き返してしまう
        client = fake_client(
            {
                "kind": "送付状", "company_id": "", "sender_hint": "岩永",
                "date": "2026年8月17日", "to_company": "株式会社A",
                "items": [{"name": "契約書", "qty": "1部"}], "missing": [], "opening": "",
            }
        )
        runner = DocBuildRunner(make_config(tmp_path), client=client)
        _, meta, _ = runner.run("岩永名義で株式会社Aあての送付状を作って", requester_name="足立 海里")
        joined = "".join(docx_texts(meta["artifact"][1]))
        assert "株式会社ライズクリエイション" in joined  # 会社は既定のまま
        assert "担当： 経理財務部　岩永" in joined

    @pytest.mark.parametrize(
        "instruction",
        [
            "株式会社Aの担当は伊藤様です。送付状を作って",
            "株式会社Aの担当は伊藤さん宛てで送付状を作って",
        ],
    )
    def test_the_recipients_contact_is_not_taken_as_our_staff(self, tmp_path, instruction):
        # 敬称が付いていれば先方の担当者。自社の担当者名に敬称は付かない
        client = fake_client(
            {
                "kind": "送付状", "company_id": "ライズクリエイション", "sender_hint": "",
                "date": "2026年8月17日", "to_company": "株式会社A", "to_person": "伊藤",
                "items": [{"name": "契約書", "qty": "1部"}], "missing": [], "opening": "",
            }
        )
        runner = DocBuildRunner(make_config(tmp_path), client=client)
        reply, meta, _ = runner.run(instruction, requester_name="足立 海里")
        assert "担当： 経理財務部　足立" in "".join(docx_texts(meta["artifact"][1]))
        assert "担当者は「" not in reply  # 依頼者本人なので断り書きは出さない

    def test_blank_requester_name_is_asked_for_instead_of_shipping_a_blank_field(
        self, tmp_path
    ):
        # 会話履歴の取得に失敗すると依頼者の表示名が拾えない。担当者欄が空の
        # まま書類を出すと、そのまま送られてしまう
        client = fake_client(
            {
                "kind": "送付状", "company_id": "ライズクリエイション", "date": "2026年8月17日",
                "to_company": "株式会社A", "items": [{"name": "契約書", "qty": "1部"}],
                "missing": [], "opening": "",
            }
        )
        runner = DocBuildRunner(make_config(tmp_path), client=client)
        reply, meta, _ = runner.run("株式会社Aあての送付状を作って", requester_name="")
        assert "artifact" not in meta
        assert meta["error"] == "missing_fields"
        assert "担当者名" in reply

    def test_the_answer_to_that_question_is_picked_up(self, tmp_path):
        # 聞き返しの答え（苗字だけ）が元の依頼に足されて戻ってくる
        client = fake_client(
            {
                "kind": "送付状", "company_id": "ライズクリエイション", "date": "2026年8月17日",
                "to_company": "株式会社A", "items": [{"name": "契約書", "qty": "1部"}],
                "missing": [], "opening": "作成しました。",
            }
        )
        runner = DocBuildRunner(make_config(tmp_path), client=client)
        _, meta, _ = runner.run(
            "株式会社Aあての送付状を作って\n担当は篠田", requester_name=""
        )
        assert "担当： 経理財務部　篠田" in "".join(docx_texts(meta["artifact"][1]))

    def test_a_recipient_name_matching_the_roster_is_not_taken_as_the_sender(
        self, tmp_path
    ):
        # 「坂田商事」は宛先。名簿と字が重なるだけで差出人担当者にしない
        client = fake_client(
            {
                "kind": "送付状", "company_id": "ライズクリエイション", "date": "2026年8月17日",
                "to_company": "坂田商事株式会社", "items": [{"name": "契約書", "qty": "1部"}],
                "missing": [], "opening": "",
            }
        )
        runner = DocBuildRunner(make_config(tmp_path), client=client)
        reply, meta, _ = runner.run("坂田商事あての送付状を作って", requester_name="")
        assert "artifact" not in meta
        assert "担当者名" in reply


class TestStaffRoster:
    """FAX送付状ひな形のプルダウンから作った社内名簿。"""

    def test_dropdown_sheet_is_kept_so_references_are_not_dangling(self):
        # 発信者の会社名・担当者のドロップダウンが参照するシートを消すと、
        # Excelで参照先を失う
        import pathlib
        import re

        z = zipfile.ZipFile(pathlib.Path(REPO_TEMPLATES) / "FAX送付状.xlsx")
        openpyxl = pytest.importorskip("openpyxl")
        names = openpyxl.load_workbook(io.BytesIO(z.read("xl/worksheets/sheet3.xml")) if False
                                       else pathlib.Path(REPO_TEMPLATES) / "FAX送付状.xlsx").sheetnames
        sheet = z.read("xl/worksheets/sheet3.xml").decode("utf-8")
        for formula in re.findall(r"<xm:f>(.*?)</xm:f>", sheet):
            referenced = formula.split("!")[0].strip("'")
            assert referenced in names

    def test_roster_is_bundled(self):
        import json
        import pathlib

        roster = json.loads(
            (pathlib.Path(REPO_TEMPLATES) / "staff_roster.json").read_text(encoding="utf-8")
        )
        assert "足立" in roster["staff"] and "坂田" in roster["staff"]
        assert "株式会社ライズクリエイション" in roster["companies"]

    def test_roster_splits_names_without_a_separator(self, tmp_path):
        from raizuinu.docbuild import surname

        roster = ("足立", "坂田", "進地")
        assert surname("足立海里", roster) == "足立"  # 区切りが無くても名簿で切れる
        assert surname("進地花子", roster) == "進地"
        assert surname("山田太郎", roster) == "山田太郎"  # 名簿に無ければ切らない

    def test_runner_uses_the_bundled_roster(self, tmp_path):
        client = fake_client(
            {
                "kind": "送付状", "company_id": "ライズクリエイション", "date": "2026年8月17日",
                "to_company": "株式会社A", "staff": "", "items": [{"name": "契約書", "qty": "1部"}],
                "missing": [], "opening": "作成しました。",
            }
        )
        runner = DocBuildRunner(make_config(tmp_path), client=client)
        _, meta, _ = runner.run("株式会社Aあての送付状を作って", requester_name="足立海里")
        assert "担当： 経理財務部　足立" in "".join(docx_texts(meta["artifact"][1]))


class TestRosterMaintenance:
    """名簿の増減が、JSONとExcelのドロップダウンの両方へ反映されること。"""

    def _roster(self):
        import json
        import pathlib

        return json.loads(
            (pathlib.Path(REPO_TEMPLATES) / "staff_roster.json").read_text(encoding="utf-8")
        )["staff"]

    def test_leavers_are_removed(self):
        for name in ("倉本", "進地", "坂口"):  # 退職・他部署
            assert name not in self._roster()

    def test_new_member_is_added(self):
        assert "岩永" in self._roster()  # 2026-09-09入社

    def test_dropdown_matches_the_roster(self):
        import pathlib

        openpyxl = pytest.importorskip("openpyxl")
        pull = openpyxl.load_workbook(pathlib.Path(REPO_TEMPLATES) / "FAX送付状.xlsx")[
            "プルダウンリスト"
        ]
        # 列全体を参照するドロップダウンなので、余った行は空でなければならない
        column = [pull[f"B{row}"].value for row in range(1, 11)]
        assert [v for v in column if v] == self._roster()
        assert all(v is None for v in column[len(self._roster()) :])

    def test_new_member_surname_can_be_split(self):
        from raizuinu.docbuild import surname

        assert surname("岩永太郎", tuple(self._roster())) == "岩永"


class TestAskBackContinuation:
    """聞き返しへの答えを、元の依頼に足して同じフローへ戻す。

    実地で「宛先は『大阪市梅田市税事務所』…」という返信が書類作成へ戻らず、
    Q&Aに落ちてファイルが作られない事故が起きた。書類名を含まない返信でも
    続きとして扱えるようにする。
    """

    def build(self, tmp_path, monkeypatch, doc_build):
        from tests.test_guest import FakeChatwork, make_handler as _unused  # noqa: F401
        from raizuinu.answer import Answer
        from raizuinu.handbook import HandbookLoader
        from raizuinu.handler import RaizuinuHandler
        from tests.test_handler import TOKEN, FakeAudit, FakeGenerator

        monkeypatch.setenv("CHATWORK_WEBHOOK_TOKEN", TOKEN)
        (tmp_path / "銀行明細取得.md").write_text("# 手順\n", encoding="utf-8")
        config = Config.load(tmp_path / "no-config.json")
        config.data["allowed_room_ids"] = [12345]
        config.data["webhook_async"] = False
        config.data["state_dir"] = str(tmp_path / "state")
        config.data["audit_log_dir"] = str(tmp_path / "logs")
        config.base_dir = tmp_path
        chatwork = FakeChatworkWithUpload()
        generator = FakeGenerator(Answer(has_answer=True, text="回答", sources=[], usage={}))
        handler = RaizuinuHandler(
            config, chatwork=chatwork, generator=generator, audit=FakeAudit(),
            handbook_loader=HandbookLoader([tmp_path], ["*.md"], [], 300),
            doc_build=doc_build,
        )
        return handler, chatwork, generator

    def send(self, handler, body, message_id, send_time):
        from tests.test_handler import sign

        raw = json.dumps(
            {
                "webhook_event_type": "mention_to_me",
                "webhook_event": {
                    "from_account_id": 111, "to_account_id": 999, "room_id": 12345,
                    "message_id": message_id, "body": body, "send_time": send_time,
                },
            }
        ).encode()
        return handler.handle_webhook(raw, sign(raw))

    def test_followup_without_the_document_name_still_builds(self, tmp_path, monkeypatch):
        class TwoStep:
            def __init__(self):
                self.seen = []

            def run(self, instruction, context="", requester_name=""):
                self.seen.append(instruction)
                if "梅田市税事務所" not in instruction:
                    return "宛先を教えてください", {"error": "missing_fields"}, {}
                return (
                    "作成しました。",
                    {"template": "書類送付状_ヤマトライジング",
                     "output_filename": "書類送付状.docx",
                     "artifact": ("書類送付状.docx", b"WORD")},
                    {},
                )

        runner = TwoStep()
        handler, chatwork, generator = self.build(tmp_path, monkeypatch, runner)

        self.send(handler, "[To:999] ヤマトライジング名で書類送付状を作成してほしい。", "1", 1700000000)
        assert not chatwork.uploads  # 1通目は聞き返し

        # 「送付状」を含まない返信でも、続きとして書類作成へ戻る
        self.send(handler, "[To:999] 宛先は「大阪市梅田市税事務所」で担当は「納税担当」。", "2", 1700000180)
        assert chatwork.uploads, "続きの返信で書類が作られていない"
        assert not generator.calls, "Q&Aへ落ちてはいけない"
        assert "梅田市税事務所" in runner.seen[1]
        assert "書類送付状を作成してほしい" in runner.seen[1]  # 元の依頼が保たれている

    def test_unrelated_question_after_an_ask_back_is_not_merged(self, tmp_path, monkeypatch):
        class AlwaysAsks:
            def __init__(self):
                self.seen = []

            def run(self, instruction, context="", requester_name=""):
                self.seen.append(instruction)
                return "宛先を教えてください", {"error": "missing_fields"}, {}

        runner = AlwaysAsks()
        handler, chatwork, generator = self.build(tmp_path, monkeypatch, runner)
        self.send(handler, "[To:999] 送付状を作って", "1", 1700000000)
        # 予定の話は別件。書類作成に足さない
        self.send(handler, "[To:999] 足立さんの今日の予定教えて", "2", 1700000060)
        assert len(runner.seen) == 1


class TestAskBackWording:
    """聞き返しの時点では何も作っていない。作った体で書かない。"""

    @pytest.mark.parametrize(
        "opening",
        [
            "承知しました。ヤマトライジング名の書類送付状で作成しましたが、宛先が不足しているため空欄にしています。",
            "作りました。",
            "よろしくお願いいたします。",
            "",
        ],
    )
    def test_completion_claims_are_replaced(self, tmp_path, opening):
        client = fake_client(
            {
                "kind": "送付状", "company_id": "ヤマトライジング", "date": "2026年8月18日",
                "to_company": "", "staff": "足立",
                "items": [{"name": "債権差押通知書", "qty": "一式"}],
                "missing": [], "opening": opening,
            }
        )
        runner = DocBuildRunner(make_config(tmp_path), client=client)
        reply, meta, _ = runner.run("ヤマトライジング名で書類送付状を作って", requester_name="足立 海里")
        assert meta["error"] == "missing_fields"
        assert reply.startswith("書類の下書き、お作りしますね。")
        assert "作成しました" not in reply
        assert "空欄" not in reply
