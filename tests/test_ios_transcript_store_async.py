"""硬约束 HC-P02：存档写盘不许阻塞调用方，也不许每句整文件重写。

为什么要盯住这件事：`TranscriptStore.ingest` 由主界面在「最近句对」变化时调用，
原来用 `queue.sync` 同步进去、每次变化都原子重写整天的文件（约 223 行起）。
句对每来一次（原文一次、译文一次）主线程就同步等一次磁盘写；一小时视频上千行，
越跑越慢。合并写（1 秒窗口）必须做到「不丢句」——内存状态立即合并，
只是落盘推迟；进后台/退出时 flush。

判据是静态的（`ingest` 函数体里不许出现 `queue.sync` / 直接 `write(`）加一条
生命周期检查（进后台要 flush）。行为层由 TranscriptStoreSelftest 覆盖。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / "ios" / "RSiOS" / "TranscriptStore.swift"
MODEL = ROOT / "ios" / "RSiOS" / "App" / "AppModel.swift"
ROOT_VIEW = ROOT / "ios" / "RSiOS" / "Pages" / "RootView.swift"


def _swift_function_body(text: str, signature: str) -> str:
    """取 Swift 函数体（按花括号配平），找不到签名直接失败。"""
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
                return text[start:index + 1]
    raise AssertionError(f"{signature} 的花括号没有配平")


def test_hc_p02_ingest_never_blocks_or_writes_inline():
    """硬约束 HC-P02：`ingest` 不许同步等待、不许直接写盘。

    违反的表现：ingest 函数体里出现 `.sync {` 或 `write(`。

    为什么不行：ingest 跑在主线程；`queue.sync` 会和写盘串起来，每次句对变化
    （原文一次、译文一次）都要等整份文件原子重写完，行数越多越卡。
    必须怎么改：ingest 只在串行队列上合并内存状态并登记一次延迟写，
    立即返回；真正的落盘由合并窗口到点或 flush() 做。
    """
    body = _swift_function_body(STORE.read_text(encoding="utf-8"), "func ingest(")
    assert not re.search(r"\.sync\s*\{", body), (
        "违反 HC-P02：ingest 里出现了 `.sync {`。ingest 由主界面调用，"
        "同步等磁盘写会把整份文件的重写时间算到主线程上。"
        "必须怎么改：在串行队列上 async 合并状态，写盘交给延迟任务。"
    )
    assert not re.search(r"\bwrite\s*\(", body), (
        "违反 HC-P02：ingest 直接调用了写盘。每句变化都原子重写整日文件，"
        "一小时视频会上千行、越写越慢。"
        "必须怎么改：ingest 只登记脏日期，等合并窗口（1 秒）到点写一次。"
    )


def test_hc_p02_store_has_a_coalescing_window_and_flush():
    """硬约束 HC-P02：必须有合并窗口和 flush 入口。

    违反的表现：找不到 flush() 或约 1 秒的合并窗口常量。

    为什么不行：没有合并窗口就回到「每句一次整文件重写」；没有 flush，
    进后台/退出时最后几句会丢（合并写只推迟落盘，不是丢弃）。
    必须怎么改：保留一个约 1 秒的合并窗口，并提供 flush() 立刻写掉待写内容。
    """
    text = STORE.read_text(encoding="utf-8")
    assert re.search(r"func flush\s*\(\)", text), (
        "违反 HC-P02：TranscriptStore 没有 flush()。进后台/退出时必须能强制落盘，"
        "否则合并窗口里的最后几句会丢。"
    )
    window = re.search(r"coalesce\w*\s*:\s*TimeInterval\s*=\s*([0-9.]+)", text)
    assert window is not None, (
        "违反 HC-P02：没有找到合并窗口常量（TimeInterval）。"
        "必须怎么改：写盘延迟约 1 秒，窗口内多次 ingest 只写一次。"
    )
    value = float(window.group(1))
    assert 0.2 <= value <= 2.0, (
        f"违反 HC-P02：合并窗口是 {value} 秒，不在 0.2–2 秒之间。"
        "太短等于没合并，太长会让后台前的最后几句在崩溃时更容易丢。"
    )


def test_hc_p02_background_flushes_the_store():
    """硬约束 HC-P02：进后台必须 flush 存档。

    违反的表现：AppModel 没有在进后台时调 flush，或 RootView 没把前后台变化接过去。

    为什么不行：iOS 在后台随时可能回收进程，合并窗口里未落盘的句子会静默丢失。
    必须怎么改：RootView 监听 `model.appState.isInBackground` 变化，交给
    `AppModel.backgroundChanged`，后者在进后台时调 `store.flush()`。
    """
    model = MODEL.read_text(encoding="utf-8")
    body = _swift_function_body(model, "func backgroundChanged(")
    assert "store.flush()" in body, (
        "违反 HC-P02：AppModel.backgroundChanged 没有调用 store.flush()。"
        "进后台必须把待写内容写掉，不能靠合并窗口自己到点。"
    )
    root = ROOT_VIEW.read_text(encoding="utf-8")
    assert re.search(r"onChange\(of:\s*model\.appState\.isInBackground", root), (
        "违反 HC-P02：前后台变化没有接到 AppModel。"
        "必须怎么改：RootView 用 model.appState.isInBackground 的变化调 backgroundChanged。"
    )
