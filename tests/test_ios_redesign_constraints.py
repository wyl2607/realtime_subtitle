"""iOS 改版（2026-11）的硬约束：只支持 iOS 26/27、Swift 6、Observation、按页拆文件、熄屏可用。

为什么扫源码：这些都是「以后某次顺手改回老写法」才会坏的约束，行为测试拦不住；
扫全部 iOS 源码，任何一处回退都会让 CI 变红并说明必须怎么改。
"""
import plistlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IOS = ROOT / "ios"
SOURCE_DIRS = ["RSiOS", "Engine", "Shared", "RSBroadcast", "RSLiveActivity"]
MAX_LINES = 500


def _swift_files():
    for name in SOURCE_DIRS:
        yield from sorted((IOS / name).rglob("*.swift"))


def _locate(path: Path, text: str, index: int) -> str:
    return f"{path.relative_to(ROOT)}:{text.count(chr(10), 0, index) + 1}"


def test_hc_u01_only_ios_26_and_newer():
    """硬约束 HC-U01：部署目标 iOS 26，源码里不许再有 iOS 26 以下的可用性分支。

    违反的是什么：把部署目标降回去，或写 `#available(iOS 17/18…)` 兼容分支。
    为什么不行：用户要求只兼容 iOS 26/27；老系统分支会把代码重新拆成两套写法。
    必须怎么改：直接用 iOS 26 的 API；只有 iOS 27 新增的 API 才允许 `#available(iOS 27, *)`。
    """
    project = (IOS / "project.yml").read_text(encoding="utf-8")
    assert 'iOS: "26.0"' in project and 'IPHONEOS_DEPLOYMENT_TARGET: "26.0"' in project, (
        "HC-U01 违反：ios/project.yml 的部署目标不是 iOS 26.0。只兼容 iOS 26/27，不许降回去。"
    )
    offenders = []
    for path in _swift_files():
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"[#@]available\(iOS (\d+)", text):
            if int(m.group(1)) < 27:
                offenders.append(_locate(path, text, m.start()))
    assert not offenders, (
        "HC-U01 违反：出现 iOS 26 及以下的可用性分支：" + ", ".join(offenders)
        + "。部署目标已经是 26，直接用对应 API；只有 iOS 27 新 API 才需要 #available(iOS 27, *)。"
    )


def test_hc_u02_swift_6_language_mode():
    """硬约束 HC-U02：Swift 6 语言模式，不许用 minimal 并发检查降级。

    为什么不行：Swift 5 / minimal 模式下数据竞争只是警告，正是之前 rslite/iOS
    一串并发崩溃的来源。必须怎么改：修掉并发错误，而不是关掉检查。
    """
    project = (IOS / "project.yml").read_text(encoding="utf-8")
    assert 'SWIFT_VERSION: "6.0"' in project, "HC-U02 违反：ios/project.yml 的 SWIFT_VERSION 不是 6.0。"
    assert "SWIFT_STRICT_CONCURRENCY" not in project, (
        "HC-U02 违反：ios/project.yml 又设置了 SWIFT_STRICT_CONCURRENCY。Swift 6 下不许降级并发检查。"
    )


def test_hc_u03_observation_not_combine():
    """硬约束 HC-U03：界面状态一律用 Observation（@Observable），不许回到 ObservableObject。

    为什么不行：两套观察机制混用，视图会时而不刷新、时而整树重绘。
    必须怎么改：类上标 @Observable，视图用 @Environment / @Bindable / @State。
    """
    offenders = []
    for path in _swift_files():
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"\bObservableObject\b|@Published\b|@StateObject\b|@ObservedObject\b", text):
            offenders.append(_locate(path, text, m.start()))
    assert not offenders, (
        "HC-U03 违反：出现 ObservableObject / @Published / @StateObject / @ObservedObject："
        + ", ".join(offenders) + "。改用 @Observable 和 @Environment/@Bindable/@State。"
    )


def test_hc_u04_no_god_files():
    """硬约束 HC-U04：主 App 每个 Swift 文件不超过 500 行，四个页面各自一个文件。

    为什么不行：原来的 ContentView 涨到 1366 行，页面、设置、画中画、资源准备全挤在
    一个视图里，改一处牵连全部。必须怎么改：按页面/职责拆文件，不许再长出巨型视图。
    """
    pages = IOS / "RSiOS" / "Pages"
    for name in ["RootView", "LivePage", "HistoryPage", "SettingsPage", "DiagnosticsPage"]:
        assert (pages / f"{name}.swift").is_file(), f"HC-U04 违反：缺少页面文件 ios/RSiOS/Pages/{name}.swift。"
    assert not (IOS / "RSiOS" / "ContentView.swift").exists(), (
        "HC-U04 违反：ContentView.swift 又出现了。页面放进 ios/RSiOS/Pages/，不要再建总视图。"
    )
    too_long = []
    for path in sorted((IOS / "RSiOS").rglob("*.swift")):
        lines = path.read_text(encoding="utf-8").count("\n") + 1
        if lines > MAX_LINES:
            too_long.append(f"{path.relative_to(ROOT)}（{lines} 行）")
    assert not too_long, (
        f"HC-U04 违反：这些文件超过 {MAX_LINES} 行：" + ", ".join(too_long) + "。按职责拆成多个文件。"
    )


def test_hc_u05_audio_tap_closures_are_not_main_actor_isolated():
    """硬约束 HC-U05：麦克风 tap 闭包只能在 nonisolated 静态函数里创建。

    为什么不行：写在 @MainActor 方法里的闭包会被推断成主线程隔离，Swift 6 的运行期
    隔离检查会在 Core Audio 实时线程上直接崩溃（编译能过，真机一开麦克风就崩）。
    必须怎么改：把 installTap 放进 `nonisolated static func`，只捕获 Sendable 的东西。
    """
    offenders = []
    for path in sorted((IOS / "RSiOS").rglob("*.swift")):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"\.installTap\(", text):
            before = text[:m.start()]
            func = before.rfind("func ")
            header = before[max(0, func - 80):func + 5]
            if "nonisolated static" not in header:
                offenders.append(_locate(path, text, m.start()))
    assert not offenders, (
        "HC-U05 违反：installTap 不在 nonisolated static func 里：" + ", ".join(offenders)
        + "。主线程隔离的闭包在音频线程上会被 Swift 6 运行期检查直接崩溃。"
    )


def test_hc_u06_keeps_working_with_screen_off():
    """硬约束 HC-U06：熄屏后继续工作，并在锁屏显示字幕。

    为什么不行：去掉后台音频模式，锁屏后识别和存档会停；去掉实时活动，熄屏时看不到字幕。
    必须怎么改：主 App 保留 UIBackgroundModes=audio 和 NSSupportsLiveActivities，
    锁屏小组件扩展 RSLiveActivity 必须存在并嵌进主 App。
    """
    info = plistlib.loads((IOS / "RSiOS" / "Info.plist").read_bytes())
    assert "audio" in info.get("UIBackgroundModes", []), (
        "HC-U06 违反：主 App 的 UIBackgroundModes 没有 audio，熄屏后识别和存档会停。"
    )
    assert info.get("NSSupportsLiveActivities") is True, (
        "HC-U06 违反：主 App 没有 NSSupportsLiveActivities，熄屏后锁屏上没有字幕。"
    )
    assert (IOS / "RSLiveActivity" / "SubtitleLiveActivity.swift").is_file(), (
        "HC-U06 违反：缺少锁屏实时活动扩展 ios/RSLiveActivity/SubtitleLiveActivity.swift。"
    )
    project = (IOS / "project.yml").read_text(encoding="utf-8")
    assert re.search(r"- target: RSLiveActivity\s+embed: true", project), (
        "HC-U06 违反：RSLiveActivity 没有作为依赖嵌进主 App。"
    )


def test_hc_u07_keychain_is_readable_while_locked():
    """硬约束 HC-U07：共享 Keychain 条目必须锁屏可读写（AfterFirstUnlock），启动时迁移老条目。

    违反的是什么：写 Keychain 不设 kSecAttrAccessible（默认 WhenUnlocked），或改回 WhenUnlocked。
    为什么不行：扩展/引擎和主 App 之间的状态、句对全靠 Keychain 传。2026-11-21 真机：
    锁屏后每次读写都返回 -25308，主 App 读到空状态，把它当成广播抢占，3 秒后自己停了麦克风；
    画中画和锁屏实时活动也拿不到新句子。
    必须怎么改：SharedKeychain.write 的更新和新增都带 kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly，
    AppModel.bootstrap 调 SharedKeychain.migrateAccessibility()。
    """
    keychain = (IOS / "Shared" / "SharedKeychain.swift").read_text(encoding="utf-8")
    assert "kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly" in keychain, (
        "HC-U07 违反：SharedKeychain 没有用 AfterFirstUnlockThisDeviceOnly，锁屏后读写会失败（-25308）。"
    )
    write_body = keychain[keychain.index("func write("):keychain.index("func migrateAccessibility")]
    assert "kSecAttrAccessible" in write_body, (
        "HC-U07 违反：SharedKeychain.write 没有带 kSecAttrAccessible，新条目会落成默认的 WhenUnlocked。"
    )
    offenders = []
    for path in _swift_files():
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"kSecAttrAccessibleWhenUnlocked\w*", text):
            offenders.append(_locate(path, text, m.start()))
    assert not offenders, "HC-U07 违反：出现 WhenUnlocked 可访问性：" + ", ".join(offenders)
    model = (IOS / "RSiOS" / "App" / "AppModel.swift").read_text(encoding="utf-8")
    assert "SharedKeychain.migrateAccessibility()" in model, (
        "HC-U07 违反：AppModel 没有在启动时迁移老条目的可访问性。"
    )


def test_hc_u09_background_work_not_driven_by_view_on_change():
    """硬约束 HC-U09：存档和锁屏实时活动不许挂在视图的 onChange 上。

    为什么不行：熄屏/后台时 SwiftUI 不重新求值视图，`.onChange(of: model.feed.lines)` 不触发，
    存档停写、锁屏字幕停更——而这恰恰是熄屏场景唯一要它们干活的时候。
    必须怎么改：AppModel 订阅 LiveFeed.didChangeNotification，在回调里存档、刷新实时活动。
    """
    root = (IOS / "RSiOS" / "Pages" / "RootView.swift").read_text(encoding="utf-8")
    for pattern in [r"onChange\(of:\s*model\.feed\.lines", r"onChange\(of:\s*model\.feed\.status"]:
        assert not re.search(pattern, root), (
            f"HC-U09 违反：RootView 又用 {pattern} 驱动后台工作。后台时它不触发，改由 AppModel 订阅 LiveFeed 通知。"
        )
    model = (IOS / "RSiOS" / "App" / "AppModel.swift").read_text(encoding="utf-8")
    assert "LiveFeed.didChangeNotification" in model, (
        "HC-U09 违反：AppModel 没有订阅 LiveFeed.didChangeNotification，后台时存档和锁屏字幕会停。"
    )


def test_hc_u10_pip_frames_are_iosurface_backed():
    """硬约束 HC-U10：画中画的像素缓冲必须由 IOSurface 支撑。

    为什么不行：画中画窗口在系统进程里绘制，帧要跨进程共享；普通内存的 CVPixelBuffer
    共享失败（PixelBufferSharing err=-15596），App 内预览正常、画中画里却一帧字幕都没有。
    必须怎么改：CVPixelBufferCreate 的属性里带 kCVPixelBufferIOSurfacePropertiesKey。
    """
    offenders = []
    for path in sorted((IOS / "RSiOS").rglob("*.swift")):
        text = path.read_text(encoding="utf-8")
        if "CVPixelBufferCreate(" in text and "kCVPixelBufferIOSurfacePropertiesKey" not in text:
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, (
        "HC-U10 违反：这些文件创建像素缓冲却没有 IOSurface 支撑：" + ", ".join(offenders)
        + "。画中画是系统进程绘制的，没有 IOSurface 帧传不过去。"
    )


def test_hc_u11_captions_scroll_with_stable_identity():
    """硬约束 HC-U11：字幕页的字幕要自动往上滚，句子按身份（会话+id）做 ForEach 的 id。

    违反的是什么：用 `id: \\.offset` 遍历句子，或字幕页退回只显示最近几句的卡片。
    为什么不行：按位置做 id，新句进来时每一格原地换字，看起来字幕不会滚
    （2026-11-21 用户真机反馈「字幕不会自动向上滚动」）；引擎只给最近 3 句，不累积就没有可滚的内容。
    必须怎么改：ForEach 用 `\\.key`；字幕页用 LiveCaptionsView 显示 AppModel.captionLines。
    """
    offenders = []
    for path in sorted((IOS / "RSiOS").rglob("*.swift")):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"id:\s*\\\.offset", text):
            offenders.append(_locate(path, text, m.start()))
    assert not offenders, (
        "HC-U11 违反：按位置做 ForEach 的 id：" + ", ".join(offenders) + "。句子用 \\.key（会话+id）。"
    )
    feed = (IOS / "RSiOS" / "LiveFeed.swift").read_text(encoding="utf-8")
    assert not re.search(r"struct RecentLine:[^{]*Identifiable", feed), (
        "HC-U11 违反：RecentLine 又遵守了 Identifiable。它的 id 只在单个会话内唯一，"
        "列表按它做 id 会让不同会话的同号句子互相顶替；ForEach 显式写 id: \\.key。"
    )
    live = (IOS / "RSiOS" / "Pages" / "LivePage.swift").read_text(encoding="utf-8")
    assert "LiveCaptionsView(" in live and "model.captionLines" in live, (
        "HC-U11 违反：字幕页没有用 LiveCaptionsView 显示累积的 captionLines，字幕不会往上滚。"
    )


def test_hc_u12_no_orphan_live_activities():
    """硬约束 HC-U12：启动时接管/结束上次留下的锁屏实时活动；启动时同步一次。

    为什么不行：进程被系统杀掉时实时活动不会自己结束，锁屏上冻结着最后一句好几个小时；
    下次启动再开一个，锁屏上就是两条。
    必须怎么改：LiveActivityController 初始化时读 Activity<SubtitleActivityAttributes>.activities，
    接管第一个、结束其余；AppModel.bootstrap 调 refreshLiveActivity()（没在采集就结束它）。
    """
    controller = (IOS / "RSiOS" / "App" / "LiveActivityController.swift").read_text(encoding="utf-8")
    assert "Activity<SubtitleActivityAttributes>.activities" in controller, (
        "HC-U12 违反：LiveActivityController 没有接管上次留下的实时活动，会冻结在锁屏或重复出现。"
    )
    model = (IOS / "RSiOS" / "App" / "AppModel.swift").read_text(encoding="utf-8")
    bootstrap = model[model.index("func bootstrap()"):model.index("private func feedChanged()")]
    assert "refreshLiveActivity()" in bootstrap, (
        "HC-U12 违反：bootstrap 没有同步一次实时活动，上次留下的活动不会被结束。"
    )


def test_hc_u13_auto_mode_starts_local_first():
    """硬约束 HC-U13：自动档连节点的同时先用本机出字。

    为什么不行：以前要等节点握手失败（基础 15 秒、冷启动最多 60 秒）才启本机，
    用户开麦克风后长时间没有字幕。节点 ready 后由 handleNodeReady 等本句定稿再切回。
    必须怎么改：SubtitleEngine.startAutoMode 在 startPinnedSession 之后调用
    AutoFallback.localFirstDecision() 并执行决策；行为用例在 AutoFallbackSelftest（HC-U13）。
    """
    engine = (IOS / "Engine" / "SubtitleEngine.swift").read_text(encoding="utf-8")
    start = engine[engine.index("private func startAutoMode"):engine.index("private func handleAutoEvent")]
    assert "localFirstDecision()" in start and start.index("startPinnedSession(") < start.index("localFirstDecision()"), (
        "HC-U13 违反：startAutoMode 没有在连节点之后立刻走 localFirstDecision，自动档又要干等节点。"
    )
    selftest = (IOS / "Tests" / "AutoFallbackSelftest.swift").read_text(encoding="utf-8")
    assert selftest.count("HC-U13") >= 5, "HC-U13 违反：AutoFallbackSelftest 里的 local-first 行为用例被删了。"


def test_hc_u14_engine_invariants_from_free_model_audit():
    """硬约束 HC-U14：引擎的几条不变式（2026-11-22 多模型审计后核实的 bug）。

    1. 翻译会话不是 Sendable，只能串行使用：LocalEngine.translate（云端句）也必须挂到 translationTail，
       不许每句一个独立 Task 并发打同一个会话。
    2. 等翻译会话准备好必须有上限（preparedSession），否则 prepareTranslation 卡住时每一句都永远
       不收尾，自动档再也切不回节点。
    3. 分析器输入队列必须有界（bufferingNewest），掉队时内存不能一直涨。
    4. 停本机引擎和切档位必须在同一个临界区（stopLocalEngine(switchingTo:)），否则中间几帧音频被丢。
    """
    local = (IOS / "Engine" / "LocalEngine.swift").read_text(encoding="utf-8")
    translate = local[local.index("func translate(_ text: String, id: Int)"):local.index("func stop()")]
    assert "translationTail = newTail" in translate, (
        "HC-U14 违反：LocalEngine.translate 没有挂到串行尾链，同一个 TranslationSession 会被并发调用。"
    )
    settle = local[local.index("private func translateAndSettle"):]
    assert "await preparedSession(ready)" in settle and "ready?.value" not in settle.split("private func preparedSession")[0], (
        "HC-U14 违反：translateAndSettle 又直接无上限地等 translationReady。"
    )
    assert re.search(r"AsyncStream<AnalyzerInput>\(bufferingPolicy:\s*\.bufferingNewest", local), (
        "HC-U14 违反：分析器输入 AsyncStream 没有设有界缓冲。"
    )
    engine = (IOS / "Engine" / "SubtitleEngine.swift").read_text(encoding="utf-8")
    for m in re.finditer(r"stopLocalEngine\(\)\s*\n\s*stateQueue\.sync\s*\{[^}]*routingTier\s*=", engine):
        raise AssertionError(
            "HC-U14 违反：" + _locate(IOS / "Engine" / "SubtitleEngine.swift", engine, m.start())
            + " 先停本机引擎、再另开临界区改 routingTier；用 stopLocalEngine(switchingTo:)。"
        )


def test_hc_u15_node_session_never_uses_system_proxy():
    """硬约束 HC-U15：节点 WebSocket 绝不走系统代理；云端客户端保留系统代理。

    为什么不行：URLSessionConfiguration.ephemeral 仍继承系统 HTTP/SOCKS/PAC 代理（codex 审计 2026-11-22），
    代理能看到 Bearer token 和原始音频，也能把连接带出 tailnet——违反 CLAUDE.md 第 4 节第 47 条。
    必须怎么改：NodeSession 建连前 `configuration.connectionProxyDictionary = [:]`；CloudClient 不许这么设。
    """
    node = (IOS / "Shared" / "NodeProtocol.swift").read_text(encoding="utf-8")
    assert "connectionProxyDictionary = [:]" in node, (
        "HC-U15 违反：NodeSession 的 URLSession 没有禁用系统代理，token 和音频会经过代理。"
    )
    cloud = (IOS / "Shared" / "CloudClient.swift").read_text(encoding="utf-8")
    assert "connectionProxyDictionary" not in cloud, (
        "HC-U15 违反：CloudClient 改了代理设置。Groq 在公网，有的用户只能经系统代理访问，不许关。"
    )
    engine = (IOS / "Engine" / "SubtitleEngine.swift").read_text(encoding="utf-8")
    body = engine[engine.index("private func writeLatestLines()"):engine.index("private func postDarwinNotification()")]
    assert "linesWriteQueue.sync" in body and "stateQueue.sync" in body and "encode(recentLines)" in body, (
        "HC-U15 违反：writeLatestLines 又在锁外读 recentLines（数据竞争，旧快照可能覆盖新字幕）。"
    )


def test_hc_u16_bounded_copies_and_watchdog_recheck():
    """硬约束 HC-U16：PCM 拷贝按帧数算字节；节点就绪看门狗到点前在锁内重新核对。

    为什么不行（2026-11-22 剩余审计核实）：
    - LocalEngine.append 按 Data 字节数 memcpy，奇数长度时比 frameCount 帧的缓冲多写 1 字节。
    - readyWatchdogFired 解锁算剩余时间后直接清 readyBox 并关 socket，期间 ready 刚到也会被当超时掐断。
    必须怎么改：memcpy 长度用 frameCount * 2；关 socket 前 `guard readyBox === box`。
    """
    local = (IOS / "Engine" / "LocalEngine.swift").read_text(encoding="utf-8")
    assert "memcpy(dst, base, pcm16k.count)" not in local, (
        "HC-U16 违反：LocalEngine.append 按字节数拷 PCM，奇数长度会越界写。"
    )
    node = (IOS / "Shared" / "NodeProtocol.swift").read_text(encoding="utf-8")
    body = node[node.index("private func readyWatchdogFired()"):node.index("private func cancelReadyWatch()")]
    assert "readyBox === box" in body, (
        "HC-U16 违反：就绪看门狗关连接前没在锁内核对 readyBox，会掐断刚就绪的会话。"
    )


def test_hc_u17_cloud_upload_falls_back_to_cellular_only_safely():
    """硬约束 HC-U17：云端上传 Wi-Fi 失败换蜂窝重发，但不许绕过系统代理、不许漏记账。

    为什么（2026-11-22 用户：5G 很差但 Wi-Fi 还好，要两条网互为备份）：URLSession 选不了网卡，
    换蜂窝只能走 NWConnection 直连——直连会绕过系统代理（CLAUDE.md 第 4 节第 47 条），
    重发又是 Groq 的另一次计费请求，本地账本必须照记（第 49 条：配额只能本地记）。
    必须怎么做：重发只经 CellularFallback.shouldRetry 判定（含 systemProxyConfigured）；
    引擎传 beforeCellularRetry 并在里面 recordRequest；蜂窝直连必须 requiredInterfaceType = .cellular。
    """
    cloud = (IOS / "Shared" / "CloudClient.swift").read_text(encoding="utf-8")
    assert "CellularFallback.shouldRetry(" in cloud and "systemProxyConfigured: CellularFallback.systemProxyConfigured()" in cloud, (
        "HC-U17 违反：CloudClient 换蜂窝重发没走 CellularFallback.shouldRetry（会绕过系统代理）。"
    )
    assert "beforeCellularRetry?()" in cloud, "HC-U17 违反：重发前没通知调用方记账。"
    assert "_ = NetworkPaths.shared" in cloud, (
        "HC-U17 违反：CloudClient 初始化时没有预热 NetworkPaths，首次失败时路径快照还是 false/false，蜂窝重发不会触发。"
    )
    fallback = (IOS / "Shared" / "NetworkPaths.swift").read_text(encoding="utf-8")
    assert "!systemProxyConfigured" in fallback, "HC-U17 违反：开了系统代理仍会直连蜂窝。"
    cellular = (IOS / "Shared" / "CellularHTTP.swift").read_text(encoding="utf-8")
    assert "requiredInterfaceType = .cellular" in cellular, "HC-U17 违反：蜂窝直连没钉蜂窝网卡。"
    engine = (IOS / "Engine" / "SubtitleEngine.swift").read_text(encoding="utf-8")
    m = re.search(r"beforeCellularRetry:\s*\{(.*?)\n\s*\}\n", engine, re.S)
    assert m and "recordRequest" in m.group(1), "HC-U17 违反：换蜂窝重发没记进额度账本。"


def test_hc_u17_retry_followups_generation_and_single_waiter():
    """HC-U17 / HC-U14 跟进：重发绑同一取消代次；翻译准备只留一个等待任务。"""
    cloud = (IOS / "Shared" / "CloudClient.swift").read_text(encoding="utf-8")
    assert "perform(cellularLaunch(request), generation: startGeneration)" in cloud and \
        "guard lockValue({ cancelGeneration }) == startGeneration" in cloud, (
        "违反：蜂窝重发没有绑定首次请求的取消代次，首次失败后用户关云端/换 key，重发仍会把音频发出去。"
    )
    local = (IOS / "Engine" / "LocalEngine.swift").read_text(encoding="utf-8")
    assert "let waiter = Task { race.settle" not in local and "PreparedBroadcast" in local, (
        "违反：preparedSession 又为每一句新开 await ready.value 的 Task，prepareTranslation 卡住时会无界挂起。"
    )


def _skip_comment(text: str, idx: int) -> int | None:
    """若 idx 处是 Swift 行/块注释起点，返回注释结束后的下标；否则返回 None。"""
    if text.startswith("//", idx):
        nl = text.find("\n", idx + 2)
        return len(text) if nl == -1 else nl + 1
    if text.startswith("/*", idx):
        depth = 1
        i = idx + 2
        n = len(text)
        while i < n and depth > 0:
            if text.startswith("/*", i):
                depth += 1
                i += 2
            elif text.startswith("*/", i):
                depth -= 1
                i += 2
            else:
                i += 1
        return i
    return None


def _skip_string_literal(text: str, idx: int) -> int:
    """跳过 Swift 字符串字面量（含转义、多行字符串与 \\(...) 插值），返回结束后的下标。"""
    n = len(text)
    if text.startswith('"""', idx):
        delim = '"""'
        i = idx + 3
    else:
        delim = '"'
        i = idx + 1
    while i < n:
        if text[i] == "\\":
            if i + 1 < n and text[i + 1] == "(":
                i = _skip_balanced(text, i + 1, "(", ")")
            else:
                i += 2
            continue
        if text.startswith(delim, i):
            return i + len(delim)
        i += 1
    return n


def _skip_balanced(text: str, open_idx: int, open_ch: str, close_ch: str) -> int:
    """从 open_idx 处的开括号起做配对扫描（忽略字符串与注释里的括号），返回闭括号后下标。"""
    n = len(text)
    assert text[open_idx] == open_ch, f"期望 {open_ch!r}，实际为 {text[open_idx]!r}"
    depth = 1
    i = open_idx + 1
    while i < n and depth > 0:
        comment_end = _skip_comment(text, i)
        if comment_end is not None:
            i = comment_end
            continue
        if text[i] == '"':
            i = _skip_string_literal(text, i)
            continue
        if text[i] == open_ch:
            depth += 1
        elif text[i] == close_ch:
            depth -= 1
        i += 1
    assert depth == 0, f"括号未闭合：{open_ch}{close_ch}（起点下标 {open_idx}）"
    return i


def _blank_swift_comments(text: str) -> str:
    """将 Swift 注释替换为空格（保留换行与字符串字面量），保持所有字符下标与行号不变。"""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        comment_end = _skip_comment(text, i)
        if comment_end is not None:
            out.append("".join("\n" if ch == "\n" else " " for ch in text[i:comment_end]))
            i = comment_end
            continue
        if text[i] == '"':
            str_end = _skip_string_literal(text, i)
            out.append(text[i:str_end])
            i = str_end
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _extract_braced_block(text: str, header_pattern: str, *, start: int = 0) -> tuple[str, int, int]:
    """按 header_pattern（末尾含 `{`）提取括号配对块，返回 `(块文本, 左括号下标, 闭括号后下标)`。"""
    m = re.search(header_pattern, text[start:])
    assert m is not None, f"HC-U19 违反：未找到代码块 {header_pattern!r}。"
    open_idx = start + m.end() - 1
    end_idx = _skip_balanced(text, open_idx, "{", "}")
    return text[open_idx:end_idx], open_idx, end_idx


def _extract_func_bodies(text: str, name: str) -> list[str]:
    """提取所有同名 Swift 函数体（支持多行/嵌套括号参数列表），按括号配对截断。"""
    bodies: list[str] = []
    for m in re.finditer(rf"\bfunc\s+{re.escape(name)}\s*\(", text):
        param_end = _skip_balanced(text, m.end() - 1, "(", ")")
        brace_match = re.match(r"[^{;]*?\{", text[param_end:])
        assert brace_match is not None, f"HC-U19 违反：函数 {name} 缺少函数体。"
        open_idx = param_end + brace_match.end() - 1
        end_idx = _skip_balanced(text, open_idx, "{", "}")
        bodies.append(text[open_idx:end_idx])
    return bodies


def _extract_button_action_label_spans(text: str) -> list[tuple[str, int, int]]:
    """提取所有 `Button { action } label: { label }` 的 `(action文本, label起点, label终点)`。"""
    spans: list[tuple[str, int, int]] = []
    for m in re.finditer(r"\bButton\s*\{", text):
        action_open = m.end() - 1
        action_end = _skip_balanced(text, action_open, "{", "}")
        label_match = re.match(r"\s*label:\s*\{", text[action_end:])
        if label_match is None:
            continue
        label_open = action_end + label_match.end() - 1
        label_end = _skip_balanced(text, label_open, "{", "}")
        spans.append((text[action_open:action_end], label_open, label_end))
    return spans


def test_hc_u19_transcript_untranslated_retry():
    """硬约束 HC-U19：回看页「未翻译 ⟳」可点按重新翻译，只用已安装本机资源且有超时，新文案只来自 DesignTokens。

    为什么不行：如果占位只是不可点的 Text，用户没法补翻漏掉的句子；如果用 Configuration/.translationTask，
    点按会弹出系统下载框或触网；如果不加超时，Translation 框架卡住会把该句永久锁在加载中；如果视图里写死文案，
    会脱离 design/tokens.json 三端令牌同步。
    """
    view = (IOS / "RSiOS" / "TranscriptView.swift").read_text(encoding="utf-8")
    view_code = _blank_swift_comments(view)

    # 1. 回看页未翻译分支必须在 Button action 中调用 retryTranslation，且无障碍标签与文案令牌配对。
    card_bodies = _extract_func_bodies(view_code, "card")
    assert len(card_bodies) == 1, "HC-U19 违反：TranscriptView 必须存在唯一的 card 函数。"
    card_body = card_bodies[0]
    needs_tx_block, _, _ = _extract_braced_block(card_body, r"\bif\s+needsTranslation\s*\{")
    assert "untranslatedButton(" in needs_tx_block and "Text(DesignTokens.Text.untranslated)" not in needs_tx_block, (
        "HC-U19 违反：TranscriptView.card 的未翻译分支必须调用 untranslatedButton，不得退回裸 Text。"
    )

    untranslated_bodies = _extract_func_bodies(view_code, "untranslatedButton")
    assert len(untranslated_bodies) == 1, "HC-U19 违反：TranscriptView 必须存在唯一的 untranslatedButton 函数。"
    untranslated_body = untranslated_bodies[0]
    untranslated_buttons = _extract_button_action_label_spans(untranslated_body)
    assert any(
        "retryTranslation(" in action
        and "DesignTokens.Text.untranslated" in untranslated_body[l_start:l_end]
        and "DesignTokens.Text.translating" in untranslated_body[l_start:l_end]
        for action, l_start, l_end in untranslated_buttons
    ), (
        "HC-U19 违反：untranslatedButton 的 Button action 必须调用 retryTranslation，"
        "且同一个 Button 的 label 必须使用 DesignTokens.Text.untranslated 与 DesignTokens.Text.translating。"
    )
    assert ".accessibilityLabel(DesignTokens.Text.retryTranslation)" in untranslated_body, (
        "HC-U19 违反：untranslatedButton 缺少 DesignTokens.Text.retryTranslation 无障碍标签。"
    )

    # 确认 TranscriptView 中所有 DesignTokens.Text.untranslated 都位于调用 retryTranslation 的 Button label 区间内。
    all_button_spans = _extract_button_action_label_spans(view_code)
    for m in re.finditer(r"\bDesignTokens\.Text\.untranslated\b", view_code):
        in_retry_button_label = any(
            l_start <= m.start() < l_end and "retryTranslation(" in action
            for action, l_start, l_end in all_button_spans
        )
        assert in_retry_button_label, (
            f"HC-U19 违反：{_locate(IOS / 'RSiOS' / 'TranscriptView.swift', view, m.start())} "
            "出现不在调用 retryTranslation 的 Button label 里的 DesignTokens.Text.untranslated。"
        )

    # contextMenu 区间内也必须有调用 retryTranslation 的「重新翻译」按钮。
    context_menu, _, _ = _extract_braced_block(card_body, r"\.contextMenu\s*\{")
    ctx_retry_action, _, _ = _extract_braced_block(
        context_menu, r"\bButton\s*\(\s*DesignTokens\.Text\.retryTranslation\s*\)\s*\{"
    )
    assert "retryTranslation(" in ctx_retry_action, (
        "HC-U19 违反：TranscriptView 的 contextMenu 中重新翻译按钮未调用 retryTranslation。"
    )

    # 重译入口（retryTranslation）必须在成功翻译后写回对应存档行并刷新列表，且不触网/不弹下载。
    retry_bodies = _extract_func_bodies(view_code, "retryTranslation")
    assert len(retry_bodies) == 1, "HC-U19 违反：TranscriptView 必须存在唯一的 retryTranslation 函数。"
    retry_body = retry_bodies[0]
    assert "RetryTranslationPolicy.canRetry(" in retry_body, (
        "HC-U19 违反：retryTranslation 必须通过 RetryTranslationPolicy.canRetry 校验重试条件。"
    )
    task_body, _, _ = _extract_braced_block(retry_body, r"\bTask\s*\{")
    do_body, _, _ = _extract_braced_block(task_body, r"\bdo\s*\{")
    assert "translator.translate(" in do_body and "store.updateTranslation(" in do_body, (
        "HC-U19 违反：retryTranslation 必须调用 translator.translate 并将结果写回 store.updateTranslation。"
    )
    assert do_body.index("translator.translate(") < do_body.index("store.updateTranslation("), (
        "HC-U19 违反：retryTranslation 必须先翻译成功再调用 store.updateTranslation 写回。"
    )
    saved_body, _, _ = _extract_braced_block(do_body, r"\bif\s+saved\s*\{")
    assert re.search(r"\breload\s*\(", saved_body), (
        "HC-U19 违反：retryTranslation 写回存档成功后必须调用 reload(...) 刷新回看列表。"
    )
    assert not re.search(r"\bprepareTranslation\s*\(", view_code), (
        "HC-U19 违反：TranscriptView 不得调用 prepareTranslation()。"
    )
    assert "TranslationSession.Configuration" not in view_code and ".translationTask" not in view_code, (
        "HC-U19 违反：TranscriptView 不得使用 TranslationSession.Configuration / .translationTask。"
    )

    # 2. RetryTranslator 必须仅走 installed 路径、先查已安装状态、不调 prepareTranslation，且有超时和去重。
    translator = (IOS / "RSiOS" / "RetryTranslator.swift").read_text(encoding="utf-8")
    translator_code = _blank_swift_comments(translator)
    actor_body, _, _ = _extract_braced_block(translator_code, r"\bactor\s+RetryTranslator\s*\{")

    translate_bodies = _extract_func_bodies(actor_body, "translate")
    assert len(translate_bodies) == 1, "HC-U19 违反：RetryTranslator 必须存在唯一的 translate 函数。"
    translate_body = translate_bodies[0]
    assert "inFlight.begin(" in translate_body, (
        "HC-U19 违反：RetryTranslator.translate 必须做同句并发去重，避免重复点按排队。"
    )
    assert "LanguageAvailability" in translate_body and ".installed" in translate_body, (
        "HC-U19 违反：RetryTranslator.translate 必须先检查本机翻译资源是否已安装。"
    )
    assert "translateWithTimeout(" in translate_body, (
        "HC-U19 违反：RetryTranslator.translate 必须调用带超时的 translateWithTimeout。"
    )
    installed_guard = re.search(r"\bguard\s+status\s*==\s*\.installed\s+else\s*\{", translate_body)
    installed_if = re.search(r"\bif\s+status\s*==\s*\.installed\s*\{", translate_body)
    assert installed_guard or installed_if, (
        "HC-U19 违反：RetryTranslator.translate 必须用 guard status == .installed else 或 "
        "if status == .installed 的真实控制流阻止未安装资源翻译。"
    )
    if installed_guard:
        guard_body, _, _ = _extract_braced_block(translate_body, r"\bguard\s+status\s*==\s*\.installed\s+else\s*\{")
        assert "throw" in guard_body and "assetsNotInstalled" in guard_body, (
            "HC-U19 违反：.installed guard 的失败分支必须真正 throw/return，而不能只是读取比较结果。"
        )
    else:
        installed_block, _, _ = _extract_braced_block(translate_body, r"\bif\s+status\s*==\s*\.installed\s*\{")
        assert "translateWithTimeout(" in installed_block, (
            "HC-U19 违反：只有 status == .installed 的 if 分支内才允许执行翻译。"
        )

    timeout_bodies = _extract_func_bodies(actor_body, "translateWithTimeout")
    assert len(timeout_bodies) == 1, "HC-U19 违反：RetryTranslator 必须存在唯一的 translateWithTimeout 函数。"
    timeout_body = timeout_bodies[0]
    assert "TranslationSession(installedSource:" in timeout_body and "session.translate(" in timeout_body, (
        "HC-U19 违反：RetryTranslator.translateWithTimeout 必须使用 TranslationSession(installedSource:target:) 执行翻译。"
    )
    assert "translationTimeout" in timeout_body, (
        "HC-U19 违反：RetryTranslator.translateWithTimeout 必须对单句翻译设置超时。"
    )
    for m in re.finditer(r"\bTranslationSession\s*\(", translator_code):
        assert translator_code[m.end():].lstrip().startswith("installedSource:"), (
            f"HC-U19 违反：{_locate(IOS / 'RSiOS' / 'RetryTranslator.swift', translator, m.start())} "
            "使用了 installedSource 以外的 TranslationSession 初始化。"
        )
    assert not re.search(r"\bprepareTranslation\s*\(", translator_code), (
        "HC-U19 违反：RetryTranslator 不得调用 prepareTranslation()——它会请求下载语言资源，重试只能用已安装资源。"
    )
    assert "TranslationSession.Configuration" not in translator_code and ".translationTask" not in translator_code, (
        "HC-U19 违反：RetryTranslator 不得使用会触发系统下载弹窗的 TranslationSession.Configuration / .translationTask。"
    )

    # 3. TranscriptStore.updateTranslation 必须按行校对并原地更新译文与标脏落盘。
    store = (IOS / "RSiOS" / "TranscriptStore.swift").read_text(encoding="utf-8")
    store_code = _blank_swift_comments(store)
    update_bodies = _extract_func_bodies(store_code, "updateTranslation")
    assert any(
        "rows[line.id].time == line.time" in body
        and "rows[line.id].src == line.src" in body
        and "rows[line.id].dst = cleanDst" in body
        and "scheduleWrite()" in body
        for body in update_bodies
    ), "HC-U19 违反：TranscriptStore.updateTranslation 必须校验行身份后原地更新 dst 并调度写盘。"
    async_update_bodies = [body for body in update_bodies if "withCheckedContinuation" in body]
    assert len(async_update_bodies) == 1 and re.search(
        r"\bupdateTranslation\s*\(\s*for:\s*line\b", async_update_bodies[0]
    ), (
        "HC-U19 违反：TranscriptStore.updateTranslation 的 async 重载必须委托给同步重载，"
        "不能只满足另一个重载的身份校验。"
    )

    policy = (IOS / "RSiOS" / "RetryTranslationPolicy.swift").read_text(encoding="utf-8")
    timeout_match = re.search(r"translationTimeout\s*:\s*TimeInterval\s*=\s*([0-9.]+)", policy)
    assert timeout_match is not None and 5.0 <= float(timeout_match.group(1)) <= 20.0, (
        "HC-U19 违反：RetryTranslationPolicy.translationTimeout 缺失或不在 5-20 秒范围内。"
    )

    # 4. 新文案只来自 DesignTokens（不在视图或翻译器里写死字面量）。
    for path, src in ((IOS / "RSiOS" / "TranscriptView.swift", view), (IOS / "RSiOS" / "RetryTranslator.swift", translator)):
        for literal in ("重新翻译", "本机翻译资源未安装", "翻译超时", "翻译失败", "点按重试", "翻译中"):
            for m in re.finditer(rf'"[^"]*{re.escape(literal)}[^"]*"', src):
                raise AssertionError(
                    f"HC-U19 违反：{_locate(path, src, m.start())} 写死了新文案字面量，必须改用 DesignTokens.Text.*"
                )


def test_hc_b10_retry_translation_not_overwritten_by_feed_reingest():
    """硬约束 HC-B10：最近句对回灌时，空译文不得覆盖已有译文。

    来源 bug：AppModel 在 feed 变化及启动时把最近几句整批重新 ingest，其中未翻译句 dst == ""；
    TranscriptStore.merge 曾对已有行无条件 `dst = dst`，用户点「未翻译 ⟳」重译成功的句子
    下一句一到（或重启）就被冲回「未翻译」。
    必须怎么做：merge 里 `let mergedDst = dst.isEmpty ? row.dst : dst`，dirty 判断和写回都用 mergedDst；
    非空新译文仍以最后一版为准。
    """
    store = (IOS / "RSiOS" / "TranscriptStore.swift").read_text(encoding="utf-8")
    store_code = _blank_swift_comments(store)
    merge_bodies = _extract_func_bodies(store_code, "merge")
    assert len(merge_bodies) == 1, "HC-B10 违反：TranscriptStore 必须存在唯一的 merge 函数。"
    merge_body = merge_bodies[0]
    assert "let mergedDst = dst.isEmpty ? row.dst : dst" in merge_body, (
        "HC-B10 违反：merge 必须用已有非空译文保护空 dst 回灌。"
    )
    assert "row.dst != mergedDst" in merge_body and "dst = mergedDst" in merge_body, (
        "HC-B10 违反：merge 的 dirty 判断和写回都必须使用 mergedDst。"
    )

    selftest = (IOS / "Tests" / "TranscriptStoreSelftest.swift").read_text(encoding="utf-8")
    selftest_code = _blank_swift_comments(selftest)
    assert any(
        "HC-B10" in selftest_code[start:start + 800]
        for start in (m.start() for m in re.finditer(r"\bcheck\s*\(", selftest_code))
    ), (
        "HC-B10 违反：TranscriptStoreSelftest 必须包含带 HC-B10 标记的 check( 回归断言。"
    )


def test_hc_u22_history_multiday_archive():
    """硬约束 HC-U22：回看页导出必须用所选日期，列日期方法必须校验文件名格式且不阻塞主线程。

    为什么不行：如果 ShareLink / 导出继续写死 Date()，切换往日后点导出仍会导出今天（或空文件）；
    如果列日期不校验 `yyyy-MM-dd.txt` 格式或直接在视图里同步扫目录，非法文件会导致崩溃或卡死主线程。
    必须怎么改：TranscriptView 用 selectedDate 驱动 fileURL(for:) 与 ShareLink；
    TranscriptStore.availableDates 在私有队列扫描并校验 yyyy-MM-dd.txt 文件名格式。
    """
    view = (IOS / "RSiOS" / "TranscriptView.swift").read_text(encoding="utf-8")
    # 1. ShareLink / canExport / fileURL(for:) 不得再写死 Date()。
    hardcoded_date_exports = list(re.finditer(r"fileURL\(\s*for:\s*Date\(\)\s*\)", view))
    assert not hardcoded_date_exports, (
        "HC-U22 违反：TranscriptView 里 fileURL(for:) 仍写死 Date()（"
        + ", ".join(_locate(IOS / "RSiOS" / "TranscriptView.swift", view, m.start()) for m in hardcoded_date_exports)
        + "），切换往日存档后导出会出错。必须改为 selectedDate。"
    )
    assert re.search(r"ShareLink\(\s*item:\s*(?:self\.)?store\.fileURL\(\s*for:\s*(?:self\.)?selectedDate\s*\)", view), (
        "HC-U22 违反：TranscriptView 的 ShareLink(item:) 没有使用 store.fileURL(for: selectedDate)。"
    )
    assert "store.availableDates(" in view, (
        "HC-U22 违反：TranscriptView 没有通过 TranscriptStore.availableDates() 获取存档日期。"
    )
    assert "contentsOfDirectory" not in view, (
        "HC-U22 违反：TranscriptView 不得直接扫描目录，必须走 TranscriptStore 队列。"
    )
    for token_name in ("historySelectDate", "historyToday", "historyEmptySelectedDay"):
        assert f"DesignTokens.Text.{token_name}" in view, (
            f"HC-U22 违反：TranscriptView 缺少 DesignTokens.Text.{token_name}。"
        )
    for literal in ("选择日期", "当天没有字幕"):
        for m in re.finditer(rf'"[^"]*{re.escape(literal)}[^"]*"', view):
            raise AssertionError(
                f"HC-U22 违反：{_locate(IOS / 'RSiOS' / 'TranscriptView.swift', view, m.start())} "
                "写死了新文案字面量，必须改用 DesignTokens.Text.*"
            )

    # 2. TranscriptStore 列日期方法必须在私有队列扫描且校验 yyyy-MM-dd.txt 文件名格式。
    store = (IOS / "RSiOS" / "TranscriptStore.swift").read_text(encoding="utf-8")
    assert "func availableDates(" in store, (
        "HC-U22 违反：TranscriptStore 缺少列出存档日期的 availableDates 方法。"
    )
    avail_idx = store.index("func availableDates(")
    avail_end = store.index("func lines(", avail_idx)
    avail_block = store[avail_idx:avail_end]
    scan_idx = store.index("private func scanAvailableDates(")
    scan_block = store[scan_idx:store.index("func fileURL(", scan_idx)]
    assert "queue.async" in avail_block and "queue.sync" not in avail_block and "queue.sync" not in scan_block, (
        "HC-U22 违反：TranscriptStore.availableDates 必须在私有队列上 async 执行，不得同步阻塞调用方。"
    )
    assert "parseDateKey(" in store and '.hasSuffix(".txt")' in store and "isDirectory" in store, (
        "HC-U22 违反：TranscriptStore 列日期方法必须校验 yyyy-MM-dd.txt 文件名格式并排除目录。"
    )
    assert "isLenient = false" in store and "dateFormatter.string(from: parsedDate) == key" in store, (
        "HC-U22 违反：TranscriptStore 必须做非宽松日期解析与往返校验，拒绝 2026-02-30 这类非法日期。"
    )
