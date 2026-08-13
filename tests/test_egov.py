import pytest

from raizuinu.egov import EgovError, normalize_article_number, render_text


class TestNormalizeArticleNumber:
    def test_plain_number(self):
        assert normalize_article_number("522") == "522"

    def test_dai_jo_notation(self):
        assert normalize_article_number("第522条") == "522"

    def test_branch_article(self):
        assert normalize_article_number("24条の5") == "24_5"
        assert normalize_article_number("第24条の5") == "24_5"
        assert normalize_article_number("24_5") == "24_5"
        assert normalize_article_number("24-5") == "24_5"

    def test_kanji_numbers(self):
        assert normalize_article_number("第二十四条の五") == "24_5"
        assert normalize_article_number("第五百二十二条") == "522"

    def test_zenkaku_digits(self):
        assert normalize_article_number("第５２２条") == "522"

    def test_invalid_raises(self):
        with pytest.raises(EgovError):
            normalize_article_number("不明")


class TestLawIdValidation:
    def test_malformed_law_id_rejected_before_request(self):
        from raizuinu.egov import EgovClient

        client = EgovClient()
        with pytest.raises(EgovError):
            client.get_article("../etc/passwd", "1")
        with pytest.raises(EgovError):
            client.get_article("abc?query=1", "1")


class TestRenderText:
    def test_renders_article_tree(self):
        node = {
            "tag": "Article",
            "attr": {"Num": "522"},
            "children": [
                {"tag": "ArticleCaption", "children": ["（契約の成立と方式）"]},
                {"tag": "ArticleTitle", "children": ["第五百二十二条"]},
                {
                    "tag": "Paragraph",
                    "children": [
                        {"tag": "Sentence", "children": ["契約は、申込みに対して相手方が承諾をしたときに成立する。"]}
                    ],
                },
            ],
        }
        text = render_text(node)
        assert "（契約の成立と方式）" in text
        assert "第五百二十二条" in text
        assert "承諾をしたときに成立する" in text
