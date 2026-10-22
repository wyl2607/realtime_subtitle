import Foundation

/// 节点协议 v2 的 hello 消息。语言码须匹配 `^[a-z]{2,3}(-[A-Za-z]{2,4})?$`，否则构造失败。
struct NodeHello {
    let src: String
    let dst: String

    private static let languageCodePattern = "^[a-z]{2,3}(-[A-Za-z]{2,4})?$"

    init?(src: String, dst: String) {
        guard Self.isValidLanguageCode(src), Self.isValidLanguageCode(dst) else {
            return nil
        }
        self.src = src
        self.dst = dst
    }

    static func isValidLanguageCode(_ code: String) -> Bool {
        code.range(of: languageCodePattern, options: .regularExpression) != nil
    }

    func json() -> String {
        "{\"type\":\"hello\",\"v\":2,\"src\":\"\(src)\",\"dst\":\"\(dst)\",\"sample_rate\":16000,\"format\":\"s16le\"}"
    }
}

/// 节点发给客户端的事件。`parse` 对字段缺失/类型不对/未知 `ev`/非 JSON 一律返回 nil。
enum NodeEvent {
    case ready(cold: Bool)
    case final(id: Int, a0: Double, a1: Double, text: String)
    case translation(id: Int, text: String)
    case status(code: String, text: String, id: Int?)
    case drained

    static func parse(_ text: String) -> NodeEvent? {
        guard let data = text.data(using: .utf8),
              let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let ev = object["ev"] as? String else {
            return nil
        }
        switch ev {
        case "ready":
            guard let cold = boolValue(object["cold"]) else { return nil }
            return .ready(cold: cold)
        case "final":
            guard let id = intValue(object["id"]),
                  let a0 = doubleValue(object["a0"]),
                  let a1 = doubleValue(object["a1"]),
                  let text = object["text"] as? String else { return nil }
            return .final(id: id, a0: a0, a1: a1, text: text)
        case "translation":
            guard let id = intValue(object["id"]),
                  let text = object["text"] as? String else { return nil }
            return .translation(id: id, text: text)
        case "status":
            guard let code = object["code"] as? String,
                  let text = object["text"] as? String else { return nil }
            return .status(code: code, text: text, id: intValue(object["id"]))
        case "drained":
            return .drained
        default:
            return nil
        }
    }

    /// 从 `status/loading` 帧里取 `elapsed_s`（节点从开始加载本会话起算的秒数）。
    /// 不把它塞进 `.status` 的关联值：那个 case 的签名被 SampleHandler 的既有
    /// 模式匹配依赖，而本轮不接 SampleHandler（另一条线在改），多加一个关联值
    /// 会直接编译失败。其它帧、缺失或类型不对一律返回 nil。
    static func loadingElapsed(from text: String) -> Double? {
        guard let data = text.data(using: .utf8),
              let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              object["ev"] as? String == "status",
              object["code"] as? String == "loading" else {
            return nil
        }
        return doubleValue(object["elapsed_s"])
    }

    /// 这些码通常意味着会话将随 close 1011 结束，客户端应放弃重试。
    static func isFatal(code: String) -> Bool {
        code == "protocol_error" || code == "engine_load_failed" || code == "internal_error"
    }

    /// URLSession 会把服务端的 1013 报成 1005、真正的码在 reason 载荷前两字节（大端）。
    /// 见 CLAUDE.md 第 7 节第 17 条。其它情况原样返回。
    static func closeReason(code: Int, reasonData: Data?) -> Int {
        guard code == 1005, let reasonData, reasonData.count >= 2 else {
            return code
        }
        let bytes = reasonData.prefix(2)
        return (Int(bytes[bytes.startIndex]) << 8) | Int(bytes[bytes.startIndex + 1])
    }

    private static func boolValue(_ value: Any?) -> Bool? {
        guard let number = value as? NSNumber else { return nil }
        if CFGetTypeID(number) == CFBooleanGetTypeID() {
            return number.boolValue
        }
        return nil
    }

    private static func intValue(_ value: Any?) -> Int? {
        guard let number = value as? NSNumber else { return nil }
        if CFGetTypeID(number) == CFBooleanGetTypeID() {
            return nil
        }
        let double = number.doubleValue
        guard double.rounded() == double,
              double >= Double(Int.min),
              double <= Double(Int.max) else {
            return nil
        }
        return Int(double)
    }

    private static func doubleValue(_ value: Any?) -> Double? {
        guard let number = value as? NSNumber else { return nil }
        if CFGetTypeID(number) == CFBooleanGetTypeID() {
            return nil
        }
        return number.doubleValue
    }
}

/// ready 截止时间的跨线程容器：收包侧在 loading 时续期，看门狗按当前值重新睡。
/// 语义与 `macos-native/Sources/rslite/NodeClient.swift` 的 `ReadyDeadlineBox` 一致。
final class ReadyDeadlineBox: @unchecked Sendable {
    private let helloAt: Double
    private let lock = NSLock()
    private var deadline: Double

    init(helloAt: Double, current: Double) {
        self.helloAt = helloAt
        self.deadline = current
    }

    var current: Double {
        lock.lock()
        defer { lock.unlock() }
        return deadline
    }

    func extend(now: Double) {
        lock.lock()
        deadline = NodeSession.readyDeadline(
            helloAt: helloAt, now: now, current: deadline, receivedLoading: true
        )
        lock.unlock()
    }
}

/// v2 节点会话客户端。不自动重连、不做 TailnetPin（以后做）。
/// 日志/打印绝不输出 token 与字幕正文——本类不做任何日志输出。
final class NodeSession: NSObject, URLSessionWebSocketDelegate {
    // 冷启动＝拉起 worker + 加载权重，Windows 节点实测 17–20s。基础等 ready 15s
    // （与 rslite/Python 客户端同值），每收到一条 loading 续 15s，但从开始建连
    // 起总共不超过 60s；超过仍按握手超时收场。
    static let readyTimeoutSeconds = 15.0
    static let readyLoadingMaxSeconds = 60.0

    /// 纯函数：loading 到达时把截止时间推到 now+15s，但不超过 helloAt+60s。
    /// 与 rslite 的 `NodeClient.readyDeadline`、Python 的 `extend_ready_deadline` 同语义。
    static func readyDeadline(
        helloAt: Double,
        now: Double,
        current: Double,
        receivedLoading: Bool,
        timeout: Double = readyTimeoutSeconds,
        maxTotal: Double = readyLoadingMaxSeconds
    ) -> Double {
        let cap = helloAt + maxTotal
        if receivedLoading {
            return min(cap, now + timeout)
        }
        return min(cap, current)
    }

    private static func monotonic() -> Double {
        ProcessInfo.processInfo.systemUptime
    }

    private let url: URL
    private let token: String
    private let src: String
    private let dst: String
    private let onEvent: (NodeEvent) -> Void
    private let onClose: (Int) -> Void
    private let onLoading: ((Double) -> Void)?
    private let readyTimeout: Double

    private let lock = NSLock()
    private var session: URLSession?
    private var socket: URLSessionWebSocketTask?
    private var closeNotified = false
    private var readyBox: ReadyDeadlineBox?
    private var readyWatchdog: DispatchWorkItem?

    init(
        url: URL,
        token: String,
        src: String,
        dst: String,
        onEvent: @escaping (NodeEvent) -> Void,
        onClose: @escaping (Int) -> Void,
        onLoading: ((Double) -> Void)? = nil,
        readyTimeout: Double = readyTimeoutSeconds
    ) {
        self.url = url
        self.token = token
        self.src = src
        self.dst = dst
        self.onEvent = onEvent
        self.onClose = onClose
        self.onLoading = onLoading
        self.readyTimeout = readyTimeout
    }

    func start() {
        lock.lock()
        guard !closeNotified else {
            lock.unlock()
            return
        }
        lock.unlock()
        guard let hello = NodeHello(src: src, dst: dst) else {
            notifyClosed(1008)
            return
        }
        var request = URLRequest(url: url)
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        let configuration = URLSessionConfiguration.ephemeral
        let session = URLSession(configuration: configuration, delegate: self, delegateQueue: nil)
        let socket = session.webSocketTask(with: request)
        // 截止时间从开始建连起算：握手卡住时 send 回调不会返回，必须先让看门狗接管。
        let helloAt = Self.monotonic()
        let box = ReadyDeadlineBox(
            helloAt: helloAt,
            current: Self.readyDeadline(
                helloAt: helloAt,
                now: helloAt,
                current: helloAt + readyTimeout,
                receivedLoading: false
            )
        )
        lock.lock()
        guard !closeNotified else {
            lock.unlock()
            return
        }
        self.session = session
        self.socket = socket
        self.readyBox = box
        // start 与 close 在同一把锁内交接：finish 若已先关闭，就不能在它之后再建活连接。
        socket.resume()
        lock.unlock()
        armReadyWatchdog()
        socket.send(.string(hello.json())) { [weak self] error in
            if error != nil {
                self?.notifyClosed(1006)
                return
            }
            self?.receiveLoop()
        }
    }

    /// 发一帧 16kHz 单声道 s16le 音频。调用方保证 ≤ 64KB 且字节数为偶数。
    func send(pcm16k: Data) {
        guard let socket else { return }
        socket.send(.data(pcm16k)) { _ in }
    }

    func flush() {
        guard let socket else { return }
        socket.send(.string(#"{"type":"flush"}"#)) { _ in }
    }

    /// 发 close 1000 并结束会话。
    func close() {
        cancelReadyWatch()
        // 显式关闭不是故障：先占住「已通知」位，之后 URLSession 的取消回调
        // （didCompleteWithError(cancelled)）就不会再把 onClose 报给上层，
        // 否则上层会把它当成断线去重连。
        lock.lock()
        closeNotified = true
        let socket = self.socket
        let session = self.session
        lock.unlock()
        socket?.cancel(with: .normalClosure, reason: nil)
        session?.finishTasksAndInvalidate()
    }

    /// 分发一帧文本事件。receiveLoop 调用；自测也直接喂假帧，不建真连接。
    func handle(eventText: String) {
        guard let event = NodeEvent.parse(eventText) else { return }
        switch event {
        case .ready:
            cancelReadyWatch()
        case .status(let code, _, _) where code == "loading":
            noteLoading(elapsed: NodeEvent.loadingElapsed(from: eventText) ?? 0)
        default:
            break
        }
        lock.lock()
        let closed = closeNotified
        lock.unlock()
        // 只查不持锁回调：onEvent 会一路进上层状态队列和自动档决策，持着这把锁回调有死锁风险；
        // 查完到回调之间的窄窗口由上层「会话仍活着」的闸门兜住。
        guard !closed else { return }
        onEvent(event)
    }

    private func receiveLoop() {
        guard let socket else { return }
        socket.receive { [weak self] result in
            switch result {
            case .success(let message):
                if case .string(let text) = message {
                    self?.handle(eventText: text)
                }
                self?.receiveLoop()
            case .failure:
                if socket.closeCode != .invalid {
                    self?.notifyClosed(NodeEvent.closeReason(code: socket.closeCode.rawValue, reasonData: socket.closeReason))
                } else {
                    self?.notifyClosed(1006)
                }
            }
        }
    }

    /// 收到 loading：续期 ready 截止时间，并把 elapsed_s 交给上层（本轮只回调，不接 UI）。
    private func noteLoading(elapsed: Double) {
        lock.lock()
        guard !closeNotified else {
            lock.unlock()
            return
        }
        let box = readyBox
        lock.unlock()
        box?.extend(now: Self.monotonic())
        onLoading?(elapsed)
    }

    /// 让看门狗睡到当前截止时间。被续期时旧 work item 会到点先醒，发现截止时间
    /// 变远就再睡一次——和 rslite 超时任务按 `deadline.current` 循环的语义相同。
    private func armReadyWatchdog() {
        lock.lock()
        guard let box = readyBox else {
            lock.unlock()
            return
        }
        readyWatchdog?.cancel()
        let remaining = box.current - Self.monotonic()
        let work = DispatchWorkItem { [weak self] in
            self?.readyWatchdogFired()
        }
        readyWatchdog = work
        lock.unlock()
        DispatchQueue.global(qos: .utility).asyncAfter(
            deadline: .now() + max(0, remaining), execute: work
        )
    }

    private func readyWatchdogFired() {
        lock.lock()
        guard let box = readyBox else {
            lock.unlock()
            return
        }
        lock.unlock()
        let remaining = box.current - Self.monotonic()
        if remaining > 0 {
            armReadyWatchdog()
            return
        }
        // 到点仍未 ready：按握手超时结束会话。rslite 到点用 .goingAway 取消 socket
        // 再上报 timeout，这里同样以 1001 通知 onClose，让调用方走正常重连退避。
        lock.lock()
        readyBox = nil
        let socket = self.socket
        lock.unlock()
        socket?.cancel(with: .goingAway, reason: nil)
        notifyClosed(1001)
    }

    private func cancelReadyWatch() {
        lock.lock()
        readyBox = nil
        readyWatchdog?.cancel()
        readyWatchdog = nil
        lock.unlock()
    }

    private func notifyClosed(_ code: Int) {
        cancelReadyWatch()
        lock.lock()
        guard !closeNotified else {
            lock.unlock()
            return
        }
        closeNotified = true
        lock.unlock()
        onClose(code)
    }

    func urlSession(
        _ session: URLSession,
        webSocketTask: URLSessionWebSocketTask,
        didCloseWith closeCode: URLSessionWebSocketTask.CloseCode,
        reason: Data?
    ) {
        notifyClosed(NodeEvent.closeReason(code: closeCode.rawValue, reasonData: reason))
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        if error != nil {
            notifyClosed(1006)
        }
    }
}
