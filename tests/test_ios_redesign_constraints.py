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


def _project_target_sources(project: str, target_name: str) -> list[str]:
    m = re.search(rf"^  {re.escape(target_name)}:\n(.*?)(?=^  \w+:|\Z)", project, re.M | re.S)
    assert m, f"HC-U20 违反：ios/project.yml 缺少 target {target_name}。"
    src = re.search(r"^    sources:\n(.*?)(?=^    \w+:|\Z)", m.group(1), re.M | re.S)
    assert src, f"HC-U20 违反：ios/project.yml 中 {target_name} 缺少 sources。"
    return re.findall(r"^\s*-\s*path:\s*(\S+)", src.group(1), re.M)


def _covers_path(sources: list[str], rel_path: str) -> bool:
    rel = Path(rel_path)
    return any(rel == Path(s) or Path(s) in rel.parents for s in sources)


def _strip_swift_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def _target_swift_files(sources: list[str]) -> list[Path]:
    files: list[Path] = []
    for src in sources:
        p = IOS / src
        if p.is_dir():
            files.extend(sorted(p.rglob("*.swift")))
        elif p.is_file() and p.suffix == ".swift":
            files.append(p)
    return files


def test_hc_u20_app_intents_and_control_widget():
    """硬约束 HC-U20：App Intents 快捷指令 + 控制中心按钮（不程序化启动广播）。

    1. 必须存在 AppShortcutsProvider，且包含开始麦克风与停止麦克风两条快捷指令。
    2. 不得出现「开始广播」类 AppIntent（ReplayKit 只能由系统 Broadcast Picker 启动）。
    3. StartMicCaptionsIntent 必须声明前台运行（iOS 26 supportedModes = .foreground），留在主 App。
    4. RSLiveActivity 的每个 ControlWidget 都必须在 WidgetBundle 中注册，且其
       ControlWidgetButton 必须使用 OpenURLIntent / OpenIntent；start-mic URL 的 scheme
       必须注册在主 App 的 Info.plist，并由 RootView/AppModel+Intents.swift 路由；
       RSLiveActivity 的 sources 与源码中不得出现 AppModel、AppDependencyManager、@Dependency。
    """
    all_swift = list(_swift_files())

    # 1) AppShortcutsProvider + 开始/停止两条
    shortcut_files = [
        p for p in all_swift
        if re.search(r":\s*AppShortcutsProvider\b", _strip_swift_comments(p.read_text(encoding="utf-8")))
    ]
    assert shortcut_files, "HC-U20 违反：缺少遵守 AppShortcutsProvider 的快捷指令提供者。"
    shortcut_text = "\n".join(_strip_swift_comments(p.read_text(encoding="utf-8")) for p in shortcut_files)
    assert "StartMicCaptionsIntent(" in shortcut_text and "StopCaptionsIntent(" in shortcut_text, (
        "HC-U20 违反：AppShortcutsProvider 必须同时包含 StartMicCaptionsIntent 与 StopCaptionsIntent。"
    )
    assert shortcut_text.count("AppShortcut(") >= 2, (
        "HC-U20 违反：AppShortcutsProvider 至少要有开始/停止两条 AppShortcut。"
    )

    # 2) 不得出现「开始广播」类 intent
    broadcast_offenders = []
    for path in all_swift:
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"\b(?:struct|class)\s+(\w*Broadcast\w*)\s*:\s*[^\{]*AppIntent\b", text, re.I):
            broadcast_offenders.append(f"{_locate(path, text, m.start())} ({m.group(1)})")
        if re.search(r":\s*[^\{]*\bAppIntent\b", text):
            for kw in ("RPBroadcast", "ReplayKit", "开始广播", "开始屏幕广播"):
                if kw in text:
                    broadcast_offenders.append(f"{path.relative_to(ROOT)} (含 {kw})")
    assert not broadcast_offenders, (
        "HC-U20 违反：检测到广播类 AppIntent（ReplayKit 不能被程序化启动）：" + ", ".join(broadcast_offenders)
    )

    # 3) StartMicCaptionsIntent 必须声明前台运行且编入 RSiOS
    start_intent_files = [
        p for p in all_swift
        if re.search(r"\bstruct\s+StartMicCaptionsIntent\s*:\s*[^\{]*\bAppIntent\b", p.read_text(encoding="utf-8"))
    ]
    assert len(start_intent_files) == 1, (
        f"HC-U20 违反：StartMicCaptionsIntent 必须存在且唯一，当前找到 {len(start_intent_files)} 处。"
    )
    start_intent_file = start_intent_files[0]
    start_intent_code = _strip_swift_comments(start_intent_file.read_text(encoding="utf-8"))
    assert re.search(r"supportedModes\s*(?::\s*IntentModes)?\s*(?:=\s*|\{\s*(?:return\s+)?)\.foreground\b", start_intent_code), (
        "HC-U20 违反：StartMicCaptionsIntent 必须声明前台运行（supportedModes = .foreground）。"
    )
    assert not re.search(r"\bstatic\s+(?:let|var)\s+openAppWhenRun\b", start_intent_code), (
        "HC-U20 违反：部署目标已为 iOS 26，StartMicCaptionsIntent 应使用 supportedModes 而非已弃用的 openAppWhenRun。"
    )

    project = (IOS / "project.yml").read_text(encoding="utf-8")
    app_sources = _project_target_sources(project, "RSiOS")
    ext_sources = _project_target_sources(project, "RSLiveActivity")
    start_intent_rel = str(start_intent_file.relative_to(IOS))
    assert _covers_path(app_sources, start_intent_rel), (
        f"HC-U20 违反：ios/project.yml 中 RSiOS 的 sources 未包含 {start_intent_rel}。"
    )

    # 4) 每个 ControlWidget 都必须注册在 WidgetBundle，且使用 OpenURLIntent / OpenIntent；
    #    start-mic URL 必须闭环到主 App 的 URL scheme 和路由；扩展不得依赖主 App。
    widget_files = list(sorted((IOS / "RSLiveActivity").rglob("*.swift")))
    control_widgets = []
    widget_bundle_text = ""
    for path in widget_files:
        text = _strip_swift_comments(path.read_text(encoding="utf-8"))
        for m in re.finditer(r"\bstruct\s+(\w+)\s*:\s*ControlWidget\b", text):
            body = text[m.start():]
            next_struct = re.search(r"\n\s*struct\s+\w+\s*:", body[1:])
            if next_struct:
                body = body[:next_struct.start() + 1]
            control_widgets.append((path, body, m.group(1)))
        if re.search(r":\s*WidgetBundle\b", text):
            widget_bundle_text += text
    assert control_widgets, "HC-U20 违反：ios/RSLiveActivity 缺少 ControlWidget 实现。"
    start_mic_urls = []
    for cw_path, cw_text, cw_name in control_widgets:
        assert f"{cw_name}()" in widget_bundle_text, (
            f"HC-U20 违反：ControlWidget `{cw_name}` 没有在 RSLiveActivity 的 WidgetBundle 中注册。"
        )
        btn_match = re.search(r"ControlWidgetButton\s*\(\s*action:\s*([A-Za-z_]\w*)\s*\(", cw_text)
        assert btn_match, f"HC-U20 违反：{cw_path.relative_to(ROOT)} 中没有使用 ControlWidgetButton(action: ...)。"
        used_action_name = btn_match.group(1)
        is_open_url = used_action_name == "OpenURLIntent"
        is_open_intent = any(
            re.search(
                rf"\bstruct\s+{re.escape(used_action_name)}\s*:\s*[^\{{\n]*\bOpenIntent\b",
                _strip_swift_comments(p.read_text(encoding="utf-8")),
            )
            for p in widget_files
        )
        assert is_open_url or is_open_intent, (
            f"HC-U20 违反：{cw_path.relative_to(ROOT)} 的 ControlWidgetButton 必须使用 OpenURLIntent 或 OpenIntent，"
            f"当前使用了 `{used_action_name}`。"
        )
        if is_open_url and "start-mic" in cw_text:
            raw_widget_text = cw_path.read_text(encoding="utf-8")
            start_mic_urls.extend(
                re.findall(r"URL\s*\(string:\s*[\"']([^\"']*start-mic[^\"']*)[\"']\s*\)", raw_widget_text)
            )

    assert start_mic_urls, "HC-U20 违反：ControlWidget 源码中缺少 start-mic URL。"
    schemes = {url.split(":", 1)[0].lower() for url in start_mic_urls}
    assert len(schemes) == 1, f"HC-U20 违反：start-mic URL 使用了多个 scheme：{sorted(schemes)}。"
    start_mic_scheme = schemes.pop()
    info = plistlib.loads((IOS / "RSiOS" / "Info.plist").read_bytes())
    registered_schemes = {
        scheme.lower()
        for url_type in info.get("CFBundleURLTypes", [])
        for scheme in url_type.get("CFBundleURLSchemes", [])
    }
    assert start_mic_scheme in registered_schemes, (
        f"HC-U20 违反：start-mic URL scheme `{start_mic_scheme}` 未注册在 ios/RSiOS/Info.plist 的 CFBundleURLSchemes 中。"
    )
    root = (IOS / "RSiOS" / "Pages" / "RootView.swift").read_text(encoding="utf-8")
    intents = (IOS / "RSiOS" / "Intents" / "AppModel+Intents.swift").read_text(encoding="utf-8")
    assert "handleStartMicOpenURL(url)" in root, "HC-U20 违反：RootView 没有把打开 URL 路由给 handleStartMicOpenURL。"
    assert "handleStartMicOpenURL" in intents and "MicCaptionIntentPolicy.isStartMicURL" in intents, (
        "HC-U20 违反：AppModel+Intents.swift 没有实现 start-mic URL 路由。"
    )
    policy = (IOS / "Shared" / "MicCaptionIntentPolicy.swift").read_text(encoding="utf-8")
    assert re.search(rf"startMicScheme\s*=\s*[\"']{re.escape(start_mic_scheme)}[\"']", policy), (
        f"HC-U20 违反：MicCaptionIntentPolicy 没有使用 start-mic URL scheme `{start_mic_scheme}`。"
    )

    forbidden_ext_paths = [
        "RSiOS/Intents/StartMicCaptionsIntent.swift",
        "Shared/MicCaptionIntentPolicy.swift",
        "Shared/CaptureArbiter.swift",
    ]
    unexpected_ext_paths = [p for p in forbidden_ext_paths if _covers_path(ext_sources, p)]
    assert not unexpected_ext_paths, (
        "HC-U20 违反：RSLiveActivity 的 sources 不应再包含：" + ", ".join(unexpected_ext_paths)
    )

    ext_source_hits = [
        s for s in ext_sources
        if re.search(r"\bAppModel\b|\bAppDependencyManager\b|@Dependency\b", s)
    ]
    assert not ext_source_hits, (
        "HC-U20 违反：RSLiveActivity 的 sources 路径出现禁止符号：" + ", ".join(ext_source_hits)
    )

    ext_code_offenders = []
    seen_ext_files = set(widget_files) | set(_target_swift_files(ext_sources))
    for path in sorted(seen_ext_files):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"\bAppModel\b|\bAppDependencyManager\b|@Dependency\b", text):
            ext_code_offenders.append(f"{_locate(path, text, m.start())} ({m.group(0)})")
    assert not ext_code_offenders, (
        "HC-U20 违反：RSLiveActivity 源码中不得出现 AppModel / AppDependencyManager / @Dependency："
        + ", ".join(ext_code_offenders)
    )


def test_start_mic_intent_does_not_claim_capture_started():
    """权限拒绝或音频初始化失败时，Intent 只能报告正在启动，不能谎称已经开始。"""
    intent = (IOS / "RSiOS" / "Intents" / "StartMicCaptionsIntent.swift").read_text(encoding="utf-8")
    start_case = intent[intent.index("case .start:"):intent.index("case .ignoreAlreadyRunning:")]
    assert 'IntentDialog("正在启动麦克风字幕")' in start_case
    assert 'IntentDialog("已开始麦克风字幕")' not in start_case
