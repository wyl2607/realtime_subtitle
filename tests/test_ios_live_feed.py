"""硬约束 HC-P01：iOS 主 App 只允许有一个运行态轮询源（LiveFeed）。

为什么要盯住这件事：状态行、最近句对、草稿都放在共享 Keychain 里，广播扩展
每写一次就发一条 Darwin 通知。主界面原来有四个 0.5 秒定时器（PiP 两处、
StatusMonitor、LinesMonitor），各读各的：约 14 次 SecItem 读取/秒，内容没变
也会重新解 JSON、重新发布，SwiftUI 跟着整树刷新。这类开销读代码看不出来
（每个定时器单独看都"有理由"），只有把所有读取点放在一起才暴露。

所以判据是静态的、按类别拦：`ios/RSiOS/*.swift` 里除 LiveFeed.swift 外，
任何地方都不许直接读运行态 Keychain（状态 / 最近句对 / 草稿）；主 App 里
也只剩 LiveFeed 一个 `Timer.scheduledTimer` 轮询源。失败时不要加豁免名单，
把读取搬到 LiveFeed 里。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RSIOS = ROOT / "ios" / "RSiOS"
LIVE_FEED = RSIOS / "LiveFeed.swift"

# 运行态的三份 Keychain 值：扩展写、主 App 读。读它们的地方必须收敛到 LiveFeed。
_RUNTIME_READ = re.compile(
    r"SharedKeychain\.read(ExtStatus|LatestLines|LatestDraft)\s*\("
)
_SCHEDULED_TIMER = re.compile(r"Timer\.scheduledTimer\s*\(")


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def test_hc_p01_live_feed_is_the_only_runtime_keychain_reader():
    """硬约束 HC-P01：运行态 Keychain 只允许 LiveFeed 读。

    违反的表现：某个视图/管理器绕过 LiveFeed 直接读 Keychain。

    为什么不行：每个读取点都会带上自己的定时器/解 JSON/发布路径，四个 0.5 秒
    轮询叠起来就是每秒十几次 SecItem 读取；内容没变也会让 SwiftUI 重绘。
    必须怎么改：把读取搬进 `ios/RSiOS/LiveFeed.swift`，其它文件只读
    `LiveFeed.shared` 上的属性。
    """
    assert LIVE_FEED.is_file(), (
        "违反 HC-P01：缺少共享运行态来源 ios/RSiOS/LiveFeed.swift。"
        "StatusMonitor / LinesMonitor / PiPManager 必须共用一个轮询源，"
        "不能各自读 Keychain。"
    )

    offenders = []
    for swift in sorted(RSIOS.glob("*.swift")):
        if swift == LIVE_FEED:
            continue
        text = swift.read_text(encoding="utf-8")
        for match in _RUNTIME_READ.finditer(text):
            offenders.append(f"{swift.name}:{_line_of(text, match.start())}: {match.group(0)}")
    assert not offenders, (
        "违反 HC-P01：这些位置绕过 LiveFeed 直接读运行态 Keychain：\n  "
        + "\n  ".join(offenders)
        + "\n为什么不行：每处都会自带定时器/解码/发布路径，内容没变也会重复刷新。"
        "\n必须怎么改：改读 LiveFeed.shared 的 status / lines / draft，"
        "读取只留在 LiveFeed.refresh() 里。"
    )


def test_hc_p01_live_feed_has_the_single_poll_timer():
    """硬约束 HC-P01：LiveFeed 必须正好持有一个运行态轮询定时器。

    违反的表现：定时器数量不是 1，或三份运行态没有全部在这里读。

    为什么不行：定时器一旦散落在多个类型里，就会再次出现「多个 0.5 秒轮询各读
    一次 Keychain」的老问题；数量对不上说明合并没做完或又长回了旧结构。
    必须怎么改：只在 LiveFeed 里建一个 0.5 秒定时器（Darwin 通知可额外触发
    立即刷新），其它类型不得再建轮询定时器。
    """
    text = LIVE_FEED.read_text(encoding="utf-8")
    timers = _SCHEDULED_TIMER.findall(text)
    assert len(timers) == 1, (
        f"违反 HC-P01：LiveFeed.swift 里发现 {len(timers)} 个 Timer.scheduledTimer，"
        "运行态轮询源必须只有一个。"
    )
    for reader in ("readExtStatus", "readLatestLines", "readLatestDraft"):
        assert reader in text, (
            f"违反 HC-P01：LiveFeed 没有读 {reader}。三份运行态（状态/句对/草稿）"
            "都要在这一个来源里读一次。"
        )


def test_hc_p01_old_per_view_monitors_are_gone():
    """硬约束 HC-P01：StatusMonitor / LinesMonitor 不能继续各自轮询。

    违反的表现：旧的每视图轮询类型或引用还在。

    为什么不行：这两个类原本各带一个 0.5 秒定时器，是四个轮询源里的两个；
    留着它们就等于合并没发生。
    必须怎么改：删掉这两个类，界面直接观察 LiveFeed.shared。
    """
    offenders = []
    for swift in sorted(RSIOS.glob("*.swift")):
        text = swift.read_text(encoding="utf-8")
        for name in ("final class StatusMonitor", "final class LinesMonitor"):
            if name in text:
                offenders.append(f"{swift.name}: {name}")
        for ref in ("statusMonitor", "linesMonitor"):
            if re.search(rf"\b{ref}\b", text):
                offenders.append(f"{swift.name}: 引用 {ref}")
    assert not offenders, (
        "违反 HC-P01：旧的每视图轮询类型还在：\n  " + "\n  ".join(sorted(set(offenders)))
        + "\n必须怎么改：删掉 StatusMonitor / LinesMonitor，改读 LiveFeed.shared。"
    )
