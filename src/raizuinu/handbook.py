"""ハンドブック（Markdown群）の取得と見出し索引。

Phase 1 はリポジトリが小さいため全ファイル取得方式（要件定義書4章）。
参照ルートは設定 `handbook.roots`（現状はリポジトリ直下のフラット構成、
将来 `handbook/`・`sops/` 階層へ移行しても設定変更のみで追従できる）。
"""

from __future__ import annotations

import fnmatch
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


@dataclass
class HandbookFile:
    name: str          # ファイル名（例: 銀行明細取得.md）
    path: Path
    content: str
    headings: list[str] = field(default_factory=list)


@dataclass
class Handbook:
    files: list[HandbookFile]
    loaded_at: float

    @property
    def file_names(self) -> set[str]:
        return {f.name for f in self.files}

    def normalize_citation(self, cited: str) -> str | None:
        """出典として挙げられたファイル名を実在ファイル名に正規化する。

        「.md」の有無・空白差を許容し、実在しなければ None（出典の捏造検知）。
        """
        candidate = cited.strip()
        if not candidate:
            return None
        for name in (candidate, candidate + ".md"):
            if name in self.file_names:
                return name
        # パス付きで挙げられた場合はファイル名部分だけで照合する
        base = candidate.replace("\\", "/").rsplit("/", 1)[-1]
        for name in (base, base + ".md"):
            if name in self.file_names:
                return name
        return None

    def render(self) -> str:
        """モデルに渡すハンドブック全文（ファイル名を明示した区切り付き）。"""
        parts = []
        for f in sorted(self.files, key=lambda x: x.name):
            parts.append(f"<file name=\"{f.name}\">\n{f.content}\n</file>")
        return "\n\n".join(parts)


class HandbookLoader:
    """ハンドブックの読み込み（TTL付きインメモリキャッシュ）。"""

    def __init__(
        self,
        roots: list[Path],
        include: list[str],
        exclude: list[str],
        cache_ttl_seconds: int = 300,
    ) -> None:
        self._roots = roots
        self._include = include
        self._exclude = exclude
        self._ttl = cache_ttl_seconds
        self._cache: Handbook | None = None

    def load(self, force: bool = False) -> Handbook:
        now = time.time()
        if not force and self._cache and now - self._cache.loaded_at < self._ttl:
            return self._cache
        files: list[HandbookFile] = []
        seen: set[str] = set()
        for root in self._roots:
            if not root.exists():
                continue
            for path in sorted(root.iterdir()):
                if not path.is_file():
                    continue
                if not any(fnmatch.fnmatch(path.name, pat) for pat in self._include):
                    continue
                if any(fnmatch.fnmatch(path.name, pat) for pat in self._exclude):
                    continue
                if path.name in seen:
                    continue
                seen.add(path.name)
                content = path.read_text(encoding="utf-8")
                headings = [m.group(2) for m in _HEADING_PATTERN.finditer(content)]
                files.append(
                    HandbookFile(name=path.name, path=path, content=content, headings=headings)
                )
        self._cache = Handbook(files=files, loaded_at=now)
        return self._cache
