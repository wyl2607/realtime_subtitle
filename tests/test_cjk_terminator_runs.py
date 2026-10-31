"""回归测试：中文里**连续的终止符**必须整体算一个句子边界。

修复前 `_SENTENCE_TERMINATOR_CJK` 每次只吃一个终止字符，于是 "……" / "？！" /
"？？" 被当成多个边界，切出来的后一句只剩一个标点，照样送去翻译、上屏成
孤零零一行「…」。用例全部是本机现写的短句，不依赖 transcripts/ 存档。
"""
import pytest

from realtime_subtitle.translate.text_rules import _split_sentences


@pytest.mark.parametrize(
    "text, expected_sentences, expected_rest",
    [
        # 省略号夹在句中：前后两句，不能切出只剩 "…" 的碎片
        ("他说……我们走吧。", ["他说……", "我们走吧。"], ""),
        # 整句以省略号收尾
        ("他说……", ["他说……"], ""),
        # 省略号 + 句号 + 残句
        ("嗯……好的。然后呢", ["嗯……", "好的。"], "然后呢"),
        # 问号 + 感叹号连用
        ("这是什么？！好吧。", ["这是什么？！", "好吧。"], ""),
        # 连用问号，且整句到此结束
        ("真的吗？？", ["真的吗？？"], ""),
        # 半角省略号在中文里同样是一整个终止符（后面没有空格，
        # 靠 `[.!?](?=\s|$)` 是匹配不到的）
        ("他说...我们走吧。", ["他说...", "我们走吧。"], ""),
    ],
)
def test_consecutive_cjk_terminators_are_one_boundary(
    text, expected_sentences, expected_rest
):
    sentences, rest = _split_sentences(text, final=True, lang="zh")
    assert sentences == expected_sentences, sentences
    assert rest == expected_rest, rest


def test_de_ellipsis_path_is_untouched():
    """德语路径一个字都没动：省略号后的小写续行仍按原规则合并成一句。

    ☠️ 这里**不能**断言 ["Er sagte…", "dann ging er."]：`_boundary_is_real`
    的第③a 条（下一个词小写 = 上一句还没说完）本来就会否决这个边界，而本次
    修复不允许碰那条规则。所以断言的是修复前的真实输出——一条都没切。
    """
    sentences, rest = _split_sentences("Er sagte… dann ging er.", final=True, lang="de")
    assert sentences == ["Er sagte… dann ging er."], sentences
    assert rest == ""


def test_zh_decimal_point_is_not_a_terminator():
    """中文里夹的小数点不能被当成半角句号切开。"""
    sentences, rest = _split_sentences("价格是3.5元。好的。", final=True, lang="zh")
    assert sentences == ["价格是3.5元。", "好的。"], sentences
    assert rest == ""
