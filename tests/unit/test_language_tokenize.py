import pytest

from lkos.indexing.language import detect_lang
from lkos.indexing.tokenize import build_or_query, query_terms, to_fts_tokens


@pytest.mark.parametrize("text,lang", [
    ("项目二期的预算上限是多少？", "zh"),
    ("本季度使用了 Kubernetes 和 Docker 进行部署。", "zh"),  # 中文夹英文仍判为中文
    ("What is the retention policy for logs?", "en"),
    ("Application logs are retained for ninety days and then deleted automatically.", "en"),
    ("Où se trouve la machine à café ?", "fr"),
    ("Les bureaux sont ouverts du lundi au vendredi, de neuf heures à dix-huit heures.", "fr"),
    ("DeepSeekV3 671B | BF16 | 4台Atlas 800I A2(64G) | 4台机器参数面200G RoCE互联，共需32个200GE RoCE端口", "zh"),
    ("GDII 概览与国家洞察 Huawei Confidential", "zh"),
    ("Strong USA alignment or lack ability to execute. Focus more on basic solutions 中东中亚", "en"),
    ("12345 !!!", "other"),
    ("", "other"),
    ("ok", "other"),
])
def test_detect_lang(text, lang):
    assert detect_lang(text) == lang


def test_index_tokens_include_short_words_for_chinese():
    tokens = to_fts_tokens("二期预算上限为一百二十万元", "zh").split()
    assert "预算" in tokens and "上限" in tokens


def test_non_chinese_text_passes_through():
    assert to_fts_tokens("Le résumé", "fr") == "Le résumé"


def test_query_terms_drop_stopwords_and_duplicates():
    assert query_terms("项目二期的预算上限是多少？预算") == ["项目", "二期", "预算", "上限"]
    assert query_terms("What is the retention policy for logs?") == ["retention", "policy", "logs"]
    assert query_terms("Où se trouve la machine à café ?") == ["se", "trouve", "machine", "à", "café"]


def test_build_or_query_quotes_and_escapes():
    q = build_or_query('budget "OR" NEAR(x) AND -y*')
    assert q == '"budget" OR "near" OR "x" OR "y"'


def test_build_or_query_empty_for_only_stopwords():
    assert build_or_query("what is the") == ""
    assert build_or_query("？？？") == ""
