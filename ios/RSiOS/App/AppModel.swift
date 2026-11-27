import Foundation
import Observation

/// 底部四个标签页。用枚举而不是下标：深链接和代码里跳页都按名字走，不会因为调整顺序跳错页。
enum AppTab: Hashable {
    case live
    case history
    case settings
    case diagnostics
}

/// 设置页的子页。
enum SettingsRoute: Hashable {
    case style
    case recognition
    case node
}

/// 主 App 的共享状态，四个页面都从环境里拿同一份。
///
/// 为什么集中：以前所有状态都是一个视图的 @State，拆页以后每页各持一份就会互相不同步
/// （例如设置页改了引擎档位，字幕页的提示不知道）。
@MainActor
@Observable
final class AppModel {
    let feed = LiveFeed.shared
    let pip = PiPManager()
    let store = TranscriptStore()
    let mic = MicCapture()
    let appState = AppStateMonitor.shared
    let assets = AssetPreparer()
    @ObservationIgnored private let liveActivity = LiveActivityController()
    @ObservationIgnored private var feedObserver: NSObjectProtocol?
    @ObservationIgnored private var ingestedLines: [RecentLine] = []

    var selectedTab: AppTab = .live
    var settingsPath: [SettingsRoute] = []

    /// 状态词只在解析出档位时更新；解析返回 nil（如 started）时保留上一次，否则每秒闪一下空白。
    private(set) var statusSummary: StatusSummary?
    /// 字幕页滚动显示的句子：本次运行累积，最多 CaptionHistory.limit 句。
    private(set) var captionLines: [RecentLine] = []
    /// 每收到新句对 +1：回看页靠它触发重新读存档文件。
    private(set) var transcriptRefresh = 0

    var engineMode = "auto"
    private(set) var engineModeSaveFailed = false

    var pairingLinkText = ""
    private(set) var pairingStatus = ""
    private(set) var pairedConfigDisplay = ""

    /// Groq key 输入框内容，保存后立刻清空；完整 key 只在 Keychain 里。
    var groqKeyInput = ""
    private(set) var groqKeyMasked = ""
    var cloudEnabled = false
    private(set) var cloudKeyStatus = ""

    /// App 启动时调用一次：onChange 不会补发当前值，启动前扩展就写好的状态和句对要先处理。
    func bootstrap() {
        guard feedObserver == nil else { return }
        // 此刻在前台、已解锁：把老版本写下的 WhenUnlocked 条目改成锁屏可读。
        SharedKeychain.migrateAccessibility()
        // 存档和锁屏实时活动跟 LiveFeed 的变更通知走，不挂在视图的 onChange 上：
        // 熄屏/后台时 SwiftUI 不重新求值视图，onChange 不触发，存档和锁屏字幕就停了。
        feedObserver = NotificationCenter.default.addObserver(
            forName: LiveFeed.didChangeNotification,
            object: feed,
            queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.feedChanged() }
        }
        engineMode = SharedStore.readEngineMode()
        loadPairedConfig()
        loadCloudSettings()
        statusChanged(feed.status)
        ingestFeedLines()
        // 上次运行被系统杀掉时留下的锁屏活动：没在采集就立刻结束，在采集就接着用。
        refreshLiveActivity()
        assets.prepareIfMissing()
    }

    private func feedChanged() {
        statusChanged(feed.status)
        if feed.lines != ingestedLines { ingestFeedLines() }
        refreshLiveActivity()
    }

    func statusChanged(_ line: String) {
        if let summary = StatusSummaryParser.summary(forStatusLine: line) {
            statusSummary = summary
        }
    }

    /// 把最近句对交给存档。ingest 是异步合并写，不挡主线程。
    func ingestFeedLines() {
        ingestedLines = feed.lines
        captionLines = CaptionHistory.merge(captionLines, with: feed.lines)
        store.ingest(feed.lines.map {
            TranscriptStore.Sentence(id: $0.id, session: $0.session, src: $0.src, dst: $0.dst)
        })
        transcriptRefresh += 1
    }

    func backgroundChanged(_ inBackground: Bool) {
        // 存档是合并写的（约 1 秒一次）；进后台后 iOS 随时可能回收进程，必须立刻写掉。
        if inBackground { store.flush() }
    }

    func extensionStatusUpdated(_ updatedAt: Date?) {
        mic.reconcileBroadcast(extensionStatus: feed.status, extensionStatusUpdatedAt: updatedAt)
    }

    /// 广播在跑：和麦克风互斥用同一个判定。以前这里自己判「新鲜且不是 finished」，
    /// 没看 src——麦克风引擎自己也会发状态通知，结果麦克风停了以后锁屏活动还当广播在跑。
    private var broadcastActive: Bool {
        CaptureArbiter.decide(
            extensionStatus: feed.status,
            extensionStatusUpdatedAt: feed.lastStatusUpdate,
            now: Date(),
            micIsRunning: false
        ) == .rejectMicStart
    }

    /// 锁屏实时活动跟着采集状态和最新一句走。熄屏后画中画看不见，这是唯一的字幕出口。
    func refreshLiveActivity() {
        let latest = feed.lines.last
        let state = SubtitleActivityAttributes.ContentState(
            source: latest?.src ?? "",
            // 译文比原文晚到：没到时用和字幕卡片同样的占位，锁屏上不留一行空白。
            translation: latest.map { SubtitleDisplay.translation($0.dst, isNewest: true).text } ?? "",
            status: statusSummary?.label ?? DesignTokens.Text.micStatusIdle
        )
        liveActivity.sync(
            capturing: mic.isRunning || broadcastActive,
            sourceName: mic.isRunning ? DesignTokens.Text.micSubtitleTitle : "屏幕广播",
            state: state
        )
    }

    func startMic() {
        mic.start(extensionStatus: feed.status, extensionStatusUpdatedAt: feed.lastStatusUpdate)
    }

    // MARK: - 引擎档位

    func saveEngineMode() {
        // 档位写进 App Group 共享容器；写失败时不能让界面看起来像已经生效。
        engineModeSaveFailed = !SharedStore.writeEngineMode(engineMode)
    }

    /// 只有启动后真的换过档位才提示，否则麦克风一开就常驻这句话。
    var engineModeNeedsRestart: Bool {
        guard mic.isRunning, let started = mic.startedEngineMode else { return false }
        return started != engineMode
    }

    // MARK: - 节点配对

    /// 相机扫配对码 / 点 rsnode://pair 链接：只填进配对框，用户点「应用配对」才写钥匙串。
    func handleOpenURL(_ url: URL) {
        guard let text = PairingLink.prefillText(for: url) else { return }
        pairingLinkText = text
        pairingStatus = "已填入配对链接，确认后点「应用配对」"
        selectedTab = .settings
        settingsPath = [.node]
    }

    func applyPairingLink() {
        let trimmed = pairingLinkText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            pairingStatus = "配对链接为空"
            return
        }
        guard let config = PairingLink.parse(trimmed) else {
            pairingStatus = "配对链接无效"
            pairedConfigDisplay = ""
            return
        }
        let json = (try? JSONEncoder().encode(config)).flatMap { String(data: $0, encoding: .utf8) } ?? ""
        if SharedKeychain.writeNodeConfig(json) {
            pairingStatus = "已配对"
            pairedConfigDisplay = config.displayName
            pairingLinkText = ""
        } else {
            pairingStatus = "配对写入失败"
        }
    }

    private func loadPairedConfig() {
        if let json = SharedKeychain.readNodeConfig(),
           let data = json.data(using: .utf8),
           let config = try? JSONDecoder().decode(NodeConfig.self, from: data) {
            pairedConfigDisplay = config.displayName
        }
    }

    // MARK: - 云端识别（Groq）

    private func loadCloudSettings() {
        cloudEnabled = SharedStore.readCloudEnabled()
        groqKeyMasked = SharedKeychain.maskedGroqKey() ?? ""
    }

    func saveCloudEnabled() {
        SharedStore.writeCloudEnabled(cloudEnabled)
    }

    /// key 只进 Keychain：写完立刻清空输入框，界面以后只留后 4 位。
    func saveGroqKey() {
        let key = groqKeyInput.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !key.isEmpty else { return }
        if SharedKeychain.writeGroqKey(key) {
            groqKeyInput = ""
            groqKeyMasked = SharedKeychain.maskedGroqKey() ?? ""
            cloudKeyStatus = "已保存到钥匙串"
        } else {
            cloudKeyStatus = "保存失败"
        }
    }

    /// 清掉 key：云端这一档立刻失效（扩展下次读到就没 key 了）。
    func clearGroqKey() {
        _ = SharedKeychain.clearGroqKey()
        groqKeyInput = ""
        groqKeyMasked = ""
        cloudKeyStatus = "已清除"
    }
}
