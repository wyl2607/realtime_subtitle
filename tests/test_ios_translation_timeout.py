"""硬约束 HC-P03：本机翻译的单句调用必须有超时。

为什么要盯住这件事：`LocalEngine` 把每句翻译挂在一条串行尾链上
（`translationTail`），下一句要等上一句的 `TranslationSession.translate` 返回。
Translation 框架一旦对某一句不返回（资源被系统换出、会话失效等），后面每一句
都会一直排队等下去：用户看到原文照常上屏、译文从某一句起永久停住，而引擎
既不报错也不自愈。超时后要按现有「翻译失败只显示原文」的路径收尾，并放行
下一句。

判据是静态的：所有 `session.translate(` 调用点都必须在超时包装里。
失败时不要加 catch 兜底或豁免，直接给调用点套上 `translateWithTimeout`。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "ios" / "Engine" / "LocalEngine.swift"

# TranslationSession 的单句调用；`session.translate(` 是唯一形态。
_TRANSLATE_CALL = re.compile(r"session\.translate\s*\(")
_TIMEOUT_CONST = re.compile(r"translationTimeout\w*\s*:\s*TimeInterval\s*=\s*([0-9.]+)")


def _function_span(text: str, signature: str) -> tuple[int, int]:
    """取 Swift 函数体的 [start, end) 偏移，找不到签名直接失败。"""
    match = re.search(re.escape(signature), text)
    assert match is not None, f"找不到函数签名：{signature}"
    start = text.index("{", match.end())
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return start, index + 1
    raise AssertionError(f"{signature} 的花括号没有配平")


def _function_body(text: str, signature: str) -> str:
    start, end = _function_span(text, signature)
    return text[start:end]


def test_hc_p03_translation_calls_are_wrapped_with_a_timeout():
    """硬约束 HC-P03：每处 `TranslationSession.translate` 都必须有超时。

    违反的表现：有调用点绕过了 translateWithTimeout。

    为什么不行：翻译是串行尾链，一句不返回，后面所有句子的译文和收尾回调都
    被卡住，用户只看到原文且引擎不会自愈。
    必须怎么改：所有单句调用统一走 `translateWithTimeout(session:text:)`，
    超时后按翻译失败处理（只显示原文）并继续下一句。
    """
    text = ENGINE.read_text(encoding="utf-8")
    helper_start, helper_end = _function_span(text, "func translateWithTimeout(")
    assert _TRANSLATE_CALL.search(text[helper_start:helper_end]), (
        "违反 HC-P03：translateWithTimeout 里没有真正调用 session.translate。"
    )

    offenders = []
    for match in _TRANSLATE_CALL.finditer(text):
        if not (helper_start <= match.start() < helper_end):
            line = text.count("\n", 0, match.start()) + 1
            offenders.append(f"LocalEngine.swift:{line}")
    assert not offenders, (
        "违反 HC-P03：这些位置直接调用了 session.translate，没经过超时包装：\n  "
        + "\n  ".join(offenders)
        + "\n必须怎么改：改成 translateWithTimeout(session:text:)，超时按翻译失败"
        "（只显示原文）收尾并放行下一句。"
    )

    calls = _TRANSLATE_CALL.findall(text)
    assert len(calls) == 1, (
        f"违反 HC-P03：全文有 {len(calls)} 处 session.translate，应当只有超时包装里的那一处。"
    )


def test_hc_p03_serial_tail_uses_the_timeout_helper():
    """硬约束 HC-P03：串行尾链（translateAndSettle）必须走超时包装。

    违反的表现：translateAndSettle 里没有调用 translateWithTimeout。

    为什么不行：串行尾链就是「一句卡住、后面全卡住」的那条路径；
    漏这一处等于修复没生效。
    必须怎么改：`translateAndSettle` 里的单句翻译改调 translateWithTimeout。
    """
    text = ENGINE.read_text(encoding="utf-8")
    body = _function_body(text, "func translateAndSettle(")
    assert "translateWithTimeout(" in body, (
        "违反 HC-P03：translateAndSettle 没有调用 translateWithTimeout。"
        "串行尾链必须带超时，否则一句不返回后面全部卡住。"
    )


def test_hc_p03_timeout_is_about_ten_seconds():
    """硬约束 HC-P03：超时常量必须存在且约 10 秒。

    违反的表现：找不到常量或数值偏离太多。

    为什么不行：没有常量就只能散写魔法数；太大等于没超时，太小会把正常的
    首句准备误判成失败（本机翻译首句包含模型准备时间）。
    必须怎么改：按周围常量风格定义一个约 10 秒的 TimeInterval 常量。
    """
    text = ENGINE.read_text(encoding="utf-8")
    match = _TIMEOUT_CONST.search(text)
    assert match is not None, (
        "违反 HC-P03：没有找到 translationTimeout*: TimeInterval 常量。"
    )
    value = float(match.group(1))
    assert 5.0 <= value <= 20.0, (
        f"违反 HC-P03：超时是 {value} 秒，偏离 10 秒太多。"
    )
