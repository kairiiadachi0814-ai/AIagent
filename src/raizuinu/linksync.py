"""マニュアルリンク集の自動更新と、新しいマニュアルの転記ファイルの自動作成（管理者承認つき）。

ハンドブックの「マニュアルリンク集_〜.md」は、Googleスプレッドシートの目次（タブごとに
「リンク／備考／更新日」の表）から転記したもの。元シートが更新されても自動では追従しなかった
（取得日 2026-08-13 のまま）。本モジュールは次を行う（指示 2026-10-07）:

1. 目次シートを毎日1回取り込み、いまのリンク集ファイルと違いがあれば、更新したファイルを
   管理者ルームへ添付して提示し、「承認」の返事で反映する（「取りやめ」で見送る）
2. 目次に新しいGoogleドキュメントのマニュアルが現れたら、本文を取り込んでハンドブックの
   転記ファイル（.md）を作り、同じく管理者ルームへ添付して提示し、「承認」で加える。
   直しの指示があれば作り直して出し直す

方針:
- 元シート・原本は「リンクを知っている全員が閲覧可」の共有を前提に、Googleの書き出し
  （スプレッドシートは xlsx、ドキュメントはテキスト）を認証なしで取る（FAX番号台帳と同じ）。
  xlsx を使うのは、目次の URL がセルのハイパーリンクに入っていて CSV では取れないため
- 反映先は状態ディレクトリ配下の `handbook/`（`Config.handbook_roots` の先頭）。同名の
  ファイルはリポジトリ側より優先される。リポジトリへの取り込みは管理者が行う
- 承認なしに書き換えない。成果物（ファイルそのもの）を提示してから承認を得る（ガードレール4）
- ID・パスワードの列は転記しない。備考が認証情報に見えるときも伏せる
"""

from __future__ import annotations

import hashlib
import json
import re
import traceback
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Callable
from xml.etree import ElementTree as ET

JST = timezone(timedelta(hours=9))

DOC_ID_RE = re.compile(r"docs\.google\.com/document/(?:u/\d+/)?d/([A-Za-z0-9_-]+)")
_URL_RE = re.compile(r"https?://\S+")

# 目次シートの見出し。列の役割は見出しの文字で決める
NAME_HEADERS = ("リンク", "サイト名", "項目", "マニュアル", "名称")
NOTE_HEADERS = ("備考", "メモ")
URL_HEADERS = ("URL", "リンク先")
DATE_HEADERS = ("更新日",)
# 転記しない列（認証情報・作業の完了日）
SKIP_HEADERS = ("ID", "パスワード", "PW", "PASS", "完了日")
# 備考が認証情報に見えるとき（ログインリンクのタブなど）は伏せる
_SECRET_NOTE_RE = re.compile(r"(パスワード|password|passwd|\bpw\b|ログイン\s*id|\bid[:：]|@)", re.I)
SECRET_PLACEHOLDER = "（認証情報は転記しない）"

_APPROVE_RE = re.compile(r"(承認|反映して|反映お願い|送信|OK|オーケー|お願いします|問題な[いし]|大丈夫|進めて)", re.I)
_CANCEL_RE = re.compile(r"(取りやめ|取り止め|見送|やめ|キャンセル|却下|不要|中止|ボツ)")
# 提案の投稿ではなく添付の投稿（IDを控えられないことがある）への返信でも通す、はっきりした言い方
_STRICT_RE = re.compile(r"(承認|反映|取りやめ|取り止め|見送)")

_SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"


@dataclass
class LinkItem:
    section: str
    name: str
    note: str = ""
    url: str = ""
    updated: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.section, self.name)

    @property
    def doc_id(self) -> str:
        match = DOC_ID_RE.search(self.url)
        return match.group(1) if match else ""


def _clean(text: Any) -> str:
    """セルの文字をリンク集の1行に収める（改行・連続空白は1つの空白に）。"""
    return re.sub(r"[\s　]+", " ", str(text or "")).strip()


def _cell_ref_parts(ref: str) -> tuple[str, int]:
    match = re.match(r"([A-Z]+)(\d+)", ref)
    return (match.group(1), int(match.group(2))) if match else ("", 0)


def _col_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n


def _col_letters(index: int) -> str:
    out = ""
    while index > 0:
        index, rem = divmod(index - 1, 26)
        out = chr(65 + rem) + out
    return out


# --- 目次シート（xlsx書き出し）の読み取り ---


def spreadsheet_export_url(spreadsheet_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?format=xlsx"


def document_export_url(doc_id: str) -> str:
    return f"https://docs.google.com/document/d/{doc_id}/export?format=txt"


def _shared_strings(z: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    out = []
    for si in root.iter(_SHEET_NS + "si"):
        out.append("".join(t.text or "" for t in si.iter(_SHEET_NS + "t")))
    return out


def _rels(z: zipfile.ZipFile, path: str) -> dict[str, str]:
    if path not in z.namelist():
        return {}
    root = ET.fromstring(z.read(path))
    return {
        rel.get("Id", ""): rel.get("Target", "")
        for rel in root.iter(_PKG_REL_NS + "Relationship")
    }


def parse_workbook(data: bytes, skip_sheets: tuple[str, ...] | list[str] = ()) -> list[LinkItem]:
    """xlsx の各タブを読んで、リンク集の項目にする（タブ名が区分）。"""
    items: list[LinkItem] = []
    with zipfile.ZipFile(BytesIO(data)) as z:
        strings = _shared_strings(z)
        workbook = ET.fromstring(z.read("xl/workbook.xml"))
        wb_rels = _rels(z, "xl/_rels/workbook.xml.rels")
        for sheet in workbook.iter(_SHEET_NS + "sheet"):
            title = sheet.get("name", "")
            if title in set(skip_sheets):
                continue
            target = wb_rels.get(sheet.get(_REL_NS + "id", ""), "")
            if not target:
                continue
            path = target.lstrip("/")
            if not path.startswith("xl/"):
                path = "xl/" + path
            if path not in z.namelist():
                continue
            sheet_rels = _rels(z, path.replace("worksheets/", "worksheets/_rels/") + ".rels")
            items += _parse_sheet(title, z.read(path), strings, sheet_rels)
    return items


# =HYPERLINK("https://…", "表示文字") の形（楽天軒の目次はほとんどがこの形。セルのハイパーリンク
# ではないため、2026-08-13 の転記ではこれらが丸ごと抜けていた）
_HYPERLINK_FORMULA_RE = re.compile(r"HYPERLINK\(\s*\"([^\"]+)\"", re.I)


def _parse_sheet(title: str, xml: bytes, strings: list[str], rels: dict[str, str]) -> list[LinkItem]:
    root = ET.fromstring(xml)
    cells: dict[tuple[int, int], str] = {}
    links: dict[tuple[int, int], str] = {}
    for c in root.iter(_SHEET_NS + "c"):
        ref = c.get("r", "")
        letters, row = _cell_ref_parts(ref)
        if not letters:
            continue
        kind = c.get("t", "")
        value = ""
        v = c.find(_SHEET_NS + "v")
        if kind == "s" and v is not None:
            try:
                value = strings[int(v.text or "0")]
            except (ValueError, IndexError):
                value = ""
        elif kind == "inlineStr":
            value = "".join(t.text or "" for t in c.iter(_SHEET_NS + "t"))
        elif v is not None:
            value = v.text or ""
        f = c.find(_SHEET_NS + "f")
        if f is not None and f.text:
            match = _HYPERLINK_FORMULA_RE.search(f.text)
            if match:
                links[(row, _col_index(letters))] = match.group(1)
        if value.strip():
            cells[(row, _col_index(letters))] = value
    for h in root.iter(_SHEET_NS + "hyperlink"):
        letters, row = _cell_ref_parts(h.get("ref", ""))
        target = rels.get(h.get(_REL_NS + "id", ""), "")
        location = h.get("location", "")
        if target and location and "#" not in target:
            url = f"{target}#{location}"  # 「#gid=0」のような同一文書内の位置は書き出しで分かれる
        else:
            url = target or location
        if letters and url:
            links[(row, _col_index(letters))] = url

    # 見出し行（「リンク」「サイト名」などがある最初の行）と、列の役割
    rows = sorted({r for r, _ in cells})
    header_row = next(
        (r for r in rows if any(_clean(cells.get((r, c))) in NAME_HEADERS for c in _cols(cells, r))),
        0,
    )
    if not header_row:
        return []
    groups: list[dict[str, int]] = []
    for col in _cols(cells, header_row):
        head = _clean(cells.get((header_row, col)))
        if head in NAME_HEADERS:
            groups.append({"name": col})
        elif not groups:
            continue
        elif head in NOTE_HEADERS:
            groups[-1].setdefault("note", col)
        elif head in URL_HEADERS:
            groups[-1].setdefault("url", col)
        elif head in DATE_HEADERS:
            groups[-1].setdefault("updated", col)
    items: list[LinkItem] = []
    for row in rows:
        if row <= header_row:
            continue
        for group in groups:
            name = _clean(cells.get((row, group["name"])))
            if not name:
                continue
            url = links.get((row, group["name"]), "")
            if not url and "url" in group:
                text = _clean(cells.get((row, group["url"])))
                url = text if _URL_RE.match(text) else links.get((row, group["url"]), "")
            if not url and _URL_RE.match(name):
                url = name
            note = _clean(cells.get((row, group["note"]))) if "note" in group else ""
            if note and _SECRET_NOTE_RE.search(note):
                note = SECRET_PLACEHOLDER
            updated = _clean(cells.get((row, group["updated"]))) if "updated" in group else ""
            items.append(LinkItem(section=title, name=name, note=note, url=url, updated=updated))
    return items


def _cols(cells: dict[tuple[int, int], str], row: int) -> list[int]:
    return sorted(c for r, c in cells if r == row)


# --- リンク集ファイルの読み書き ---


def render_links_md(title: str, sheet_url: str, items: list[LinkItem], fetched: date, notes: list[str] = ()) -> str:
    lines = [
        f"# {title}",
        "",
        f"- 元シート: {sheet_url}",
        f"- 取得日: {fetched.isoformat()}（元シート更新時は自動で取り込み、管理者の承認を経て更新する）",
        "- 本ファイルは目次シートから項目名・URL・備考を自動転記したリンク集。責任者照合: 未実施",
        "- 用途: 「このマニュアル／シートはどこ？」という質問に、該当URLを案内するための索引",
    ]
    lines += [f"- {n}" for n in notes]
    sections: list[str] = []
    for item in items:
        if item.section not in sections:
            sections.append(item.section)
    for section in sections:
        rows = [i for i in items if i.section == section]
        with_date = any(i.updated for i in rows)
        lines += ["", f"## {section}", ""]
        if with_date:
            lines += ["| 項目 | 備考 | URL | 更新日 |", "|---|---|---|---|"]
        else:
            lines += ["| 項目 | 備考 | URL |", "|---|---|---|"]
        for i in rows:
            cols = [_md_cell(i.name), _md_cell(i.note), _md_cell(i.url)]
            if with_date:
                cols.append(_md_cell(i.updated))
            lines.append("| " + " | ".join(cols) + " |")
    return "\n".join(lines) + "\n"


def _md_cell(text: str) -> str:
    return str(text or "").replace("|", "｜")


def parse_links_md(text: str) -> list[LinkItem]:
    """いまのリンク集ファイルから項目を読み戻す（差分の説明に使う）。"""
    items: list[LinkItem] = []
    section = ""
    for line in str(text or "").splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        if not line.startswith("|") or line.startswith("|---"):
            continue
        cols = [c.strip() for c in line.strip().strip("|").split("|")]
        if not cols or cols[0] in ("項目",):
            continue
        name = cols[0]
        note = cols[1] if len(cols) > 1 else ""
        url = cols[2] if len(cols) > 2 else ""
        updated = cols[3] if len(cols) > 3 else ""
        if name:
            items.append(LinkItem(section=section, name=name, note=note, url=url, updated=updated))
    return items


def strip_fetch_date(text: str) -> str:
    return "\n".join(l for l in str(text or "").splitlines() if not l.startswith("- 取得日:"))


def summarize_changes(old: list[LinkItem], new: list[LinkItem], limit: int = 20) -> list[str]:
    """追加・削除・変更を人が読める行にする。"""
    before = {i.key: i for i in old}
    after = {i.key: i for i in new}
    lines: list[str] = []
    for key, item in after.items():
        if key not in before:
            lines.append(f"追加: {item.section}「{item.name}」 {item.url}".rstrip())
    for key, item in before.items():
        if key not in after:
            lines.append(f"削除: {item.section}「{item.name}」")
    for key, item in after.items():
        prev = before.get(key)
        if prev is None:
            continue
        changed = []
        if prev.url != item.url:
            changed.append(f"URL {prev.url or '（なし）'} → {item.url or '（なし）'}")
        if prev.note != item.note:
            changed.append(f"備考「{prev.note}」→「{item.note}」")
        if prev.updated != item.updated:
            changed.append(f"更新日 {prev.updated or '（なし）'} → {item.updated or '（なし）'}")
        if changed:
            lines.append(f"変更: {item.section}「{item.name}」 " + "、".join(changed))
    if len(lines) > limit:
        rest = len(lines) - limit
        lines = lines[:limit] + [f"…ほか{rest}件（添付のファイルをご確認ください）"]
    return lines


def known_document_ids(roots: list[Path], exclude_patterns: tuple[str, ...] = ("リンク集",)) -> set[str]:
    """転記済みのマニュアル（ハンドブックの各ファイル冒頭の出典URL）のドキュメントID。"""
    ids: set[str] = set()
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.glob("*.md")):
            if any(p in path.name for p in exclude_patterns):
                continue
            try:
                head = "\n".join(path.read_text(encoding="utf-8").splitlines()[:12])
            except OSError:
                continue
            ids.update(DOC_ID_RE.findall(head))
    return ids


def safe_filename(title: str, limit: int = 60) -> str:
    name = unicodedata.normalize("NFKC", str(title or "")).strip()
    name = name.replace("~", "〜")  # NFKC で「～」が ASCII のチルダになるのを戻す
    name = re.sub(r"[\\/:*?\"<>|]", "", name)
    name = re.sub(r"[\s　]+", "_", name).strip("_")
    name = name[:limit] or "untitled"
    return name + ".md"


# --- 転記（Googleドキュメント → ハンドブックの .md） ---

TRANSCRIBE_SYSTEM = """あなたは株式会社ライズクリエイション経理財務部の社内マニュアルを、AIアシスタントが
参照するハンドブック（Markdown）へ転記する担当です。渡されたGoogleドキュメントの本文を、
内容を変えずにMarkdownへ書き起こしてください。

厳守すること:
- 要約しない。手順・条件・日付・金額・名前・注意書きは一字一句の精度で残す。
  読みやすくするための見出し（##、###）・箇条書き・表への整理はしてよいが、
  内容を足したり削ったり言い換えたりしない
- 原本に無いことを書かない。不明な箇所は「（原本の記載が判読できない）」と書く
- ID・パスワード・口座番号などの認証情報・機密の値は転記せず「（認証情報は原本を参照）」と置く。
  パスワードの置き場（どのシートにあるか）は書いてよい
- 原本に画面画像があると思われる箇所（「下図」「画面」「スクリーンショット」など）は、
  「（原本に画面画像あり。本ファイルには転記していない）」と注記する
- 見出し行や本文の先頭に「# タイトル」を書かない（ファイルの冒頭はこちらで付ける）。
  出力はMarkdown本文だけ。前置き・あとがき・コードフェンスを付けない
"""


def transcript_header(title: str, doc_url: str, fetched: date, updated: str = "") -> str:
    lines = [
        f"# {title}",
        "",
        f"- 出典：Googleドキュメント「{title}」",
        f"  {doc_url}",
        f"- 原本最終更新：{updated or '不明（原本の更新履歴を参照）'}",
        f"- 転記日：{fetched.isoformat()}（自動転記。管理者の承認を経てハンドブックに追加）",
        "- 責任者照合：未実施（原本と目視で照合すること）",
        "- 注記：原本に画面画像がある場合、本ファイルには転記できていない。認証情報（ID・パスワード類）は転記していない",
        "",
    ]
    return "\n".join(lines)


class HandbookSync:
    """目次シートの変更と新しいマニュアルを見つけ、管理者の承認を経てハンドブックへ反映する。"""

    STATE_FILE = "handbook_sync.json"

    def __init__(
        self,
        config: Any,
        chatwork: Any,
        client: Any | None = None,
        http_get: Callable[..., tuple[int, str, bytes]] | None = None,
        now: Callable[[], datetime] | None = None,
        cost: Any | None = None,
    ) -> None:
        self._config = config
        self._chatwork = chatwork
        self._client = client
        self._cost = cost
        self._now = now or (lambda: datetime.now(JST))
        if http_get is None:
            import requests

            def http_get(url: str, timeout: int = 60) -> tuple[int, str, bytes]:
                resp = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
                return resp.status_code, resp.url, resp.content

        self._http_get = http_get
        self._state_path = config.resolve_path(config.state_dir) / self.STATE_FILE
        self._out_dir = config.handbook_sync_dir

    # --- 公開API ---

    @property
    def settings(self) -> dict:
        return dict(self._config.handbook_sync or {})

    def run_once(self, force: bool = False, dry_run: bool = False) -> int:
        """1日1回、目次シートを取り込んで提案を出す。→ 出した提案の数（dry_run は出さない）。"""
        settings = self.settings
        if not settings.get("enabled"):
            return 0
        now = self._now()
        state = self._load()
        today = now.date().isoformat()
        if not force:
            clock = str(settings.get("check_time") or "07:30")
            hour, minute = (int(x) for x in clock.split(":"))
            if state.get("last_checked") == today or now < now.replace(hour=hour, minute=minute, second=0, microsecond=0):
                return 0
        proposals = 0
        pending_files = {p.get("filename") for p in state.get("proposals") or []}
        all_items: list[tuple[dict, LinkItem]] = []
        for collection in settings.get("collections") or []:
            try:
                items = self._fetch_collection(collection)
            except Exception:
                print("[warn] 目次シートの取得に失敗: " + traceback.format_exc(), flush=True)
                continue
            all_items += [(collection, i) for i in items]
            filename = str(collection.get("file") or "")
            if not filename or filename in pending_files:
                continue
            rendered = render_links_md(
                str(collection.get("title") or filename),
                f"https://docs.google.com/spreadsheets/d/{collection.get('spreadsheet_id')}/edit",
                items,
                now.date(),
                list(collection.get("notes") or []),
            )
            current = self._current_text(filename)
            if strip_fetch_date(rendered) == strip_fetch_date(current):
                continue
            changes = summarize_changes(parse_links_md(current), items)
            if dry_run:
                print(f"[dry-run] {filename}: {len(changes)}件の違い", flush=True)
                for line in changes:
                    print("  " + line, flush=True)
                continue
            self._propose_links(state, collection, filename, rendered, changes)
            proposals += 1
        proposals += self._propose_new_manuals(state, all_items, now, dry_run)
        if not dry_run:
            state["last_checked"] = today
            self._save(state)
        return proposals

    def handle(self, room_id: int, account_id: int, text: str, reply_to: str = "") -> str | None:
        """管理者ルームでの返事（承認・取りやめ・直し）。扱ったら返信文、無関係なら None。"""
        settings = self.settings
        if not settings.get("enabled"):
            return None
        if int(room_id) != int(self._config.admin_room_id or 0):
            return None
        if int(account_id) not in {int(a) for a in self._config.admin_account_ids}:
            return None
        state = self._load()
        pending = list(state.get("proposals") or [])
        if not pending:
            return None
        target = None
        if reply_to:
            target = next((p for p in pending if str(reply_to) in [str(i) for i in p.get("ids") or []]), None)
            if target is None and not _STRICT_RE.search(text):
                return None  # 別の投稿への返事（「承認」「取りやめ」とはっきり書かれていれば、添付の投稿への返信として扱う）
        if target is None and len(pending) == 1 and (_APPROVE_RE.search(text) or _CANCEL_RE.search(text)):
            target = pending[0]
        if target is None:
            picked = re.search(r"(\d+)", unicodedata.normalize("NFKC", text))
            if picked and 1 <= int(picked.group(1)) <= len(pending):
                target = pending[int(picked.group(1)) - 1]
        if target is None:
            if _APPROVE_RE.search(text) or _CANCEL_RE.search(text):
                lines = ["どの件のお返事か分からなかったので、番号でお知らせください。"]
                lines += [f"{n}. {p.get('filename')}（{p.get('kind_label')}）" for n, p in enumerate(pending, 1)]
                return "\n".join(lines)
            return None
        if _CANCEL_RE.search(text) and not _APPROVE_RE.search(text):
            self._drop(state, target, declined=True)
            self._audit({"type": "handbook_sync_declined", "filename": target.get("filename"), "kind": target.get("kind")})
            return f"承知しました。「{target.get('filename')}」は反映せずに見送ります。"
        if _APPROVE_RE.search(text):
            path = self._write(target)
            self._drop(state, target, applied=True)
            self._audit({"type": "handbook_sync_applied", "filename": target.get("filename"), "kind": target.get("kind"), "path": str(path)})
            return (
                f"「{target.get('filename')}」をハンドブックに反映しました。次の回答から参照します（最大5分後）。\n"
                "リポジトリへの取り込みは、デプロイ手順書の「自動更新されたハンドブックの取り込み」をご覧ください。"
            )
        if target.get("kind") == "transcript":
            # 直しの指示。指示を添えて作り直し、出し直す
            try:
                revised = self._transcribe(target, instruction=text)
            except Exception:
                print("[warn] 転記の作り直しに失敗: " + traceback.format_exc(), flush=True)
                return "作り直しに失敗しました。もう一度お知らせいただくか、しばらくしてからお試しください。"
            self._drop(state, target)
            state = self._load()
            self._propose_transcript(state, {**target, "content": revised, "revision_note": text})
            self._save(state)
            return "ご指摘を反映して作り直し、添付し直しました。ご確認ください。"
        return None

    def pending(self) -> list[dict]:
        return list(self._load().get("proposals") or [])

    # --- 内部: 取得と提案 ---

    def _fetch_collection(self, collection: dict) -> list[LinkItem]:
        status, final_url, data = self._http_get(spreadsheet_export_url(str(collection.get("spreadsheet_id"))))
        if status != 200 or "accounts.google.com" in str(final_url) or not data.startswith(b"PK"):
            raise RuntimeError(
                f"目次シートを開けませんでした（{collection.get('name')}。共有設定が「リンクを知っている全員」か確認）"
            )
        return parse_workbook(data, tuple(collection.get("skip_sheets") or ()))

    def _current_text(self, filename: str) -> str:
        for root in [self._out_dir] + list(self._config.handbook_roots):
            path = Path(root) / filename
            if path.exists():
                try:
                    return path.read_text(encoding="utf-8")
                except OSError:
                    continue
        return ""

    def _propose_links(self, state: dict, collection: dict, filename: str, content: str, changes: list[str]) -> None:
        lead = [
            f"マニュアルリンク集（{collection.get('name')}）の元シートに変更がありました。更新した「{filename}」を添付します。",
            f"■変更点（{len(changes)}件）",
        ] + ["・" + c for c in changes] + [
            "この投稿への返信で「承認」とお知らせいただければ、ハンドブックに反映します。「取りやめ」で見送ります。",
        ]
        proposal = {
            "kind": "links",
            "kind_label": "リンク集の更新",
            "filename": filename,
            "content": content,
            "collection": collection.get("name"),
            "summary": changes,
        }
        self._post_proposal(state, proposal, "\n".join(lead))

    def _propose_new_manuals(self, state: dict, all_items: list[tuple[dict, LinkItem]], now: datetime, dry_run: bool) -> int:
        settings = self.settings.get("transcribe") or {}
        if not settings.get("enabled", True):
            return 0
        known = known_document_ids([self._out_dir] + list(self._config.handbook_roots))
        known |= set((state.get("transcribed") or {}).keys())
        known |= set((state.get("declined") or {}).keys())
        known |= {str(p.get("doc_id")) for p in state.get("proposals") or [] if p.get("doc_id")}
        # 読めなかった原本（共有設定が「リンクを知っている全員」でない等）は、しばらく置いてから試し直す
        retry_after = timedelta(days=int(settings.get("unreachable_retry_days", 7)))
        unreachable = state.get("unreachable") or {}
        for doc_id, info in unreachable.items():
            tried = _parse_iso(info.get("at"))
            if tried and now - tried < retry_after:
                known.add(doc_id)
        seen: set[str] = set()
        candidates: list[tuple[dict, LinkItem]] = []
        for collection, item in all_items:
            if not collection.get("transcribe", True):
                continue  # リンクだけ持つ目次（楽天軒など）。マニュアルの転記はしない
            doc_id = item.doc_id
            if not doc_id or doc_id in known or doc_id in seen:
                continue
            seen.add(doc_id)
            candidates.append((collection, item))
        limit = int(settings.get("max_per_run", 3))
        if dry_run:
            print(f"[dry-run] 転記していないマニュアル: {len(candidates)}件（1回につき{limit}件まで）", flush=True)
        made = 0
        for collection, item in candidates[:limit]:
            if dry_run:
                print(f"[dry-run] 新しいマニュアル: {collection.get('name')}／{item.section}「{item.name}」 {item.url}", flush=True)
                continue
            try:
                try:
                    text = self._fetch_document(item.doc_id)
                except Exception as exc:
                    state.setdefault("unreachable", {})[item.doc_id] = {
                        "title": item.name, "url": item.url, "at": now.isoformat(),
                        "count": int(unreachable.get(item.doc_id, {}).get("count", 0)) + 1,
                        "error": str(exc)[:200],
                    }
                    self._save(state)
                    print(f"[warn] 原本を読めませんでした: {item.name} {item.url} — {exc}", flush=True)
                    continue
                proposal = {
                    "kind": "transcript",
                    "kind_label": "新しいマニュアルの転記",
                    "filename": safe_filename(item.name),
                    "title": item.name,
                    "doc_id": item.doc_id,
                    "doc_url": item.url,
                    "updated": item.updated,
                    "collection": collection.get("name"),
                    "section": item.section,
                    "source_text": text,
                }
                proposal["content"] = self._transcribe(proposal)
            except Exception:
                print("[warn] マニュアルの転記に失敗: " + traceback.format_exc(), flush=True)
                continue
            self._propose_transcript(state, proposal)
            made += 1
        if candidates[limit:] and not dry_run:
            print(f"[info] 転記待ちのマニュアルがあと{len(candidates) - limit}件あります（明日以降に続けます）", flush=True)
        return made

    def _propose_transcript(self, state: dict, proposal: dict) -> None:
        lead = [
            f"新しいマニュアル「{proposal.get('title')}」（{proposal.get('collection')}／{proposal.get('section')}）の転記ファイルを作成しました。添付をご確認ください。",
            f"ファイル名: {proposal.get('filename')}（{len(proposal.get('content') or '')}字）",
            f"原本: {proposal.get('doc_url')}",
        ]
        if proposal.get("revision_note"):
            lead.append(f"（直しの指示「{_clean(proposal['revision_note'])[:60]}」を反映した作り直しです）")
        lead += [
            "※原本の画面画像は転記できていません。ID・パスワード類は転記していません。",
            "この投稿への返信で「承認」とお知らせいただければハンドブックに加えます。直したい点があれば、その旨を返信していただければ作り直します。「取りやめ」で見送ります。",
        ]
        self._post_proposal(state, proposal, "\n".join(lead))

    def _post_proposal(self, state: dict, proposal: dict, lead: str) -> None:
        admin_room = int(self._config.admin_room_id or 0)
        if not admin_room:
            raise RuntimeError("admin_room_id が設定されていません")
        heads = " ".join(f"[To:{int(a)}]" for a in self._config.admin_account_ids)
        message_id = str(self._chatwork.send_message(admin_room, f"{heads}\n{lead}" if heads else lead) or "")
        ids = [message_id] if message_id else []
        filename = str(proposal.get("filename"))
        try:
            self._chatwork.upload_file(
                admin_room,
                filename,
                str(proposal.get("content") or "").encode("utf-8"),
                message=f"{filename} を添付します。",
            )
            ids += self._find_own_messages(admin_room, filename, after=message_id)
        except Exception:
            print("[warn] 成果物の添付に失敗: " + traceback.format_exc(), flush=True)
        record = {
            **{k: v for k, v in proposal.items() if k != "source_text"},
            "source_text": proposal.get("source_text", "")[:200000],
            "ids": ids,
            "proposed_at": self._now().isoformat(),
        }
        proposals = [p for p in state.get("proposals") or [] if p.get("filename") != filename]
        proposals.append(record)
        state["proposals"] = proposals
        self._save(state)
        self._audit(
            {
                "type": "handbook_sync_proposed",
                "kind": proposal.get("kind"),
                "filename": filename,
                "collection": proposal.get("collection"),
                "message_id": message_id,
                "summary": (proposal.get("summary") or [])[:20],
            }
        )

    def _find_own_messages(self, room_id: int, filename: str, after: str) -> list[str]:
        """添付の投稿（アップロードAPIはIDを返さない）を、直近の発言から拾う（少し待って3回まで）。"""
        import time

        try:
            me = int(self._chatwork.get_me())
            inner = getattr(self._chatwork, "_client", None) or self._chatwork  # 巡回のキャッシュを通さず取り直す
            for attempt in range(3):
                found = []
                for message in inner.get_recent_messages(room_id, limit=20):
                    account = (message.get("account") or {}).get("account_id")
                    mid = str(message.get("message_id", ""))
                    if int(account or 0) == me and filename in str(message.get("body", "")) and mid != after:
                        found.append(mid)
                if found:
                    return found[-1:]
                if attempt < 2:
                    time.sleep(1.5)
            return []
        except Exception:
            return []

    # --- 内部: 転記 ---

    def _fetch_document(self, doc_id: str) -> str:
        status, final_url, data = self._http_get(document_export_url(doc_id))
        if status != 200 or "accounts.google.com" in str(final_url):
            raise RuntimeError("原本のドキュメントを開けませんでした（共有設定が「リンクを知っている全員」か確認）")
        text = data.decode("utf-8-sig", errors="replace")
        limit = int((self.settings.get("transcribe") or {}).get("max_source_chars", 120000))
        return text[:limit]

    def _transcribe(self, proposal: dict, instruction: str = "") -> str:
        """原本の本文からハンドブックの転記ファイルを作る（冒頭の出典はコードで付ける）。"""
        from .answer import _call_with_continuation

        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        cfg = self._config
        title = str(proposal.get("title") or "")
        source = str(proposal.get("source_text") or "")
        prompt = f"マニュアル名: {title}\n原本URL: {proposal.get('doc_url')}\n\n--- 原本の本文 ---\n{source}"
        if instruction:
            previous = str(proposal.get("content") or "")
            prompt += (
                "\n\n--- 前回の転記 ---\n" + previous.split("\n", 10)[-1][:60000]
                + f"\n\n--- 管理者からの直しの指示 ---\n{instruction}\n"
                "指示を反映して転記し直してください（指示に無い箇所は前回どおり）。"
            )
        kwargs = {
            "model": cfg.model,
            "max_tokens": int(cfg.max_tokens),
            "system": TRANSCRIBE_SYSTEM,
        }
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        parts: list[str] = []
        usage_total: dict[str, int] = {}
        for _ in range(4):
            response, usage, _ = _call_with_continuation(self._client.messages.create, kwargs, messages)
            for key, value in (usage or {}).items():
                usage_total[key] = usage_total.get(key, 0) + int(value)
            text = "".join(getattr(b, "text", "") for b in getattr(response, "content", []) if getattr(b, "type", "") == "text")
            parts.append(text)
            if getattr(response, "stop_reason", "") != "max_tokens":
                break
            messages = messages + [
                {"role": "assistant", "content": text},
                {"role": "user", "content": "続きを、途切れたところから出力してください（重複させない）。"},
            ]
        if self._cost is not None and usage_total:
            try:
                self._cost.add_usage(usage_total)
            except Exception:
                print("[warn] コスト計上に失敗: " + traceback.format_exc(), flush=True)
        body = "".join(parts).strip()
        body = re.sub(r"^```(?:markdown)?\s*|\s*```$", "", body)
        body = re.sub(r"^#\s+" + re.escape(title) + r"\s*\n", "", body)
        header = transcript_header(title, str(proposal.get("doc_url") or ""), self._now().date(), str(proposal.get("updated") or ""))
        self._audit({"type": "handbook_transcribe", "title": title, "doc_id": proposal.get("doc_id"), "usage": usage_total, "chars": len(body)})
        return header + body.rstrip() + "\n"

    # --- 内部: 反映と状態 ---

    def _write(self, proposal: dict) -> Path:
        self._out_dir.mkdir(parents=True, exist_ok=True)
        path = self._out_dir / str(proposal.get("filename"))
        path.write_text(str(proposal.get("content") or ""), encoding="utf-8")
        return path

    def _drop(self, state: dict, proposal: dict, applied: bool = False, declined: bool = False) -> None:
        state["proposals"] = [p for p in state.get("proposals") or [] if p.get("filename") != proposal.get("filename")]
        doc_id = str(proposal.get("doc_id") or "")
        if doc_id and applied:
            state.setdefault("transcribed", {})[doc_id] = {"filename": proposal.get("filename"), "at": self._now().isoformat()}
        if doc_id and declined:
            state.setdefault("declined", {})[doc_id] = {"title": proposal.get("title"), "at": self._now().isoformat()}
        self._save(state)

    def _load(self) -> dict:
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, state: dict) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    def _audit(self, record: dict) -> None:
        try:
            from .audit import AuditLogger

            cfg = self._config
            AuditLogger(log_dir=cfg.resolve_path(cfg.audit_log_dir), retention_days=cfg.audit_log_retention_days).log(record)
        except Exception:
            print("[warn] 監査ログの記録に失敗: " + traceback.format_exc(), flush=True)


def _parse_iso(text: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=JST)


def content_hash(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:16]


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    from .chatwork import ChatworkClient
    from .config import Config

    parser = argparse.ArgumentParser(description="マニュアルリンク集の取り込みと新しいマニュアルの転記（管理者承認つき）")
    parser.add_argument("--now", action="store_true", help="時刻や1日1回の制限を無視して今すぐ取り込む")
    parser.add_argument("--dry-run", action="store_true", help="提案を投稿せず、違いと新しいマニュアルを表示するだけ")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    config = Config.load()
    syncer = HandbookSync(config, ChatworkClient(config.chatwork_api_token or ""))
    made = syncer.run_once(force=args.now, dry_run=args.dry_run)
    print(f"提案 {made} 件" if not args.dry_run else "（dry-run）")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
