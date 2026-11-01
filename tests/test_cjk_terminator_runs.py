"""CJK 连续全角终止符合并测试：验证 ……、？！、？？ 等不被拆成多句。"""
import pytest
from realtime_subtitle.translate.text_rules import _split_sentences


@pytest.mark.parametrize("text,expected_sentences,expected_rest", [
    # 基础用例：连续全角省略号
    ("他说……我们走吧。", ["他说……", "我们走吧。"], ""),
    # 只有连续省略号结尾
    ("他说……", ["他说……"], ""),
    # 连续省略号 + 正常句号
    ("嗯……好的。然后呢", ["嗯……", "好的。"], "然后呢"),
    # 混合全角标点：？！
    ("这是什么？！好吧。", ["这是什么？！", "好吧。"], ""),
    # 连续问号
    ("真的吗？？", ["真的吗？？"], ""),
])
def test_cjk_consecutive_terminators_merged(text, expected_sentences, expected_rest):
    """连续的全角终止符应被视为一个终止符，不产生只含标点的空句子。"""
    sentences, rest = _split_sentences(text, final=True, lang="zh")
    assert sentences == expected_sentences, f"sentences: got {sentences}, expected {expected_sentences}"
    assert rest == expected_rest, f"rest: got {rest!r}, expected {expected_rest!r}"


def test_german_ellipsis_unchanged():
    """德语路径不受影响：单个 … 仍按原逻辑处理。"""
    text = "Er sagte… dann ging er."
    sentences, rest = _split_sentences(text, final=True, lang="de")
    # 德语下 … 后面没有空格，但 _SENTENCE_TERMINATOR 要求 (?=\s|$)
    # 所以 … 不会被当成终止符，整句作为一个句子
    assert sentences == ["Er sagte… dann ging er."], sentences
    assert rest == ""


def test_chinese_halfwidth_decimal_not_split():
    """中文里的半角小数点不被切开（3.5 元）。"""
    text = "价格是3.5元。好的。"
    sentences, rest = _split_sentences(text, final=True, lang="zh")
    assert sentences == ["价格是3.5元。", "好的。"], sentences
    assert rest == ""