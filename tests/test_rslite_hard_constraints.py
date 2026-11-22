"""rslite（macos-native/）的源码扫描硬约束。

为什么扫源码：这两类问题只在真机并发/实时线程上偶发，CI 的 macOS runner 跑不到
那条路径；而它们怕的都是「以后新写一处又犯同样的错」，扫全部源码才拦得住。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "macos-native" / "Sources" / "rslite"


def _closure_after(text: str, start: int) -> str:
    """从 start 之后第一个 `{` 起，按花括号配对取出整个闭包体（不能在第一个 `}` 截断）。"""
    open_at = text.find("{", start)
    if open_at < 0:
        return ""
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[open_at:i + 1]
    return text[open_at:]


def _swift_files():
    return sorted(SOURCES.rglob("*.swift"))


def test_hc_s01_tap_buffers_are_copied_before_crossing_async_boundary():
    """硬约束 HC-S01：installTap 回调交来的缓冲必须先复制再 yield。

    违反的是什么：tap 闭包里直接 `yield(buffer)`。为什么不行：AVAudioEngine 会
    复用这个缓冲，消费端在另一个任务里读到的可能已被下一块覆盖或释放，轻则
    识别输入错乱，重则 EXC_BAD_ACCESS。必须怎么改：在闭包里拷贝成新的
    AVAudioPCMBuffer（参考 MicSource.copy / ProcessTapSource.copy）再 yield。
    """
    offenders = []
    assert _swift_files(), "HC-S01：找不到 rslite 源码，守卫不能空跑通过"
    for path in _swift_files():
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"installTap\(", text):
            closure = _closure_after(text, match.end())
            if re.search(r"yield\(\s*buffer\s*\)", closure):
                line = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{path.relative_to(ROOT)}:{line}")
    assert not offenders, (
        "HC-S01 违反：installTap 回调把引擎持有的缓冲直接 yield 出去了："
        + ", ".join(offenders)
        + "。AVAudioEngine 会复用该缓冲，必须先拷贝成新的 AVAudioPCMBuffer 再 yield。"
    )


def test_hc_s02_headless_stdout_is_written_one_line_at_a_time():
    """硬约束 HC-S02：headless 的 stdout 只能经 RSLite.writeLine 整行写一次。

    违反的是什么：在别处直接 FileHandle.standardOutput.write。为什么不行：事件
    回调来自多条线程，payload 和换行分两次写会交错成坏 JSON 行，下游按行解析
    丢事件。必须怎么改：调用 RSLite.writeLine(data)，它在锁里整行写一次。
    """
    offenders = []
    for path in _swift_files():
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"FileHandle\.standardOutput\.write\(", text):
            before = text[:match.start()]
            fn = before.rfind("func ")
            if "func writeLine" not in before[fn:fn + 40]:
                line = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{path.relative_to(ROOT)}:{line}")
    assert not offenders, (
        "HC-S02 违反：这些位置绕过 RSLite.writeLine 直接写 stdout："
        + ", ".join(offenders)
        + "。多线程下会交错成坏 JSON 行，必须改用 RSLite.writeLine(data)。"
    )
