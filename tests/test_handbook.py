from raizuinu.handbook import HandbookLoader


def make_repo(tmp_path):
    (tmp_path / "銀行明細取得.md").write_text(
        "# 銀行明細取得\n\n## りそな銀行\n手順A\n\n## 奈良信用金庫\n手順B\n",
        encoding="utf-8",
    )
    (tmp_path / "海外送金.md").write_text("# 海外送金\n本文\n", encoding="utf-8")
    (tmp_path / "引き継ぎ_19.md").write_text("# 引き継ぎ\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("# dev\n", encoding="utf-8")
    (tmp_path / "note.txt").write_text("not markdown", encoding="utf-8")
    return HandbookLoader(
        roots=[tmp_path],
        include=["*.md"],
        exclude=["引き継ぎ_*.md", "CLAUDE.md"],
        cache_ttl_seconds=300,
    )


class TestHandbookLoader:
    def test_loads_only_included_md_files(self, tmp_path):
        handbook = make_repo(tmp_path).load()
        assert handbook.file_names == {"銀行明細取得.md", "海外送金.md"}

    def test_extracts_headings(self, tmp_path):
        handbook = make_repo(tmp_path).load()
        f = next(x for x in handbook.files if x.name == "銀行明細取得.md")
        assert "りそな銀行" in f.headings
        assert "奈良信用金庫" in f.headings

    def test_render_wraps_files_with_names(self, tmp_path):
        rendered = make_repo(tmp_path).load().render()
        assert '<file name="銀行明細取得.md">' in rendered
        assert "手順A" in rendered

    def test_cache_within_ttl(self, tmp_path):
        loader = make_repo(tmp_path)
        first = loader.load()
        (tmp_path / "新規.md").write_text("# 新規\n", encoding="utf-8")
        assert loader.load() is first  # TTL内は再読込しない
        assert "新規.md" in loader.load(force=True).file_names


class TestNormalizeCitation:
    def test_exact_and_without_extension(self, tmp_path):
        handbook = make_repo(tmp_path).load()
        assert handbook.normalize_citation("銀行明細取得.md") == "銀行明細取得.md"
        assert handbook.normalize_citation("銀行明細取得") == "銀行明細取得.md"

    def test_path_prefix_stripped(self, tmp_path):
        handbook = make_repo(tmp_path).load()
        assert handbook.normalize_citation("handbook/銀行明細取得.md") == "銀行明細取得.md"

    def test_nonexistent_returns_none(self, tmp_path):
        handbook = make_repo(tmp_path).load()
        assert handbook.normalize_citation("存在しないファイル.md") is None
        assert handbook.normalize_citation("") is None
