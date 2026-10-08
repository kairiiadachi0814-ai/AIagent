"""マニュアルリンク集の自動更新と、新しいマニュアルの転記（管理者承認つき）。

指示（2026-10-07）: リンク集の更新があれば該当の .md を自動で更新し、新しいマニュアルが
できたら転記の .md を自動で作る。ただし更新・作成の前に必ず管理者の確認・承認を取り、
その際は成果物そのものを提示する。
"""

import io
import zipfile
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from xml.sax.saxutils import escape

import pytest

from raizuinu.config import Config
from raizuinu.linksync import (
    JST,
    HandbookSync,
    LinkItem,
    known_document_ids,
    parse_links_md,
    parse_workbook,
    render_links_md,
    safe_filename,
    strip_fetch_date,
    summarize_changes,
)

ADMIN_ROOM, ADMIN = 444945031, 6945415
SHEET_ID = "1CTUOjMcUzpTUpy6iP69jvYR1O_-M3lOsCTz6C5Ug2IA"
DOC_A = "1iHOtXMqBd8ljtELkDerO_rpPJAMl4BzR6o9MG_xRzAk"  # 転記済み（ハンドブックに出典あり）
DOC_NEW = "1NEWnewNEWnewNEWnewNEWnewNEWnewNEWnewNEWnew"  # 新しいマニュアル
URL_A = f"https://docs.google.com/document/d/{DOC_A}/edit?tab=t.0"
URL_NEW = f"https://docs.google.com/document/d/{DOC_NEW}/edit"


def build_xlsx(sheets):
    """最小の xlsx。sheets = [(タブ名, {セル参照: 文字}, {セル参照: URL}), ...]"""
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    parts = {}
    sheet_tags, wb_rels = [], []
    for n, (title, cells, links) in enumerate(sheets, 1):
        rows = {}
        for ref, text in cells.items():
            row = int("".join(ch for ch in ref if ch.isdigit()))
            rows.setdefault(row, []).append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(text)}</t></is></c>')
        body = "".join(f'<row r="{r}">{"".join(cs)}</row>' for r, cs in sorted(rows.items()))
        hyper = "".join(f'<hyperlink ref="{ref}" r:id="rIdL{i}"/>' for i, ref in enumerate(links, 1))
        parts[f"xl/worksheets/sheet{n}.xml"] = f'<worksheet {ns}><sheetData>{body}</sheetData><hyperlinks>{hyper}</hyperlinks></worksheet>'
        rels = "".join(
            f'<Relationship Id="rIdL{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="{escape(url)}" TargetMode="External"/>'
            for i, url in enumerate(links.values(), 1)
        )
        parts[f"xl/worksheets/_rels/sheet{n}.xml.rels"] = f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}</Relationships>'
        sheet_tags.append(f'<sheet name="{escape(title)}" sheetId="{n}" r:id="rId{n}"/>')
        wb_rels.append(f'<Relationship Id="rId{n}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{n}.xml"/>')
    parts["xl/workbook.xml"] = f'<workbook {ns}><sheets>{"".join(sheet_tags)}</sheets></workbook>'
    parts["xl/_rels/workbook.xml.rels"] = f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{"".join(wb_rels)}</Relationships>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in parts.items():
            z.writestr(name, data)
    return buf.getvalue()


# 実際の目次シートの形: 1行目=表題、3行目=組の見出し、4行目=列見出し、5行目以降=項目（組ごとに列が並ぶ）
RISE_SHEET = (
    "日常業務",
    {
        "B1": "日常業務マニュアル一覧",
        "B3": "日常", "D3": "最終作業", "F3": "支払関係",
        "B4": "リンク", "C4": "備考", "D4": "完了日", "F4": "リンク", "G4": "備考",
        "B5": "書類格納ルール", "D5": "ー", "F5": "請求書処理１　取得～タスク作成まで",
        "B6": "弥生スマート証憑登録", "C6": "なるべく1週間以内\n遅くとも月内に", "F6": "新しい手順書", "G6": "毎週",
    },
    {"B5": "https://docs.google.com/document/d/1zKRC4X2k9fTM4uVoHlZdkLimM5wt54LvrqYAFny3V9g/edit", "F5": URL_A, "F6": URL_NEW},
)
LOGIN_SHEET = (
    "ログインリンク",
    {
        "B1": "サイト名", "C1": "URL", "D1": "ID", "E1": "パスワード", "F1": "備考",
        "A2": "モール関係", "B2": "Q10", "C2": "https://qsm.qoo10.jp/", "D2": "user01", "E2": "secret", "F2": "担当: 山田",
        "B3": "minne", "C3": "https://minne.com/", "F3": "ID: shop@example.com",
    },
    {},
)


def make_config(tmp_path, collections=None):
    config = Config.load(tmp_path / "no-config.json")
    config.base_dir = tmp_path
    config.data["state_dir"] = str(tmp_path / "state")
    config.data["audit_log_dir"] = str(tmp_path / "logs")
    config.data["admin_room_id"] = ADMIN_ROOM
    config.data["admin_account_ids"] = [ADMIN]
    config.data["handbook"] = {"roots": ["."], "include": ["*.md"], "exclude": ["CLAUDE.md"]}
    config.data["handbook_sync"] = {
        "enabled": True,
        "check_time": "07:30",
        "collections": collections or [
            {"name": "ライズ経理財務", "spreadsheet_id": SHEET_ID, "file": "マニュアルリンク集_ライズ経理財務.md",
             "title": "ライズマニュアルリンク集（経理財務 目次）", "skip_sheets": [], "notes": []},
        ],
        "transcribe": {"enabled": True, "max_per_run": 3, "max_source_chars": 120000},
    }
    return config


class FakeChatwork:
    def __init__(self):
        self.sent, self.uploaded = [], []
        self._next = 9000

    def send_message(self, room_id, body):
        self._next += 1
        self.sent.append((room_id, body, str(self._next)))
        return str(self._next)

    def upload_file(self, room_id, filename, data, message=""):
        self._next += 1
        self.uploaded.append((room_id, filename, data, message, str(self._next)))
        return "file-" + str(self._next)

    def get_me(self):
        return 7777

    def get_recent_messages(self, room_id, limit=20):
        out = [{"message_id": mid, "body": body, "account": {"account_id": 7777}} for _, body, mid in self.sent]
        out += [{"message_id": mid, "body": msg, "account": {"account_id": 7777}} for _, _, _, msg, mid in self.uploaded]
        return sorted(out, key=lambda m: int(m["message_id"]))[-limit:]


def fake_http(xlsx, docs):
    calls = []

    def get(url, timeout=60):
        calls.append(url)
        if "spreadsheets" in url:
            return 200, url, xlsx
        for doc_id, text in docs.items():
            if f"/document/d/{doc_id}/" in url:
                return 200, url, text.encode("utf-8")
        return 404, url, b""

    get.calls = calls
    return get


def fake_client(text="## 手順\n1. 取得する\n2. 保存する\n"):
    client = SimpleNamespace(calls=[])

    def create(**kwargs):
        client.calls.append(kwargs)
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(input_tokens=1000, output_tokens=300, cache_creation_input_tokens=0, cache_read_input_tokens=0),
        )

    client.messages = SimpleNamespace(create=create)
    return client


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


OLD_MD = """# ライズマニュアルリンク集（経理財務 目次）

- 元シート: https://docs.google.com/spreadsheets/d/1CTUOjMcUzpTUpy6iP69jvYR1O_-M3lOsCTz6C5Ug2IA/edit
- 取得日: 2026-08-13（元シート更新時は本ファイルの再取込が必要）
- 本ファイルは目次シートから項目名・URL・備考を自動転記したリンク集。責任者照合: 未実施
- 用途: 「このマニュアル／シートはどこ？」という質問に、該当URLを案内するための索引

## 日常業務

| 項目 | 備考 | URL |
|---|---|---|
| 書類格納ルール |  | https://docs.google.com/document/d/1zKRC4X2k9fTM4uVoHlZdkLimM5wt54LvrqYAFny3V9g/edit |
| 請求書処理１ 取得～タスク作成まで |  | https://docs.google.com/document/d/1iHOtXMqBd8ljtELkDerO_rpPJAMl4BzR6o9MG_xRzAk/edit |
| 弥生スマート証憑登録 | なるべく1週間以内 遅くとも月内に |  |
"""

TRANSCRIBED_MD = f"""# 請求書処理１ 取得～タスク作成まで

- 出典：Googleドキュメント「請求書処理１ 取得～タスク作成まで」
  {URL_A}
- 原本最終更新：2025-08-06
- 転記日：2026-07-17

## 【1】取得
"""


def syncer(tmp_path, now=datetime(2026, 10, 7, 8, 0, tzinfo=JST), docs=None, client=None, sheets=None):
    (tmp_path / "マニュアルリンク集_ライズ経理財務.md").write_text(OLD_MD, encoding="utf-8")
    (tmp_path / "請求書処理1_取得からタスク作成まで.md").write_text(TRANSCRIBED_MD, encoding="utf-8")
    clock = Clock(now)
    s = HandbookSync(
        make_config(tmp_path), FakeChatwork(),
        client=client or fake_client(),
        http_get=fake_http(
            build_xlsx(sheets or [RISE_SHEET]),
            docs if docs is not None else {DOC_NEW: "新しい手順書\n\n1. 取得する\n2. 保存する"},
        ),
        now=clock,
    )
    s.clock = clock
    return s


class TestReadingTheIndexSheet:
    def test_items_come_out_in_row_order_with_their_links(self):
        items = parse_workbook(build_xlsx([RISE_SHEET]))
        assert [(i.section, i.name) for i in items] == [
            ("日常業務", "書類格納ルール"), ("日常業務", "請求書処理１ 取得～タスク作成まで"),
            ("日常業務", "弥生スマート証憑登録"), ("日常業務", "新しい手順書"),
        ]
        assert items[1].url == URL_A and items[3].url == URL_NEW
        assert items[2].note == "なるべく1週間以内 遅くとも月内に"  # 改行は1つの空白に
        assert items[2].updated == ""  # 「完了日」は作業の記録なので転記しない
        assert items[3].note == "毎週" and items[3].doc_id == DOC_NEW

    def test_login_sheets_keep_only_site_url_and_note_and_hide_credentials(self):
        items = parse_workbook(build_xlsx([LOGIN_SHEET]))
        assert [(i.name, i.url, i.note) for i in items] == [
            ("Q10", "https://qsm.qoo10.jp/", "担当: 山田"),
            ("minne", "https://minne.com/", "（認証情報は転記しない）"),  # 備考が認証情報に見える
        ]
        joined = " ".join(f"{i.name} {i.note} {i.url}" for i in items)
        assert "user01" not in joined and "secret" not in joined

    def test_links_written_as_hyperlink_formulas_are_read_too(self):
        # 楽天軒の目次はほとんどが =HYPERLINK("url","表示") の形。2026-08-13 の転記では丸ごと抜けていた
        ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
        sheet = (
            f'<worksheet {ns}><sheetData>'
            '<row r="4"><c r="B4" t="inlineStr"><is><t>リンク</t></is></c><c r="C4" t="inlineStr"><is><t>備考</t></is></c></row>'
            '<row r="5"><c r="B5" t="str"><f>HYPERLINK(&quot;https://docs.google.com/document/d/1UJvk0PiNOmcBkKl9Vs-JlKwdee4WFrHpW4q6EtfNRJo/edit?usp=sharing&quot;, &quot;通常発注マニュアル&quot;)</f><v>通常発注マニュアル</v></c></row>'
            '<row r="6"><c r="B6" t="inlineStr"><is><t>賞味期限 計算</t></is></c></row>'
            '</sheetData><hyperlinks><hyperlink ref="B6" r:id="rIdL1" location="gid=0"/></hyperlinks></worksheet>'
        )
        parts = {
            "xl/workbook.xml": f'<workbook {ns}><sheets><sheet name="発注関係" sheetId="1" r:id="rId1"/></sheets></workbook>',
            "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
            "xl/worksheets/sheet1.xml": sheet,
            "xl/worksheets/_rels/sheet1.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rIdL1" Target="https://docs.google.com/spreadsheets/d/1nOhz/edit" TargetMode="External"/></Relationships>',
        }
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name, data in parts.items():
                z.writestr(name, data)
        items = parse_workbook(buf.getvalue())
        assert [(i.name, i.url) for i in items] == [
            ("通常発注マニュアル", "https://docs.google.com/document/d/1UJvk0PiNOmcBkKl9Vs-JlKwdee4WFrHpW4q6EtfNRJo/edit?usp=sharing"),
            ("賞味期限 計算", "https://docs.google.com/spreadsheets/d/1nOhz/edit#gid=0"),  # 文書内の位置は # で付け直す
        ]

    def test_sheets_can_be_skipped(self):
        items = parse_workbook(build_xlsx([RISE_SHEET, LOGIN_SHEET]), skip_sheets=("ログインリンク",))
        assert {i.section for i in items} == {"日常業務"}

    def test_the_update_date_column_is_kept_when_present(self):
        sheet = ("発注関係", {"B4": "リンク", "C4": "備考", "D4": "更新日", "B5": "通常発注マニュアル", "D5": "2025/10/22"}, {"B5": URL_A})
        items = parse_workbook(build_xlsx([sheet]))
        assert items[0].updated == "2025/10/22"
        md = render_links_md("t", "u", items, date(2026, 10, 7))
        assert "| 項目 | 備考 | URL | 更新日 |" in md and "| 通常発注マニュアル |  | " + URL_A + " | 2025/10/22 |" in md


class TestTheLinksFile:
    def test_render_and_parse_round_trip(self):
        items = parse_workbook(build_xlsx([RISE_SHEET]))
        md = render_links_md("ライズマニュアルリンク集（経理財務 目次）", "https://sheet", items, date(2026, 10, 7), ["注意: 認証情報は転記しない"])
        assert md.startswith("# ライズマニュアルリンク集（経理財務 目次）\n\n- 元シート: https://sheet\n- 取得日: 2026-10-07")
        assert "- 注意: 認証情報は転記しない" in md and "\n## 日常業務\n" in md
        assert [(i.name, i.url, i.note) for i in parse_links_md(md)] == [(i.name, i.url, i.note) for i in items]

    def test_only_the_fetch_date_differing_is_not_a_change(self):
        a = render_links_md("t", "u", [LinkItem("s", "n", "", "https://x")], date(2026, 10, 7))
        b = render_links_md("t", "u", [LinkItem("s", "n", "", "https://x")], date(2026, 10, 8))
        assert strip_fetch_date(a) == strip_fetch_date(b)

    def test_changes_are_described_as_added_removed_changed(self):
        old = parse_links_md(OLD_MD)
        new = parse_workbook(build_xlsx([RISE_SHEET]))
        lines = summarize_changes(old, new)
        assert any(l.startswith("追加: 日常業務「新しい手順書」") and URL_NEW in l for l in lines)
        assert any(l.startswith("変更: 日常業務「請求書処理１ 取得～タスク作成まで」 URL") for l in lines)  # ?tab=t.0 が付いた
        assert not any(l.startswith("削除") for l in lines)
        removed = summarize_changes(new, old)
        assert any(l == "削除: 日常業務「新しい手順書」" for l in removed)

    def test_known_documents_are_read_from_handbook_headers_not_link_files(self, tmp_path):
        (tmp_path / "マニュアルリンク集_ライズ経理財務.md").write_text(OLD_MD, encoding="utf-8")
        (tmp_path / "請求書処理1.md").write_text(TRANSCRIBED_MD, encoding="utf-8")
        ids = known_document_ids([tmp_path])
        assert DOC_A in ids and "1zKRC4X2k9fTM4uVoHlZdkLimM5wt54LvrqYAFny3V9g" not in ids

    @pytest.mark.parametrize("title, expected", [
        ("請求書処理１　取得～タスク作成まで", "請求書処理1_取得〜タスク作成まで.md"),
        ("ＡＮＡフーズ（高島屋オンライン） 発注マニュアル", "ANAフーズ(高島屋オンライン)_発注マニュアル.md"),
        ("a/b:c*d?e", "abcde.md"),
    ])
    def test_file_names_are_safe(self, title, expected):
        assert safe_filename(title) == expected


class TestProposingToTheAdmin:
    def test_a_changed_index_and_a_new_manual_are_proposed_with_the_files_attached(self, tmp_path):
        s = syncer(tmp_path)
        assert s.run_once() == 2
        chatwork = s._chatwork
        assert {r for r, _, _ in chatwork.sent} == {ADMIN_ROOM}
        links_msg = chatwork.sent[0][1]
        assert links_msg.startswith(f"[To:{ADMIN}]\nマニュアルリンク集（ライズ経理財務）の元シートに変更がありました。")
        assert "■変更点" in links_msg and "追加: 日常業務「新しい手順書」" in links_msg
        assert "「承認」とお知らせいただければ" in links_msg and "「取りやめ」" in links_msg
        assert chatwork.uploaded[0][1] == "マニュアルリンク集_ライズ経理財務.md"
        new_links = chatwork.uploaded[0][2].decode("utf-8")
        assert "| 新しい手順書 | 毎週 | " + URL_NEW + " |" in new_links and "取得日: 2026-10-07" in new_links
        transcript_msg = chatwork.sent[1][1]
        assert "新しいマニュアル「新しい手順書」（ライズ経理財務／日常業務）の転記ファイルを作成しました" in transcript_msg
        assert "ファイル名: 新しい手順書.md" in transcript_msg and "直したい点があれば" in transcript_msg
        transcript = chatwork.uploaded[1][2].decode("utf-8")
        assert transcript.startswith("# 新しい手順書\n\n- 出典：Googleドキュメント「新しい手順書」\n  " + URL_NEW)
        assert "- 転記日：2026-10-07" in transcript and "## 手順\n1. 取得する" in transcript
        prompt = s._client.calls[0]["messages"][0]["content"]
        assert "原本の本文" in prompt and "1. 取得する" in prompt and "要約しない" in s._client.calls[0]["system"]
        # 承認前は何も書き換えない
        assert not (tmp_path / "state" / "handbook").exists()
        assert (tmp_path / "マニュアルリンク集_ライズ経理財務.md").read_text(encoding="utf-8") == OLD_MD
        pending = s.pending()
        assert [p["kind"] for p in pending] == ["links", "transcript"]
        assert all(p["ids"] for p in pending)

    def test_it_runs_once_a_day_after_the_check_time(self, tmp_path):
        s = syncer(tmp_path, now=datetime(2026, 10, 7, 7, 0, tzinfo=JST))
        assert s.run_once() == 0  # 7:30 前
        s.clock.now = datetime(2026, 10, 7, 7, 35, tzinfo=JST)
        assert s.run_once() == 2
        s.clock.now = datetime(2026, 10, 7, 12, 0, tzinfo=JST)
        assert s.run_once() == 0  # 同じ日に二度は見ない
        s.clock.now = datetime(2026, 10, 8, 8, 0, tzinfo=JST)
        assert s.run_once() == 0  # 提案が残っている間は出し直さない
        assert len(s._chatwork.sent) == 2

    def test_already_transcribed_manuals_are_not_proposed_again(self, tmp_path):
        s = syncer(tmp_path)
        s.run_once()
        titles = [p.get("title") for p in s.pending() if p["kind"] == "transcript"]
        assert titles == ["新しい手順書"]  # 請求書処理１ は出典が既にある

    def test_an_unreadable_manual_is_noted_and_not_retried_every_day(self, tmp_path):
        # 実例（2026-10-08）: 「YR経費精算処理マニュアル」が「リンクを知っている全員」の共有でなく読めなかった
        s = syncer(tmp_path, docs={})  # 原本が読めない
        assert s.run_once() == 1  # リンク集の提案だけ
        state = s._load()
        assert DOC_NEW in state["unreachable"] and state["unreachable"][DOC_NEW]["count"] == 1
        s.clock.now = datetime(2026, 10, 9, 8, 0, tzinfo=JST)
        s.run_once()
        assert s._load()["unreachable"][DOC_NEW]["count"] == 1  # 7日は試し直さない
        s.clock.now = datetime(2026, 10, 16, 8, 0, tzinfo=JST)
        s.run_once()
        assert s._load()["unreachable"][DOC_NEW]["count"] == 2

    def test_a_links_only_collection_gets_no_transcripts(self, tmp_path):
        # 楽天軒の目次はリンクの案内だけ（本文の転記はしない）
        s = syncer(tmp_path)
        s._config.data["handbook_sync"]["collections"][0]["transcribe"] = False
        assert s.run_once() == 1
        assert [p["kind"] for p in s.pending()] == ["links"]

    def test_dry_run_posts_nothing(self, tmp_path, capsys):
        s = syncer(tmp_path)
        assert s.run_once(force=True, dry_run=True) == 0
        assert s._chatwork.sent == [] and s.pending() == []
        out = capsys.readouterr().out
        assert "新しい手順書" in out and "件の違い" in out


class TestApproval:
    def approved(self, tmp_path):
        s = syncer(tmp_path)
        s.run_once()
        return s

    def test_approving_writes_the_file_where_the_handbook_reads_first(self, tmp_path):
        s = self.approved(tmp_path)
        links_id = s.pending()[0]["ids"][0]
        reply = s.handle(ADMIN_ROOM, ADMIN, "承認", reply_to=links_id)
        assert "「マニュアルリンク集_ライズ経理財務.md」をハンドブックに反映しました" in reply
        path = tmp_path / "state" / "handbook" / "マニュアルリンク集_ライズ経理財務.md"
        assert path.exists() and "新しい手順書" in path.read_text(encoding="utf-8")
        assert s._config.handbook_roots[0] == tmp_path / "state" / "handbook"  # 同名はこちらが優先
        assert [p["kind"] for p in s.pending()] == ["transcript"]
        # 転記の方も承認（添付の投稿への返信でも通る）→ 転記済みとして覚える
        reply = s.handle(ADMIN_ROOM, ADMIN, "OKです", reply_to=s.pending()[0]["ids"][-1])
        assert "新しい手順書.md" in reply
        assert (tmp_path / "state" / "handbook" / "新しい手順書.md").exists()
        assert DOC_NEW in s._load()["transcribed"] and s.pending() == []

    def test_declining_keeps_the_handbook_untouched(self, tmp_path):
        s = self.approved(tmp_path)
        transcript = s.pending()[1]
        reply = s.handle(ADMIN_ROOM, ADMIN, "取りやめ", reply_to=transcript["ids"][0])
        assert "見送ります" in reply
        assert not (tmp_path / "state" / "handbook").exists()
        assert DOC_NEW in s._load()["declined"]
        s.clock.now = datetime(2026, 10, 8, 8, 0, tzinfo=JST)
        s.run_once()
        assert [p["kind"] for p in s.pending()] == ["links"]  # 見送った分は出し直さない

    def test_a_correction_is_applied_and_reproposed(self, tmp_path):
        s = self.approved(tmp_path)
        transcript = s.pending()[1]
        reply = s.handle(ADMIN_ROOM, ADMIN, "手順2は「Boxに保存する」と書いてください", reply_to=transcript["ids"][0])
        assert "作り直し" in reply
        prompt = s._client.calls[-1]["messages"][0]["content"]
        assert "管理者からの直しの指示" in prompt and "Boxに保存する" in prompt and "前回の転記" in prompt
        assert len(s._chatwork.uploaded) == 3  # 作り直した転記を添付し直す
        assert "作り直しです" in s._chatwork.sent[-1][1]
        assert [p["kind"] for p in s.pending()] == ["links", "transcript"]

    def test_without_a_reply_target_it_asks_which_one(self, tmp_path):
        s = self.approved(tmp_path)
        reply = s.handle(ADMIN_ROOM, ADMIN, "承認")
        assert reply.startswith("どの件のお返事か分からなかったので、番号でお知らせください。")
        assert "1. マニュアルリンク集_ライズ経理財務.md" in reply and "2. 新しい手順書.md" in reply
        assert "反映しました" in s.handle(ADMIN_ROOM, ADMIN, "2を承認")
        assert "反映しました" in s.handle(ADMIN_ROOM, ADMIN, "承認")  # 残り1件なら番号は要らない

    def test_other_rooms_people_and_messages_are_ignored(self, tmp_path):
        s = self.approved(tmp_path)
        assert s.handle(384793683, ADMIN, "承認") is None
        assert s.handle(ADMIN_ROOM, 9228914, "承認") is None
        assert s.handle(ADMIN_ROOM, ADMIN, "今日の予定は？") is None  # 承認でも取りやめでもない一言
        assert s.handle(ADMIN_ROOM, ADMIN, "OKです", reply_to="123456") is None  # 別の投稿への緩い返事
        assert len(s.pending()) == 2

    def test_a_clear_approval_on_an_unknown_reply_target_still_counts(self, tmp_path):
        # 添付の投稿のIDが控えられないことがあるので、「承認」とはっきり書かれていれば受ける
        s = self.approved(tmp_path)
        reply = s.handle(ADMIN_ROOM, ADMIN, "承認", reply_to="123456")
        assert reply.startswith("どの件のお返事か分からなかったので")  # 2件あるので番号を聞く
        s.handle(ADMIN_ROOM, ADMIN, "取りやめ", reply_to=s.pending()[1]["ids"][0])
        assert "反映しました" in s.handle(ADMIN_ROOM, ADMIN, "承認", reply_to="123456")  # 残り1件なら通す

    def test_nothing_pending_means_nothing_to_handle(self, tmp_path):
        s = HandbookSync(make_config(tmp_path), FakeChatwork(), client=fake_client(), http_get=fake_http(b"", {}))
        assert s.handle(ADMIN_ROOM, ADMIN, "承認") is None


class TestConfig:
    def test_the_sync_dir_comes_first_only_when_enabled(self, tmp_path):
        config = make_config(tmp_path)
        assert config.handbook_roots == [tmp_path / "state" / "handbook", tmp_path]
        config.data["handbook_sync"]["enabled"] = False
        assert config.handbook_roots == [tmp_path]

    def test_the_real_config_lists_both_collections(self):
        config = Config.load(Path(__file__).resolve().parents[1] / "config" / "config.json")
        names = [c["name"] for c in config.handbook_sync["collections"]]
        assert names == ["ライズ経理財務", "楽天軒"]
        assert config.handbook_sync["enabled"] is True
        assert "連絡先" in config.handbook_sync["collections"][1]["skip_sheets"]
