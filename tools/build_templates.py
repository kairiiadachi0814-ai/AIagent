"""Boxの書類送付状ひな形から、白紙のひな形（templates/）を作る一回きりのツール。

Boxにある3つの送付状ファイルは、過去に送った分が1ファイルに積み上がった状態で、
取引先名・担当者名・送付書類がそのまま残っている。そのままリポジトリへ入れると
過去の相手先情報がVPSへ渡ってしまうため、次の手順で白紙化する。

1. 1通分の段落だけを切り出す（どの範囲かは SPECS に明記し、中身を突き合わせて検証する）
2. 可変部（日付・宛先・自社担当者・送付書類の明細）をプレースホルダに置き換える
3. 書式（フォント・サイズ・配置・タブ位置）はそのまま残す

実行はローカルPC（Box同期フォルダが見える環境）のみ。VPSでは動かさない。

    python tools/build_soufujo_templates.py
"""

from __future__ import annotations

import copy
import re

import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
BOX = Path("C:/Users/admin/Box/⑨各種書類ひな形データ/書類送付（郵送・FAX他）")
OUT_DIR = Path(__file__).resolve().parents[1] / "templates"

# 差込み用のプレースホルダ。値は必ず1つの<w:r>に収める（runが分割されると置換できないため）
PH_DATE = "{{date}}"
PH_TO = "{{to_line}}"  # 宛先。1行につき1段落へ複製する
PH_STAFF = "{{staff}}"
PH_ITEM = "{{item_name}}"  # 送付書類。1件につき1段落へ複製する
PH_QTY = "{{item_qty}}"

SPECS = [
    {
        "out": "書類送付状_ライズ.docx",
        "src": "書類送付状_ライズ.docx",
        # 2026年5月11日付 南都銀行あての1通（末尾から2通目。標準の書式が崩れていない）
        "paras": (657, 694),
        "expect": {657: "2026年5月11日", 658: "株式会社南都銀行", 671: "書類送付のご案内", 694: "以上"},
        "edits": {
            657: PH_DATE,
            658: PH_TO,
            659: None,  # 宛先2行目・3行目は削除し、1行目を必要な数だけ複製する
            660: None,
            668: f"担当： 経理財務部　{PH_STAFF}",
            684: f"■{PH_ITEM}\t\t{PH_QTY}",
        },
    },
    {
        "out": "書類送付状_ヤマトライジング.docx",
        "src": "書類送付状_ヤマトライジング.docx",
        "paras": (0, 38),
        "expect": {0: "2026年7月22日", 9: "株式会社ヤマトライジング", 16: "書類送付のご案内", 38: "以上"},
        "edits": {
            0: PH_DATE,
            2: PH_TO,
            3: None,
            13: f"担当： 経理財務部　{PH_STAFF}",
            29: f"■{PH_ITEM}\t\t{PH_QTY}",
        },
    },
    {
        "out": "書類送付状_楽天軒.docx",
        "src": "書類送付状_楽天軒.docx",
        # 2025年5月28日付 百五銀行あての1通（末尾の1通）
        "paras": (39, 77),
        "expect": {39: "2025年5月28日", 44: "ＲＡＫＵＴＥＮＫＥＮ株式会社", 51: "書類送付のご案内", 77: "以上"},
        "edits": {
            39: PH_DATE,
            41: PH_TO,
            42: None,
            48: f"担当： {PH_STAFF}",
            68: f"・{PH_ITEM}\t{PH_QTY}",
        },
    },
]


def para_text(p: ET.Element) -> str:
    return "".join(t.text or "" for t in p.iter(W + "t"))


def set_para_text(p: ET.Element, text: str) -> None:
    """段落の本文を text に差し替える。\\t はWordのタブにする。

    書式は先頭runの<w:rPr>を引き継ぐ。プレースホルダを1つのrunに収めるため、
    元の複数runは捨てて作り直す。
    """
    runs = p.findall(W + "r")
    rpr = None
    for r in runs:
        found = r.find(W + "rPr")
        if found is not None:
            rpr = copy.deepcopy(found)
            break
    for child in list(p):
        if child.tag in (W + "r", W + "hyperlink", W + "bookmarkStart", W + "bookmarkEnd"):
            p.remove(child)
    for index, chunk in enumerate(text.split("\t")):
        if index:  # 区切りの位置にタブrunを入れる
            tab_run = ET.SubElement(p, W + "r")
            if rpr is not None:
                tab_run.append(copy.deepcopy(rpr))
            ET.SubElement(tab_run, W + "tab")
        if not chunk:
            continue
        run = ET.SubElement(p, W + "r")
        if rpr is not None:
            run.append(copy.deepcopy(rpr))
        t = ET.SubElement(run, W + "t")
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        t.text = chunk


def strip_page_breaks(p: ET.Element) -> None:
    """切り出した1通に、元ファイルの改ページが残らないようにする。"""
    for run in p.findall(W + "r"):
        for br in run.findall(W + "br"):
            if br.get(W + "type") == "page":
                run.remove(br)
        for rendered in run.findall(W + "lastRenderedPageBreak"):
            run.remove(rendered)


_XMLNS_RE = re.compile(r'xmlns:([A-Za-z0-9_]+)="([^"]+)"')
_ROOT_TAG_RE = re.compile(r"<w:document\b[^>]*>")


def build(spec: dict) -> Path:
    src = BOX / spec["src"]
    with zipfile.ZipFile(src) as z:
        parts = {name: z.read(name) for name in z.namelist()}

    original = parts["word/document.xml"].decode("utf-8")
    # 元の接頭辞（w14・mc等）をそのまま使わせる。ElementTreeに任せるとns0等へ
    # 付け替えられ、mc:Ignorableが未宣言の接頭辞を指してWordが開けなくなる
    for prefix, uri in _XMLNS_RE.findall(original[:2000]):
        ET.register_namespace(prefix, uri)
    root = ET.fromstring(parts["word/document.xml"])
    body = root.find(W + "body")
    children = list(body)
    paras = [c for c in children if c.tag == W + "p"]

    for index, expected in spec["expect"].items():
        actual = para_text(paras[index]).strip()
        if expected not in actual:
            raise SystemExit(
                f"{spec['src']}: 段落{index}の中身が想定と違います。"
                f"ひな形が更新された可能性があります。\n  期待: {expected}\n  実際: {actual}"
            )

    start, end = spec["paras"]
    keep = {id(p) for p in paras[start : end + 1]}
    drop = {id(paras[i]) for i, new in spec["edits"].items() if new is None}
    sect_pr = body.find(W + "sectPr")

    for child in children:
        if child.tag == W + "sectPr":
            continue
        if id(child) not in keep or id(child) in drop:
            body.remove(child)

    for index, new_text in spec["edits"].items():
        if new_text is None:
            continue
        set_para_text(paras[index], new_text)
    for p in paras[start : end + 1]:
        strip_page_breaks(p)

    if sect_pr is not None:  # 用紙サイズ・余白は元のまま最後に置く
        body.remove(sect_pr)
        body.append(sect_pr)

    # ルートタグは原本のものへ戻す。使われていない名前空間宣言（mc:Ignorableが
    # 参照するw15・w16等）はElementTreeが落としてしまうため
    xml = ET.tostring(root, encoding="unicode")
    original_root = _ROOT_TAG_RE.search(original)
    generated_root = _ROOT_TAG_RE.search(xml)
    if not original_root or not generated_root:
        raise SystemExit(f"{spec['src']}: w:document のルートタグを見つけられませんでした")
    xml = xml[: generated_root.start()] + original_root.group(0) + xml[generated_root.end() :]
    parts["word/document.xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n' + xml
    ).encode("utf-8")

    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / spec["out"]
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in parts.items():
            z.writestr(name, data)
    return out


# --- FAX送付状（Excel） ---

# 「汎用」以外の取引先別シートは、そのまま宛先の連絡先台帳として使える。
# 依頼で「りそな銀行あて」と言われたときにFAX番号を引けるよう、JSONへ書き出す。
FAX_SRC = "FAX送付状.xlsx"
FAX_CELLS = {
    "company": "D10",
    "person": "D12",
    "fax": "D14",
    "tel": "D16",
    "subject": "E19",
    "sender_staff": "O12",
}


def build_fax() -> None:
    import json
    import shutil

    from openpyxl import load_workbook

    src = BOX / FAX_SRC
    out = OUT_DIR / FAX_SRC
    OUT_DIR.mkdir(exist_ok=True)
    # 原本をそのまま置く。フォームコントロール・印刷設定を壊さないため、
    # 差し込みは実行時にシートXMLを直接書き換える（templatefill.render_xlsx）
    shutil.copyfile(src, out)

    wb = load_workbook(src)
    if "汎用" not in wb.sheetnames:
        raise SystemExit(f"{FAX_SRC}: 「汎用」シートが見つかりません")
    directory = []
    for name in wb.sheetnames:
        if name in ("汎用", "FAX送信状（原本）", "プルダウンリスト"):
            continue
        ws = wb[name]
        entry = {"sheet": name}
        for key, ref in FAX_CELLS.items():
            value = ws[ref].value
            if isinstance(value, str) and value.strip():
                entry[key] = value.strip()
        body = []
        for row in range(23, 30):
            for col in ("B", "C"):
                value = ws[f"{col}{row}"].value
                if isinstance(value, str) and value.strip():
                    body.append(value.strip())
                    break
        if body:
            entry["body"] = "\n".join(body)
        if entry.get("company"):
            directory.append(entry)

    path = OUT_DIR / "fax_destinations.json"
    path.write_text(
        json.dumps(directory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"{out.name}: 原本をコピー（{out.stat().st_size:,}バイト）")
    print(f"{path.name}: 宛先{len(directory)}件")
    for entry in directory:
        print(f"    {entry['company']} / {entry.get('person','-')} / FAX {entry.get('fax','-')}")


def main() -> int:
    if not BOX.exists():
        print(f"Box同期フォルダが見つかりません: {BOX}", file=sys.stderr)
        return 1
    build_fax()
    for spec in SPECS:
        out = build(spec)
        with zipfile.ZipFile(out) as z:
            root = ET.fromstring(z.read("word/document.xml"))
        paras = [c for c in root.find(W + "body") if c.tag == W + "p"]
        texts = [para_text(p).strip() for p in paras]
        print(f"{out.name}: 段落{len(paras)}件")
        for t in texts:
            if t:
                print(f"    {t}")
        remaining = [t for t in texts if "様" in t and "{{" not in t]
        if remaining:
            print(f"  ※ 宛先らしき文字が残っています: {remaining}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
