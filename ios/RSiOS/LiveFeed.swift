import Foundation
import Observation
import os

/// 主 App 唯一的运行态来源：扩展状态行、最近句对、未定稿草稿。
///
/// 为什么合并：这三份值都存在共享 Keychain 里，广播扩展每写一次就发一条 Darwin
/// 通知。以前主界面和画中画各自开定时器各读各的——四个 0.5 秒轮询叠起来约每秒
/// 十四次 SecItem 读取，而且内容没变也照样解 JSON、发布、整棵视图树重绘。
/// 现在只在这里读一次，内容真的变了才写属性（@Observable 只在属性变化时通知）。
///
/// 为什么还留 0.5 秒轮询：Darwin 通知在扩展崩溃、系统合并通知时会丢；
/// 定时器只是兜底，让状态最多晚半秒，节奏和原来四个定时器一致。
@MainActor
@Observable
final class LiveFeed {
    static let shared = LiveFeed()

    /// 内容真的变了才发。画中画未激活且 App 在前台时不再按定时器整帧重绘，
    /// 靠这条通知在句子/状态变化时补一帧。
    static let didChangeNotification = Notification.Name("com.wyl2607.rsios.livefeed.didChange")

    /// 兜底轮询间隔（秒）。
    static let pollInterval: TimeInterval = 0.5

    private(set) var status: String = "无"
    /// 只有 Darwin 通知才算「扩展刚写过」；兜底轮询读到旧值不能延长广播占用时间。
    private(set) var lastStatusUpdate: Date?
    private(set) var lines: [RecentLine] = []
    private(set) var draft: String = ""
    /// 诊断页用的两个计数：扩展分别写过多少次状态 / 句对。
    private(set) var statusNotificationCount = 0
    private(set) var linesNotificationCount = 0

    private let readStatus: () -> String?
    private let readLines: () -> String?
    private let readDraft: () -> String?
    private let now: () -> Date
    private let logger = Logger(subsystem: "com.wyl2607.rsios.spike", category: "live-feed")

    private var lastLinesJSON: String?
    private var timer: Timer?
    private var darwinObservers: [UnsafeRawPointer] = []

    init(
        readStatus: @escaping () -> String? = { SharedKeychain.readExtStatus() },
        readLines: @escaping () -> String? = { SharedKeychain.readLatestLines() },
        readDraft: @escaping () -> String? = { SharedKeychain.readLatestDraft() },
        now: @escaping () -> Date = Date.init
    ) {
        self.readStatus = readStatus
        self.readLines = readLines
        self.readDraft = readDraft
        self.now = now
        start()
    }

    isolated deinit {
        timer?.invalidate()
        let center = CFNotificationCenterGetDarwinNotifyCenter()
        for observer in darwinObservers {
            CFNotificationCenterRemoveObserver(center, observer, nil, nil)
        }
    }

    /// 读一次三份运行态，只有内容变化才更新属性。
    func refresh() {
        var changed = false

        // 读不到（锁屏时 Keychain 拒绝、或条目还没写过）就保留上一次的值，不改成「无」/清空：
        // 读失败不代表扩展停了，清掉会让字幕、画中画、锁屏实时活动一起变空。
        if let newStatus = readStatus(), newStatus != status {
            status = newStatus
            changed = true
        }

        if let linesJSON = readLines(), linesJSON != lastLinesJSON {
            lastLinesJSON = linesJSON
            // 坏 JSON 保留上一份好句对：扩展写到一半时不能把字幕清空。
            if let decoded = Self.decodeLines(linesJSON) {
                lines = decoded
                changed = true
            } else {
                logger.error("LiveFeed latest lines decode failed")
            }
        }

        if let newDraft = readDraft(), newDraft != draft {
            draft = newDraft
            changed = true
        }

        if changed {
            NotificationCenter.default.post(name: Self.didChangeNotification, object: self)
        }
    }

    private static func decodeLines(_ json: String) -> [RecentLine]? {
        guard let data = json.data(using: .utf8) else { return nil }
        return try? JSONDecoder().decode([RecentLine].self, from: data)
    }

    private func start() {
        refresh()
        registerDarwinNotifications()
        timer = Timer.scheduledTimer(withTimeInterval: Self.pollInterval, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.refresh() }
        }
        timer?.fire()
    }

    /// 状态/句对各一条 Darwin 通知：只有状态那条能把「扩展刚写过」的时间往前推。
    private func registerDarwinNotifications() {
        let center = CFNotificationCenterGetDarwinNotifyCenter()
        let observer = UnsafeRawPointer(Unmanaged.passUnretained(self).toOpaque())

        CFNotificationCenterAddObserver(center, observer, { _, observer, _, _, _ in
            guard let observer else { return }
            let feed = Unmanaged<LiveFeed>.fromOpaque(observer).takeUnretainedValue()
            DispatchQueue.main.async {
                feed.statusNotificationCount += 1
                feed.lastStatusUpdate = feed.now()
                feed.refresh()
            }
        }, "com.wyl2607.rsios.spike.tick" as CFString, nil, .deliverImmediately)

        CFNotificationCenterAddObserver(center, observer, { _, observer, _, _, _ in
            guard let observer else { return }
            let feed = Unmanaged<LiveFeed>.fromOpaque(observer).takeUnretainedValue()
            DispatchQueue.main.async {
                feed.linesNotificationCount += 1
                feed.refresh()
            }
        }, "com.wyl2607.rsios.spike.lines" as CFString, nil, .deliverImmediately)

        darwinObservers.append(observer)
    }
}

struct RecentLine: Codable, Identifiable, Equatable {
    let id: Int
    let src: String
    var dst: String
    /// 产生这一句的引擎会话代号：各引擎的 id 都从 1 重新计数，
    /// 存档靠「代号 + id」去重，不然后一个会话会覆盖前一个会话的句子。
    let session: String
}
