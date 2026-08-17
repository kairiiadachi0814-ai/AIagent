"""ひな形（Word/Excel）へ値を差し込んで、新しい書類のバイト列を作る。

方針は「原本を極力そのまま使う」。書式・フォント・タブ位置・印刷設定・
フォームコントロールを壊さないため、ライブラリで開き直して保存し直すのではなく、
ファイル（zip）の中の必要な部分だけを書き換える。

- Word: word/document.xml の段落テキストを差し替える。プレースホルダは
  ひな形作成時に1つの<w:r>へ収めてあるため、単純な文字列置換で足りる。
  繰り返し行（宛先・送付書類）は、その段落を必要な数だけ複製する。
- Excel: 対象シートのセルだけを書き換える。文字列は共有文字列表を触らずに
  済むインライン文字列（t="inlineStr"）で入れる。
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import Any
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
# 段落IDは文書内で一意。段落を複製したら必ず外す
W14_PARA_ID = "{http://schemas.microsoft.com/office/word/2010/wordml}paraId"
_XMLNS_RE = re.compile(r'xmlns:([A-Za-z0-9_]+)="([^"]+)"')
_ROOT_TAG_RE = re.compile(r"<w:document\b[^>]*>")
_PLACEHOLDER_RE = re.compile(r"\{\{[a-z_]+\}\}")


class TemplateError(Exception):
    """ひな形の差し込みに失敗した（プレースホルダの取りこぼし等）。"""


# --- Word ---


def _para_text(p: ET.Element) -> str:
    return "".join(t.text or "" for t in p.iter(W + "t"))


def _replace_in_para(p: ET.Element, values: dict[str, str]) -> None:
    for t in p.iter(W + "t"):
        text = t.text or ""
        if "{{" not in text:
            continue
        for key, value in values.items():
            text = text.replace("{{" + key + "}}", value)
        t.text = text
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")


def render_docx(template: bytes, scalars: dict[str, str], repeats: dict[str, list[dict[str, str]]]) -> bytes:
    """Wordのひな形へ差し込む。

    scalars: {"date": "2026年8月17日", ...} を1回だけ置換する
    repeats: {"item_name": [{"item_name": "契約書", "item_qty": "1部"}, ...]}
             キーを含む段落を、リストの件数だけ複製して置換する。
             リストが空のときは、その段落を削除する
    """
    with zipfile.ZipFile(io.BytesIO(template)) as z:
        parts = {name: z.read(name) for name in z.namelist()}

    original = parts["word/document.xml"].decode("utf-8")
    for prefix, uri in _XMLNS_RE.findall(original[:4000]):
        ET.register_namespace(prefix, uri)
    root = ET.fromstring(parts["word/document.xml"])
    body = root.find(W + "body")

    for marker, rows in repeats.items():
        token = "{{" + marker + "}}"
        children = list(body)
        for index, child in enumerate(children):
            if child.tag != W + "p" or token not in _para_text(child):
                continue
            position = list(body).index(child)
            body.remove(child)
            for offset, row in enumerate(rows):
                clone = ET.fromstring(ET.tostring(child))
                clone.attrib.pop(W14_PARA_ID, None)  # 段落IDの重複を避ける
                _replace_in_para(clone, row)
                body.insert(position + offset, clone)
            break  # 目印の段落はひな形に1つだけ

    for p in body.iter(W + "p"):
        _replace_in_para(p, scalars)

    leftover = _PLACEHOLDER_RE.findall("".join(_para_text(p) for p in body.iter(W + "p")))
    if leftover:
        raise TemplateError(f"差し込めなかった項目があります: {sorted(set(leftover))}")

    xml = ET.tostring(root, encoding="unicode")
    original_root = _ROOT_TAG_RE.search(original)
    generated_root = _ROOT_TAG_RE.search(xml)
    if original_root and generated_root:
        # 使われていない名前空間宣言をElementTreeが落とすと、mc:Ignorableが
        # 未宣言の接頭辞を指してWordが開けなくなる。ルートタグは原本のまま使う
        xml = xml[: generated_root.start()] + original_root.group(0) + xml[generated_root.end() :]
    parts["word/document.xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n' + xml
    ).encode("utf-8")

    return _repack(parts)


# --- Excel ---

_CELL_RE_CACHE: dict[str, re.Pattern[str]] = {}


def _cell_re(ref: str) -> re.Pattern[str]:
    if ref not in _CELL_RE_CACHE:
        _CELL_RE_CACHE[ref] = re.compile(rf'<c r="{ref}"(?P<attrs>[^>]*?)(?:/>|>(?P<inner>.*?)</c>)', re.S)
    return _CELL_RE_CACHE[ref]


def _set_cell(sheet_xml: str, ref: str, value: Any) -> str:
    """セル1つを書き換える。書式（s属性）は残し、値だけ差し替える。

    文字列は共有文字列表を触らずに済むインライン文字列で入れる。
    """
    match = _cell_re(ref).search(sheet_xml)
    if not match:
        raise TemplateError(f"ひな形にセル {ref} が見つかりませんでした")
    attrs = re.sub(r'\s+t="[^"]*"', "", match.group("attrs")).rstrip("/").rstrip()
    if isinstance(value, (int, float)):
        cell = f'<c r="{ref}"{attrs}><v>{value}</v></c>'
    else:
        text = escape(str(value))
        cell = f'<c r="{ref}"{attrs} t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'
    return sheet_xml[: match.start()] + cell + sheet_xml[match.end() :]


def sheet_path_by_name(parts: dict[str, bytes], name: str) -> tuple[str, int, str]:
    """シート名から (xl/worksheets/sheetN.xml, 0始まりの位置, sheetId) を返す。

    sheetIdは計算チェーン（calcChain.xml）のi属性が指す値で、位置とは別物。
    """
    workbook = parts["xl/workbook.xml"].decode("utf-8")
    rels = parts["xl/_rels/workbook.xml.rels"].decode("utf-8")
    targets = {
        m.group(1): m.group(2)
        for m in re.finditer(r'Id="(rId\d+)"[^>]*Target="([^"]+)"', rels)
    }
    sheets = re.findall(
        r'<sheet name="([^"]+)" sheetId="(\d+)"[^>]*r:id="(rId\d+)"', workbook
    )
    for index, (sheet_name, sheet_id, rid) in enumerate(sheets):
        if sheet_name == name:
            target = targets[rid].lstrip("/")
            if not target.startswith("xl/"):
                target = "xl/" + target
            return target, index, sheet_id
    raise TemplateError(f"ひな形にシート「{name}」が見つかりませんでした")


def render_xlsx(template: bytes, sheet_name: str, cells: dict[str, Any]) -> bytes:
    """Excelのひな形の指定シートへ値を差し込み、そのシートを開いた状態にする。"""
    with zipfile.ZipFile(io.BytesIO(template)) as z:
        parts = {name: z.read(name) for name in z.namelist()}

    path, index, sheet_id = sheet_path_by_name(parts, sheet_name)
    sheet_xml = parts[path].decode("utf-8")
    overwritten_formulas = []
    for ref, value in cells.items():
        if value is None or value == "":
            continue
        before = _cell_re(ref).search(sheet_xml)
        if before and "<f" in (before.group("inner") or ""):
            overwritten_formulas.append(ref)
        sheet_xml = _set_cell(sheet_xml, ref, value)
    parts[path] = sheet_xml.encode("utf-8")

    # 数式を消したセルが計算チェーンに残ると、Excelが壊れたファイルとして扱う。
    # i属性はシートID。同じ座標が他シートにもあるため、必ずIDまで見て抜く
    chain = parts.get("xl/calcChain.xml")
    if chain and overwritten_formulas:
        text = chain.decode("utf-8")
        for ref in overwritten_formulas:
            text = re.sub(rf'<c r="{ref}" i="{sheet_id}"[^>]*/>', "", text)
        if not re.search(r"<c\b", text):
            # 中身が空の<calcChain>は不正。参照ごと外す
            parts.pop("xl/calcChain.xml", None)
            parts["[Content_Types].xml"] = re.sub(
                r'<Override PartName="/xl/calcChain\.xml"[^>]*/>',
                "",
                parts["[Content_Types].xml"].decode("utf-8"),
            ).encode("utf-8")
            parts["xl/_rels/workbook.xml.rels"] = re.sub(
                r'<Relationship[^>]*Target="calcChain\.xml"[^>]*/>',
                "",
                parts["xl/_rels/workbook.xml.rels"].decode("utf-8"),
            ).encode("utf-8")
        else:
            parts["xl/calcChain.xml"] = text.encode("utf-8")
    parts["xl/workbook.xml"] = re.sub(
        r'(<workbookView\b[^>]*?)\sactiveTab="\d+"',
        rf'\1 activeTab="{index}"',
        parts["xl/workbook.xml"].decode("utf-8"),
        count=1,
    ).encode("utf-8")
    for name in list(parts):
        if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"):
            xml = parts[name].decode("utf-8")
            xml = re.sub(r'\s+tabSelected="1"', "", xml, count=1)
            if name == path:
                xml = xml.replace("<sheetView ", '<sheetView tabSelected="1" ', 1)
            parts[name] = xml.encode("utf-8")

    return _repack(parts)


def _repack(parts: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in parts.items():
            z.writestr(name, data)
    return buf.getvalue()
