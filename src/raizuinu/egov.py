"""e-Gov法令検索API（Version 2）クライアント。

法令・政令・省令の条文を取得する（著作権法13条により法令は著作権の対象外）。
APIキー不要の公開API。参照先は laws.e-gov.go.jp のみ。

エンドポイント（2026-08-13実測確認）:
- GET /keyword?keyword=...&limit=...  条文キーワード検索
- GET /laws?law_title=...&limit=...   法令名検索
- GET /law_data/{law_id}              法令本文（XMLをJSON化した木構造）
"""

from __future__ import annotations

import re
import unicodedata
from collections import OrderedDict
from typing import Any

import requests

API_BASE = "https://laws.e-gov.go.jp/api/2"
LAW_PAGE_BASE = "https://laws.e-gov.go.jp/law"
_TAG_STRIP = re.compile(r"<[^>]+>")

_KANJI_DIGITS = {"〇": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


class EgovError(Exception):
    """e-Gov API呼び出しの失敗。"""


class EgovClient:
    def __init__(self, timeout_seconds: int = 30, cache_size: int = 8) -> None:
        self._timeout = timeout_seconds
        # 法令全文は大きい（民法で数MB）ため、取得済み法令をプロセス内でLRUキャッシュ
        self._law_cache: OrderedDict[str, dict] = OrderedDict()
        self._cache_size = cache_size

    # --- 公開API ---

    def search(self, keyword: str, limit: int = 5) -> list[dict[str, str]]:
        """条文キーワード検索。ヒットした法令と該当箇所の抜粋を返す。"""
        data = self._get("/keyword", {"keyword": keyword, "limit": limit})
        results = []
        for item in data.get("items") or []:
            law_info = item.get("law_info") or {}
            revision = item.get("revision_info") or {}
            snippets = [
                _TAG_STRIP.sub("", str(s.get("text", "")))
                for s in (item.get("sentences") or [])[:3]
            ]
            law_id = str(law_info.get("law_id", ""))
            results.append(
                {
                    "law_id": law_id,
                    "law_num": str(law_info.get("law_num", "")),
                    "law_title": str(revision.get("law_title", "")),
                    "url": f"{LAW_PAGE_BASE}/{law_id}" if law_id else "",
                    "snippets": snippets,
                }
            )
        return results

    def search_by_title(self, title: str, limit: int = 5) -> list[dict[str, str]]:
        """法令名で検索する（キーワード検索で見つからない場合の補助）。"""
        data = self._get("/laws", {"law_title": title, "limit": limit})
        results = []
        for item in data.get("laws") or []:
            law_info = item.get("law_info") or {}
            revision = item.get("revision_info") or {}
            law_id = str(law_info.get("law_id", ""))
            results.append(
                {
                    "law_id": law_id,
                    "law_num": str(law_info.get("law_num", "")),
                    "law_title": str(revision.get("law_title", "")),
                    "url": f"{LAW_PAGE_BASE}/{law_id}" if law_id else "",
                    "snippets": [],
                }
            )
        return results

    def get_article(self, law_id: str, article: str | None = None) -> dict[str, str]:
        """条文の原文テキストを返す。articleを省略すると法令の冒頭（目次除く）を返す。"""
        law = self._get_law(law_id)
        full_text = law.get("law_full_text") or {}
        revision = law.get("revision_info") or {}
        title = str(revision.get("law_title", ""))
        url = f"{LAW_PAGE_BASE}/{law_id}"

        if article:
            num = normalize_article_number(article)
            node = _find_article(full_text, num)
            if node is None:
                raise EgovError(
                    f"{title} に第{num.replace('_', '条の')}条が見つかりません（条番号を確認してください）"
                )
            text = render_text(node)
        else:
            text = render_text(full_text)[:3000]
        return {"law_title": title, "law_id": law_id, "article": article or "", "url": url, "text": text}

    # --- 内部 ---

    def _get(self, path: str, params: dict) -> dict:
        try:
            resp = requests.get(
                API_BASE + path,
                params=params,
                timeout=self._timeout,
                headers={"Accept": "application/json"},
            )
        except requests.RequestException as exc:
            raise EgovError(f"e-Gov APIに接続できません: {exc}") from exc
        if resp.status_code == 404:
            raise EgovError("該当する法令が見つかりません（law_idを確認してください）")
        if resp.status_code >= 400:
            raise EgovError(f"e-Gov APIエラー: HTTP {resp.status_code}")
        return resp.json()

    def _get_law(self, law_id: str) -> dict:
        law_id = law_id.strip()
        # law_idはURLパスに埋め込むため形式を厳格に検証する（パス操作の防止）
        if not re.fullmatch(r"[0-9A-Za-z_]{5,40}", law_id):
            raise EgovError(f"law_idの形式が不正です: {law_id!r}")
        if law_id in self._law_cache:
            self._law_cache.move_to_end(law_id)
            return self._law_cache[law_id]
        data = self._get(f"/law_data/{law_id}", {})
        self._law_cache[law_id] = data
        while len(self._law_cache) > self._cache_size:
            self._law_cache.popitem(last=False)
        return data


def normalize_article_number(article: str) -> str:
    """条番号表記を e-Gov の Num 形式（例: '24_5'）へ正規化する。

    受け付ける例: '522' '第522条' '24条の5' '第二十四条の五' '24_5' '24-5'
    """
    s = unicodedata.normalize("NFKC", str(article)).strip()
    s = s.replace("第", "").replace("条", "の").replace("-", "の").replace("_", "の")
    s = re.sub(r"の+", "の", s).strip("の ")
    parts = [p.strip() for p in s.split("の") if p.strip()]
    nums = []
    for part in parts:
        if part.isdigit():
            nums.append(str(int(part)))
        else:
            value = _kanji_to_int(part)
            if value is None:
                raise EgovError(f"条番号を解釈できません: {article}")
            nums.append(str(value))
    if not nums:
        raise EgovError(f"条番号を解釈できません: {article}")
    return "_".join(nums)


def _kanji_to_int(text: str) -> int | None:
    """漢数字（〜九千台まで）を整数へ。解釈不能ならNone。"""
    if not text or any(c not in "〇一二三四五六七八九十百千" for c in text):
        return None
    total, current = 0, 0
    for c in text:
        if c in _KANJI_DIGITS:
            current = current * 10 + _KANJI_DIGITS[c]
        elif c == "十":
            total += (current or 1) * 10
            current = 0
        elif c == "百":
            total += (current or 1) * 100
            current = 0
        elif c == "千":
            total += (current or 1) * 1000
            current = 0
    return total + current


def _find_article(node: Any, num: str) -> dict | None:
    if isinstance(node, dict):
        if node.get("tag") == "Article" and (node.get("attr") or {}).get("Num") == num:
            return node
        for child in node.get("children") or []:
            found = _find_article(child, num)
            if found:
                return found
    return None


def render_text(node: Any) -> str:
    """XML木構造からテキストを組み立てる（段落・号で改行）。"""
    parts: list[str] = []

    def walk(n: Any) -> None:
        if isinstance(n, str):
            parts.append(n)
            return
        if not isinstance(n, dict):
            return
        tag = n.get("tag", "")
        if tag in ("Paragraph", "Item", "ArticleCaption", "ArticleTitle"):
            parts.append("\n")
        for child in n.get("children") or []:
            walk(child)

    walk(node)
    text = "".join(parts)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
