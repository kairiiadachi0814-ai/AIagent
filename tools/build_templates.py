"""Boxの書類送付状ひな形から、白紙のひな形（templates/）を作る一回きりのツール。

Boxにある3つの送付状ファイルは、過去に送った分が1ファイルに積み上がった状態で、
取引先名・担当者名・送付書類がそのまま残っている。そのままリポジトリへ入れると
過去の相手先情報がVPSへ渡ってしまうため、次の手順で白紙化する。

1. 1通分の段落だけを切り出す（どの範囲かは SPECS に明記し、中身を突き合わせて検証する）
2. 可変部（日付・宛先・自社担当者・送付書類の明細）をプレースホルダに置き換える
3. 書式（フォント・サイズ・配置・タブ位置）はそのまま残す

実行はローカルPC（Box同期フォルダが見える環境）のみ。VPSでは動かさない。

    python tools/build_templates.py
"""

from __future__ import annotations

import copy
import io
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
# 差出人ブロック。会社ごとにひな形を作らず、companies.json の値を差し込む
PH_COMPANY = "{{sender_company}}"
PH_ADDR1 = "{{sender_address1}}"
PH_ADDR2 = "{{sender_address2}}"
PH_TEL = "{{sender_tel}}"
PH_DEPT = "{{sender_dept}}"

SPECS = [
    {
        # 標準の型（■で送付書類を並べる）。差出人は companies.json から差し込むため、
        # グループ会社が増えてもひな形は増やさない
        "out": "送付状_標準.docx",
        "src": "書類送付状_ライズ.docx",
        # 2026年5月11日付 南都銀行あての1通（末尾から2通目。標準の書式が崩れていない）
        "paras": (657, 694),
        "expect": {657: "2026年5月11日", 658: "株式会社南都銀行", 671: "書類送付のご案内", 694: "以上"},
        "edits": {
            657: PH_DATE,
            658: PH_TO,
            659: None,  # 宛先2行目・3行目は削除し、1行目を必要な数だけ複製する
            660: None,
            664: PH_COMPANY,
            665: PH_ADDR1,
            666: PH_ADDR2,
            667: f"TEL：{PH_TEL}",
            668: f"担当： {PH_DEPT}{PH_STAFF}",
            # 部数は右端で揃えず左詰め。区切りの空白は差し込む側が持つ
            # （「一式」のように部数を付けない行で末尾に空白を残さないため）
            684: f"■{PH_ITEM}{PH_QTY}",
        },
    },
    {
        # 楽天軒の型（・で並べる）
        "out": "送付状_楽天軒型.docx",
        "src": "書類送付状_楽天軒.docx",
        # 2025年5月28日付 百五銀行あての1通（末尾の1通）
        "paras": (39, 77),
        "expect": {39: "2025年5月28日", 44: "ＲＡＫＵＴＥＮＫＥＮ株式会社", 51: "書類送付のご案内", 77: "以上"},
        "edits": {
            39: PH_DATE,
            41: PH_TO,
            42: None,
            44: PH_COMPANY,
            45: PH_ADDR1,
            46: PH_ADDR2,
            47: f"TEL：{PH_TEL}",
            48: f"担当： {PH_DEPT}{PH_STAFF}",
            67: None,  # 「＜添付書類＞」の見出しは使わない（記から2行空けて本文）
            68: f"・{PH_ITEM}{PH_QTY}",
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


FAX_KEEP_SHEETS = ("プルダウンリスト", "汎用")

# 経理財務部の担当者名簿。Box原本のプルダウンは古いままなので、ここで差分を当てる。
# 名簿は「Chatworkの表示名から苗字を切り出すときの照合」と「Excelのドロップダウン」の
# 両方に効く。人の出入りがあったらこの2つを直して再生成する。
STAFF_REMOVED = ("倉本", "進地", "坂口")  # 退職・他部署（2026-08-17時点）
STAFF_ADDED = ("岩永",)  # 2026-09-09入社
# ドロップダウンが参照する列（プルダウンリスト!$B:$B）。空欄も選択肢に出るため、
# 使わない行は必ず空セルへ戻す
ROSTER_COLUMN = "B"
ROSTER_MAX_ROWS = 10


def build_roster(names: list[str]) -> list[str]:
    """原本のプルダウンに差分を当てた名簿を返す（並び順は原本を尊重）。"""
    kept = [n for n in names if n not in STAFF_REMOVED]
    return kept + [n for n in STAFF_ADDED if n not in kept]


def strip_to_general_sheet(data: bytes, keep: tuple[str, ...] = FAX_KEEP_SHEETS) -> bytes:
    """FAX送付状ひな形から取引先別のシートを取り除く。

    原本には取引先別のシートが15枚あり、他社の担当者名・FAX番号・過去の
    送信本文が入っている。そのまま同梱すると、1社あてに作った下書きの中に
    他14社の連絡先が付いてくるため、取引先別シートを落とす。
    連絡先は fax_destinations.json に切り出してあるので機能は落ちない。

    「プルダウンリスト」は残す。発信者の会社名・担当者名のドロップダウンが
    このシートを参照しており、消すと参照先を失うため。

    openpyxlで開き直すとチェックボックス・プルダウン・印刷設定が消えるため、
    zipの中身を直接編集する。
    """
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        parts = {name: z.read(name) for name in z.namelist()}

    workbook = parts["xl/workbook.xml"].decode("utf-8")
    rels = parts["xl/_rels/workbook.xml.rels"].decode("utf-8")
    targets = {
        m.group(1): m.group(2)
        for m in re.finditer(r'Id="(rId\d+)"[^>]*Target="([^"]+)"', rels)
    }
    sheets = re.findall(
        r'<sheet name="([^"]+)" sheetId="(\d+)"[^>]*r:id="(rId\d+)"[^>]*/>', workbook
    )
    missing = [name for name in keep if not any(name == s for s, _, _ in sheets)]
    if missing:
        raise SystemExit(f"{FAX_SRC}: シートが見つかりません: {missing}")

    def part_path(target: str) -> str:
        target = target.lstrip("/")
        return target if target.startswith("xl/") else "xl/" + target

    def related(sheet_path: str) -> list[str]:
        """シートが参照している部品（図形・フォームコントロール・印刷設定）。"""
        rels_path = sheet_path.replace("worksheets/", "worksheets/_rels/") + ".rels"
        if rels_path not in parts:
            return []
        found = [rels_path]
        for target in re.findall(r'Target="([^"]+)"', parts[rels_path].decode("utf-8")):
            found.append(part_path(target.replace("../", "")))
        return found

    # 1) 残すシートの共有文字列をインライン文字列へ移し、共有文字列表を空にする
    #    （表を残すと、シートを消しても他社名がファイル内に残ってしまう）
    shared = re.findall(
        r"<si>(.*?)</si>", parts["xl/sharedStrings.xml"].decode("utf-8"), re.S
    )

    def inline(match: re.Match[str]) -> str:
        attrs = match.group("attrs").replace(' t="s"', "")
        # ふりがな（<rPh>）は本文ではない。残すと「FAX送信状ソウシンジョウ」になる
        si = re.sub(r"<rPh\b.*?</rPh>", "", shared[int(match.group("idx"))], flags=re.S)
        text = "".join(re.findall(r"<t[^>]*>(.*?)</t>", si, re.S))
        return f'<c{attrs} t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'

    for sheet_name in keep:
        path = part_path(targets[next(r for n, _, r in sheets if n == sheet_name)])
        sheet_xml = re.sub(
            r'<c(?P<attrs>[^>]*\st="s"[^>]*)><v>(?P<idx>\d+)</v></c>',
            inline,
            parts[path].decode("utf-8"),
        )
        if sheet_name == "汎用":
            # 発信者欄は依頼ごとに変わる。会社名・部署・FAX・TEL（M10/M12/M14/M16）は
            # グループ会社ごとに companies.json から差し込み、担当者名（O12）は
            # 依頼者の姓を入れる。ひな形に特定の会社の情報を残すと、
            # 差し込みを取りこぼしたときに別会社の連絡先が載ったまま出てしまう
            def blank(match: re.Match[str]) -> str:
                # 値の型（t属性）は中身と一緒に落とす。書式（s属性）は残す
                attrs = re.sub(r'\s+t="[^"]*"', "", match.group("attrs"))
                return f'<c r="{match.group("ref")}"{attrs}/>'

            for ref in ("M10", "M12", "M14", "M16", "O12"):
                sheet_xml = re.sub(
                    rf'<c r="(?P<ref>{ref})"(?P<attrs>[^>]*?)(?:/>|>.*?</c>)',
                    blank,
                    sheet_xml,
                    count=1,
                )
        parts[path] = sheet_xml.encode("utf-8")
    parts["xl/sharedStrings.xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'count="0" uniqueCount="0"/>'
    ).encode("utf-8")

    # 2) 残すシート以外と、それが抱えている部品を消す
    removed: set[str] = set()
    for name, _sheet_id, rid in sheets:
        if name in keep:
            continue
        path = part_path(targets[rid])
        removed.update([path, *related(path)])
        workbook = re.sub(rf'<sheet name="{re.escape(name)}"[^>]*/>', "", workbook)
        rels = re.sub(rf'<Relationship Id="{rid}"[^>]*/>', "", rels)
    keep_ids = {sid for n, sid, _ in sheets if n in keep}
    chain = parts.get("xl/calcChain.xml", b"").decode("utf-8")
    chain = re.sub(
        r'<c r="[^"]+" i="(\d+)"[^>]*/>',
        lambda m: m.group(0) if m.group(1) in keep_ids else "",
        chain,
    )
    parts["xl/calcChain.xml"] = chain.encode("utf-8")

    for path in removed:
        parts.pop(path, None)
    content_types = parts["[Content_Types].xml"].decode("utf-8")
    for path in removed:
        content_types = re.sub(
            rf'<Override PartName="/{re.escape(path)}"[^>]*/>', "", content_types
        )
    parts["[Content_Types].xml"] = content_types.encode("utf-8")

    # 3) 1枚だけになるので、開いたときに必ずそのシートが選ばれるようにする
    workbook = re.sub(
        r'<workbookView\b[^>]*?/>',
        '<workbookView xWindow="-120" yWindow="-120" windowWidth="29040" '
        'windowHeight="15720" firstSheet="0" activeTab="0"/>',
        workbook,
        count=1,
    )
    parts["xl/workbook.xml"] = workbook.encode("utf-8")
    parts["xl/_rels/workbook.xml.rels"] = rels.encode("utf-8")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, blob in parts.items():
            z.writestr(name, blob)
    return buf.getvalue()


def apply_roster(data: bytes, staff: list[str]) -> bytes:
    """ひな形の「プルダウンリスト」シートを、渡した名簿の内容に揃える。

    ドロップダウンは列全体（$B:$B）を参照しているので、使わない行は
    空セルへ戻さないと空欄が選択肢として並ぶ。
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from raizuinu.templatefill import set_cell, sheet_path_by_name

    with zipfile.ZipFile(io.BytesIO(data)) as z:
        parts = {name: z.read(name) for name in z.namelist()}
    path, _index, _sheet_id = sheet_path_by_name(parts, "プルダウンリスト")
    sheet_xml = parts[path].decode("utf-8")
    for row in range(1, ROSTER_MAX_ROWS + 1):
        value = staff[row - 1] if row <= len(staff) else None
        sheet_xml = set_cell(sheet_xml, f"{ROSTER_COLUMN}{row}", value)
    parts[path] = sheet_xml.encode("utf-8")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, blob in parts.items():
            z.writestr(name, blob)
    return buf.getvalue()


def build_fax() -> None:
    import json

    from openpyxl import load_workbook

    src = BOX / FAX_SRC
    out = OUT_DIR / FAX_SRC
    OUT_DIR.mkdir(exist_ok=True)
    stripped = strip_to_general_sheet(src.read_bytes())

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

    # 発信者のドロップダウン（プルダウンリスト）は、そのまま社内の名簿として使える。
    # Chatworkの表示名から苗字を切り出すときの照合に使う
    pull = wb["プルダウンリスト"]
    roster = {
        "companies": [
            str(pull[f"A{row}"].value).strip()
            for row in range(1, 20)
            if isinstance(pull[f"A{row}"].value, str) and pull[f"A{row}"].value.strip()
        ],
        "staff": build_roster(
            [
                str(pull[f"{ROSTER_COLUMN}{row}"].value).strip()
                for row in range(1, ROSTER_MAX_ROWS + 1)
                if isinstance(pull[f"{ROSTER_COLUMN}{row}"].value, str)
                and pull[f"{ROSTER_COLUMN}{row}"].value.strip()
            ]
        ),
    }
    # ひな形のドロップダウンも同じ名簿に揃える（人が選ぶときに古い名前を出さない）
    out.write_bytes(apply_roster(stripped, roster["staff"]))
    roster_path = OUT_DIR / "staff_roster.json"
    roster_path.write_text(
        json.dumps(roster, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"{roster_path.name}: 会社{len(roster['companies'])}件 / 担当者{len(roster['staff'])}件")
    print("    " + "、".join(roster["staff"]))

    path = OUT_DIR / "fax_destinations.json"
    path.write_text(
        json.dumps(directory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    leaked = [e["company"] for e in directory if e["company"] in out.read_bytes().decode("latin-1", "ignore")]
    wb_out = load_workbook(out)
    print(f"{out.name}: シート{wb_out.sheetnames} / {out.stat().st_size:,}バイト")
    if leaked or wb_out.sheetnames != list(FAX_KEEP_SHEETS):
        raise SystemExit(f"{FAX_SRC}: 取引先データが残っています: {leaked or wb_out.sheetnames}")
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
