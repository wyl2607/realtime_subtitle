@preconcurrency import AVFoundation
import Foundation

struct NodeConfig: Codable, Equatable, Sendable {
    var id: String
    var nodeID: String
    var url: String
    var tokenFile: String

    enum CodingKeys: String, CodingKey {
        case id
        case nodeID = "node_id"
        case url
        case tokenFile = "token_file"
    }
}

struct NodeInfo: Codable, Equatable, Sendable {
    struct ASR: Codable, Equatable, Sendable {
        var model: String?
        var backend: String?
        var rtf: Double?
    }

    var v: Int
    var nodeID: String
    var hwHash: String
    var asr: ASR
    var translator: String
    var busy: Bool
    var onAC: Bool
    var inUse: Bool
    var worker: String

    enum CodingKeys: String, CodingKey {
        case v
        case nodeID = "node_id"
        case hwHash = "hw_hash"
        case asr
        case translator
        case busy
        case onAC = "on_ac"
        case inUse = "in_use"
        case worker
    }
}

enum NodeClientError: Error, CustomStringConvertible {
    case badURL(String)
    case tokenUnavailable(String)
    case tokenPermissions(String)
    case unauthorized
    case nodeIDMismatch(expected: String, got: String)
    case busy
    case timeout
    case protocolError(String)
    case connection(String)
    case pingTimeout
    /// 音频断档超过 maxPadGapSeconds：干净结束会话，不是节点故障（路由不标 offline、不罚时）。
    case audioGap

    var description: String {
        switch self {
        case .badURL(let value): return "节点地址不可用：\(value)"
        case .tokenUnavailable(let id): return "节点 \(id) 的 token 文件不可用"
        case .tokenPermissions(let id): return "节点 \(id) 的 token 文件权限必须是 0600"
        case .unauthorized: return "节点鉴权失败"
        case .nodeIDMismatch(let expected, let got): return "node_id 不一致：期望 \(expected)，实际 \(got)"
        case .busy: return "节点 busy"
        case .timeout: return "节点握手超时"
        case .protocolError(let message): return "节点协议错误：\(message)"
        case .connection(let message): return "节点连接失败：\(message)"
        case .pingTimeout: return "节点心跳超时"
        case .audioGap: return "音频断档过长，结束会话待重开"
        }
    }

    var reasonCode: String {
        switch self {
        case .badURL:
            return "bad_url"
        case .tokenUnavailable:
            return "token_unavailable"
        case .tokenPermissions:
            return "token_permissions"
        case .unauthorized:
            return "http_401"
        case .nodeIDMismatch:
            return "node_id_mismatch"
        case .busy:
            return "close_1013"
        case .timeout:
            return "handshake_timeout"
        case .pingTimeout:
            return "ping_timeout"
        case .audioGap:
            return "audio_gap"
        case .protocolError:
            return "protocol_error"
        case .connection(let message):
            if message.hasPrefix("info http ") {
                return "http_\(message.dropFirst("info http ".count))"
            }
            if message.hasPrefix("http ") {
                return "http_\(message.dropFirst("http ".count))"
            }
            if message.hasPrefix("close ") {
                return "close_\(message.dropFirst("close ".count))"
            }
            return "connection_error"
        }
    }
}

/// 节点会话的身份：节点 id + 该节点的会话代号。
struct NodeSessionRef: Hashable, Sendable {
    var nodeID: String
    var generation: Int
}

/// P7 / P2：与 gateway 约定一致，每 10s 一次 ping，10s 无 pong 判故障。
enum HeartbeatPolicy {
    static let interval = 10.0
    static let timeout = 10.0

    enum Action: Equatable {
        case none
        case sendPing
        case dead
    }

    /// 纯函数：有未回的 ping（lastPongAt 早于 lastPingSentAt）且超时 -> dead；
    /// 没有未回的 ping 且到期 -> 再发一个。
    static func step(
        now: Double,
        lastPingSentAt: Double,
        lastPongAt: Double,
        interval: Double = HeartbeatPolicy.interval,
        timeout: Double = HeartbeatPolicy.timeout
    ) -> Action {
        if lastPongAt < lastPingSentAt {
            return now - lastPingSentAt >= timeout ? .dead : .none
        }
        return now - lastPingSentAt >= interval ? .sendPing : .none
    }
}

struct NodeCallbacks: Sendable {
    var onReady: @Sendable (String, Int, NodeInfo) -> Void = { _, _, _ in }
    var onFinal: @Sendable (String, Int, Int, String, Double?, Double?) -> Void = { _, _, _, _, _, _ in }
    var onTranslation: @Sendable (String, Int, Int, String) -> Void = { _, _, _, _ in }
    var onStatus: @Sendable (String) -> Void = { _ in }
    var onClosed: @Sendable (String, Int, Error?) -> Void = { _, _, _ in }
}

/// v2 节点会话客户端。token 只从 0600 文件读，只放 Authorization 头，不进 URL/argv/日志。
@available(macOS 27.0, *)
final class NodeClient: NSObject, URLSessionWebSocketDelegate, URLSessionTaskDelegate, @unchecked Sendable {
    private static let wireFormat = AVAudioFormat(
        commonFormat: .pcmFormatInt16, sampleRate: 16_000, channels: 1, interleaved: true
    )!
    private static let frameBytes = 3200
    // 冷启动＝拉起 worker + import MLX + 加载权重，mini2 实测 >5s；预算对齐 RFC「冷启动后首条精修 ≤15s」，等待期间本机 B 照常出字
    private static let readyTimeout: Duration = .seconds(15)
    private static let closeTimeout: Duration = .seconds(2)

    private let config: NodeConfig
    private let token: String
    private let info: NodeInfo
    private let udsPath: String?
    private let sourceLocaleID: String
    private let targetLanguageID: String
    private var offsetSeconds: Double
    private let sessionGeneration: Int
    private let callbacks: NodeCallbacks
    private let lock = NSLock()

    private var socket: NodeWebSocketTask?
    private var session: URLSession?
    private var converter: AVAudioConverter?
    private var isClosed = false
    private var remoteCloseCode: Int?
    private var closedNotified = false
    private var heartbeatTask: Task<Void, Never>?
    /// 节点时钟 = 已发给节点的样本数 / 16000；首个音频块到来时把 offset 对齐到它的 startSeconds。
    private var clockStarted = false
    private var sentSamples = 0
    private let closeFrameReceived = OneShotGate()
    private let finished = OneShotGate()

    init(
        config: NodeConfig,
        token: String,
        info: NodeInfo,
        udsPath: String? = nil,
        sourceLocaleID: String,
        targetLanguageID: String,
        offsetSeconds: Double,
        sessionGeneration: Int,
        callbacks: NodeCallbacks
    ) {
        self.config = config
        self.token = token
        self.info = info
        self.udsPath = udsPath
        self.sourceLocaleID = sourceLocaleID
        self.targetLanguageID = targetLanguageID
        self.offsetSeconds = offsetSeconds
        self.sessionGeneration = sessionGeneration
        self.callbacks = callbacks
    }

    static func prepareUDS(path: String) async throws -> NodeInfo {
        try await UDSWebSocketTask.fetchInfo(path: path)
    }

    static func prepare(config: NodeConfig, timeout: Duration = .seconds(5)) async throws -> (String, NodeInfo) {
        guard let token = readTokenFile(config.tokenFile) else {
            throw NodeClientError.tokenUnavailable(config.id)
        }
        guard let infoURL = infoURL(from: config.url) else {
            throw NodeClientError.badURL(config.url)
        }
        var request = URLRequest(url: infoURL)
        request.timeoutInterval = timeout.secondsValue
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        let session = URLSession(configuration: directSessionConfiguration(timeout: timeout.secondsValue))
        defer { session.invalidateAndCancel() }
        let (data, response) = try await session.data(for: request)
        if let http = response as? HTTPURLResponse, http.statusCode == 401 {
            throw NodeClientError.unauthorized
        }
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw NodeClientError.connection("info http \((response as? HTTPURLResponse)?.statusCode ?? -1)")
        }
        let info = try JSONDecoder().decode(NodeInfo.self, from: data)
        guard info.v == 2 else {
            throw NodeClientError.protocolError("info v=\(info.v)")
        }
        guard info.nodeID == config.nodeID else {
            throw NodeClientError.nodeIDMismatch(expected: config.nodeID, got: info.nodeID)
        }
        return (token, info)
    }

    func connect() async throws {
        // P6 的 nodes.json 只记 ws://<名>:8791（install_node.sh 就这么写），会话路径由客户端补；
        // 不补的话升级请求打到 "/"，gateway 只认 /v2/session，真节点上必然连不上。
        if config.id == "local_uds" {
            guard let udsPath else {
                throw NodeClientError.badURL(config.id)
            }
            let udsTask = try UDSWebSocketTask(path: udsPath)
            lock.withLockVoid { self.socket = udsTask }
            udsTask.resume()
        } else {
            guard var components = URLComponents(string: config.url) else {
                throw NodeClientError.badURL(config.url)
            }
            if components.path.isEmpty || components.path == "/" {
                components.path = "/v2/session"
            }
            guard let url = components.url else {
                throw NodeClientError.badURL(config.url)
            }
            var request = URLRequest(url: url)
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            let sessionConfiguration = Self.directSessionConfiguration(timeout: 24 * 60 * 60)
            let session = URLSession(configuration: sessionConfiguration, delegate: self, delegateQueue: nil)
            let socket = session.webSocketTask(with: request)
            lock.withLockVoid {
                self.session = session
                self.socket = socket
            }
            socket.resume()
        }
        do {
            let hello: [String: Any] = [
                "type": "hello",
                "v": 2,
                "src": Self.languageCode(sourceLocaleID),
                "dst": Self.languageCode(targetLanguageID),
                "sample_rate": 16_000,
                "format": "s16le",
            ]
            guard let socket = currentSocket() else {
                throw NodeClientError.connection("socket closed")
            }
            try await socket.send(.string(Self.jsonString(hello)))
            try await awaitReady(socket)
            callbacks.onReady(config.id, sessionGeneration, info)
            Task { await receiveLoop(socket) }
            let heartbeat = Task { await self.heartbeatLoop(socket) }
            lock.withLockVoid { heartbeatTask = heartbeat }
        } catch {
            throw await classify(error, currentSocket())
        }
    }

    func send(_ audio: TimedAudio) async throws {
        guard let socket = currentSocket() else {
            throw NodeClientError.connection("socket closed")
        }
        if converter == nil {
            guard let c = AVAudioConverter(from: audio.buffer.format, to: Self.wireFormat) else {
                throw NodeClientError.protocolError("cannot create converter")
            }
            converter = c
        }
        guard let converter else {
            return
        }
        let data = try Self.convert(audio.buffer, using: converter)
        // 节点按收到的样本数推时钟（P2）。会话建立期间/发送变慢时 AudioFanout 会丢旧帧，
        // 客户端时钟前进了而节点没收到：用零 PCM 补上这段，节点 a0 才不会永久偏前（P5 替换对得上行）。
        let padResult = lock.withLockValue { () -> Int? in
            if !clockStarted {
                clockStarted = true
                offsetSeconds = audio.startSeconds
                return 0
            }
            return Self.boundedGapPadSamples(offset: offsetSeconds, sentSamples: sentSamples, audioStart: audio.startSeconds)
        }
        // 断档过长（如睡眠唤醒）不补零：补 30 分钟 = 57MB 零 PCM。抛 .audioGap，由路由干净结束会话并重开。
        guard let pad = padResult else {
            throw NodeClientError.audioGap
        }
        if pad > 0 {
            var remaining = pad * 2
            while remaining > 0 {
                let n = min(Self.frameBytes, remaining)
                try await socket.send(.data(Data(count: n)))
                remaining -= n
            }
            lock.withLockVoid { sentSamples += pad }
        }
        guard !data.isEmpty else {
            return
        }
        var pending = data
        while !pending.isEmpty {
            let n = min(Self.frameBytes, pending.count)
            try await socket.send(.data(Data(pending.prefix(n))))
            pending.removeFirst(n)
        }
        lock.withLockVoid { sentSamples += data.count / 2 }
    }

    /// 纯函数：该块音频开始前，节点时钟落后客户端时钟多少个样本需要补零（≤ 容差视为连续）。
    static func gapPadSamples(offset: Double, sentSamples: Int, audioStart: Double, tolerance: Double = 0.1) -> Int {
        let gap = audioStart - (offset + Double(sentSamples) / 16_000)
        guard gap > tolerance else {
            return 0
        }
        return Int((gap * 16_000).rounded())
    }

    /// 补零上限（秒）：大于正常调度抖动/短暂卡顿，远小于 gateway 积压上限；超过则结束会话而非补零。
    static let maxPadGapSeconds = 5.0

    /// 有上限的补零：nil = 断档超过 maxPadGapSeconds（调用方应结束会话，不补零）。
    static func boundedGapPadSamples(offset: Double, sentSamples: Int, audioStart: Double, tolerance: Double = 0.1) -> Int? {
        let gap = audioStart - (offset + Double(sentSamples) / 16_000)
        if gap > maxPadGapSeconds {
            return nil
        }
        return gapPadSamples(offset: offset, sentSamples: sentSamples, audioStart: audioStart, tolerance: tolerance)
    }

    func flush() async {
        try? await currentSocket()?.send(.string(#"{"type":"flush"}"#))
    }

    func drain(timeout: Duration = .seconds(3)) async {
        guard let socket = currentSocket() else {
            return
        }
        try? await socket.send(.string(#"{"type":"drain"}"#))
        let gate = OneShotGate()
        let token = NodeDrainWaiters.shared.add(session: NodeSessionRef(nodeID: config.id, generation: sessionGeneration), gate: gate)
        let timer = Task {
            try? await Task.sleep(for: timeout)
            gate.open()
        }
        await gate.wait()
        timer.cancel()
        NodeDrainWaiters.shared.remove(token)
    }

    func close() async {
        let pair = lock.withLockValue { () -> (NodeWebSocketTask?, URLSession?)? in
            guard !isClosed else {
                return nil
            }
            isClosed = true
            return (socket, session)
        }
        guard let (socket, session) = pair else {
            return
        }
        let heartbeat = lock.withLockValue { heartbeatTask }
        heartbeat?.cancel()
        socket?.cancel(with: .normalClosure, reason: nil)
        let timer = Task {
            try? await Task.sleep(for: Self.closeTimeout)
            finished.open()
        }
        await finished.wait()
        timer.cancel()
        session?.invalidateAndCancel()
    }

    func urlSession(
        _ session: URLSession,
        webSocketTask: URLSessionWebSocketTask,
        didCloseWith closeCode: URLSessionWebSocketTask.CloseCode,
        reason: Data?
    ) {
        lock.lock()
        remoteCloseCode = Self.effectiveCode(closeCode, reason)
        lock.unlock()
        closeFrameReceived.open()
        finished.open()
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        finished.open()
    }

    private func awaitReady(_ socket: NodeWebSocketTask) async throws {
        try await withThrowingTaskGroup(of: Void.self) { group in
            group.addTask {
                while true {
                    let message = try await socket.receive()
                    if case .string(let text) = message,
                       Self.parseEvent(text)?["ev"] as? String == "ready" {
                        return
                    }
                }
            }
            group.addTask {
                do {
                    try await Task.sleep(for: Self.readyTimeout)
                } catch {
                    return
                }
                socket.cancel(with: .goingAway, reason: nil)
                throw NodeClientError.timeout
            }
            do {
                try await group.next()
                group.cancelAll()
            } catch {
                group.cancelAll()
                throw await classify(error, currentSocket())
            }
        }
    }

    private func receiveLoop(_ socket: NodeWebSocketTask) async {
        while true {
            do {
                let message = try await socket.receive()
                guard case .string(let text) = message else {
                    continue
                }
                handle(text)
            } catch {
                let closed = lock.withLockValue { isClosed }
                finished.open()
                notifyClosed(closed ? nil : error)
                return
            }
        }
    }

    /// onClosed 每个会话只通知一次（receiveLoop 与心跳都可能触发）。
    private func notifyClosed(_ error: Error?) {
        let first = lock.withLockValue { () -> Bool in
            guard !closedNotified else { return false }
            closedNotified = true
            return true
        }
        if first {
            callbacks.onClosed(config.id, sessionGeneration, error)
        }
    }

    /// P7：定期 ping，超时无 pong 判故障。静默断网时 receive/send 都不报错，只有心跳能发现。
    private func heartbeatLoop(_ socket: NodeWebSocketTask) async {
        let startedAt = Self.monotonic()
        let pongAt = PongClock(start: startedAt)
        var lastPingSentAt = startedAt
        while !Task.isCancelled {
            do {
                try await Task.sleep(for: .seconds(1))
            } catch {
                return
            }
            if lock.withLockValue({ isClosed }) {
                return
            }
            let now = Self.monotonic()
            switch HeartbeatPolicy.step(now: now, lastPingSentAt: lastPingSentAt, lastPongAt: pongAt.value) {
            case .none:
                continue
            case .sendPing:
                lastPingSentAt = now
                socket.sendPing { [weak self] error in
                    if let error {
                        self?.heartbeatFailed(socket, error)
                    } else {
                        pongAt.set(Self.monotonic())
                    }
                }
            case .dead:
                heartbeatFailed(socket, NodeClientError.pingTimeout)
                return
            }
        }
    }

    private func heartbeatFailed(_ socket: NodeWebSocketTask, _ error: Error) {
        let closed = lock.withLockValue { isClosed }
        notifyClosed(closed ? nil : error)
        socket.cancel(with: .goingAway, reason: nil)
    }

    private static func monotonic() -> Double {
        ProcessInfo.processInfo.systemUptime
    }

    private func handle(_ text: String) {
        guard let event = Self.parseEvent(text), let ev = event["ev"] as? String else {
            return
        }
        switch ev {
        case "final":
            guard let id = event["id"] as? Int, let body = event["text"] as? String else { return }
            let offset = lock.withLockValue { offsetSeconds }
            let t0 = (event["a0"] as? Double).map { $0 + offset }
            let t1 = (event["a1"] as? Double).map { $0 + offset }
            callbacks.onFinal(config.id, sessionGeneration, id, body, t0, t1)
        case "translation":
            guard let id = event["id"] as? Int, let body = event["text"] as? String else { return }
            callbacks.onTranslation(config.id, sessionGeneration, id, body)
        case "status":
            if let code = event["code"] as? String {
                callbacks.onStatus("节点 \(config.id)：\(code)")
            }
        case "drained":
            NodeDrainWaiters.shared.open(session: NodeSessionRef(nodeID: config.id, generation: sessionGeneration))
        default:
            return
        }
    }

    private func classify(_ error: Error, _ socket: NodeWebSocketTask?) async -> NodeClientError {
        if let e = error as? NodeClientError {
            return e
        }
        if let socket = socket as? URLSessionWebSocketTask, let http = socket.response as? HTTPURLResponse {
            if http.statusCode == 401 {
                return .unauthorized
            }
            // 101 = 升级成功（之后被 close 帧关掉，如 1013 busy），按 close 码分类，不是 HTTP 错误
            if !(200..<300).contains(http.statusCode) && http.statusCode != 101 {
                return .connection("http \(http.statusCode)")
            }
        }
        let timeout = Task {
            try? await Task.sleep(for: .seconds(1))
            closeFrameReceived.open()
        }
        await closeFrameReceived.wait()
        timeout.cancel()
        let code = lock.withLockValue { remoteCloseCode }
        if code == 1013 || socket?.nodeCloseCode == 1013 {
            return .busy
        }
        if let code, code != 1005 {
            return .connection("close \(code)")
        }
        if socket?.nodeCloseCode != 1005 {
            return .connection("close \(socket?.nodeCloseCode ?? 1005)")
        }
        return .connection(error.localizedDescription)
    }

    private func currentSocket() -> NodeWebSocketTask? {
        lock.withLockValue { socket }
    }

    private static func convert(_ buffer: AVAudioPCMBuffer, using converter: AVAudioConverter) throws -> Data {
        let ratio = wireFormat.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 1024
        guard let output = AVAudioPCMBuffer(pcmFormat: wireFormat, frameCapacity: capacity) else {
            throw NodeClientError.protocolError("alloc pcm")
        }
        var didProvideInput = false
        var convertError: NSError?
        let status = converter.convert(to: output, error: &convertError) { _, outStatus in
            if didProvideInput {
                outStatus.pointee = .noDataNow
                return nil
            }
            didProvideInput = true
            outStatus.pointee = .haveData
            return buffer
        }
        if let convertError {
            throw convertError
        }
        if status == .error {
            throw NodeClientError.protocolError("convert pcm")
        }
        guard output.frameLength > 0, let channel = output.int16ChannelData else {
            return Data()
        }
        return Data(bytes: channel[0], count: Int(output.frameLength) * MemoryLayout<Int16>.size)
    }

    private static func readTokenFile(_ path: String) -> String? {
        let expanded = (path as NSString).expandingTildeInPath
        guard let attributes = try? FileManager.default.attributesOfItem(atPath: expanded),
              let permissions = attributes[.posixPermissions] as? NSNumber
        else {
            return nil
        }
        guard permissions.intValue & 0o777 == 0o600 else {
            return nil
        }
        guard let data = FileManager.default.contents(atPath: expanded),
              let text = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines),
              !text.isEmpty
        else {
            return nil
        }
        return text
    }

    private static func directSessionConfiguration(timeout: TimeInterval) -> URLSessionConfiguration {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = timeout
        configuration.connectionProxyDictionary = [:]
        return configuration
    }

    private static func infoURL(from wsURL: String) -> URL? {
        guard var components = URLComponents(string: wsURL) else {
            return nil
        }
        if components.scheme == "ws" {
            components.scheme = "http"
        } else if components.scheme == "wss" {
            components.scheme = "https"
        } else {
            return nil
        }
        components.path = "/v1/info"
        components.query = nil
        return components.url
    }

    private static func languageCode(_ localeID: String) -> String {
        Locale(identifier: localeID).language.languageCode?.identifier ?? localeID
    }

    private static func jsonString(_ object: [String: Any]) -> String {
        let data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
        return String(data: data ?? Data(), encoding: .utf8) ?? "{}"
    }

    private static func parseEvent(_ text: String) -> [String: Any]? {
        guard let data = text.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data),
              let event = object as? [String: Any] else {
            return nil
        }
        return event
    }

    private static func effectiveCode(_ closeCode: URLSessionWebSocketTask.CloseCode, _ reason: Data?) -> Int {
        guard closeCode.rawValue == 1005, let reason, reason.count >= 2 else {
            return closeCode.rawValue
        }
        return Int(reason[reason.startIndex]) << 8 | Int(reason[reason.startIndex + 1])
    }
}

final class OneShotGate: @unchecked Sendable {
    private let lock = NSLock()
    private var isOpen = false
    private var waiters: [CheckedContinuation<Void, Never>] = []

    var isOpened: Bool { lock.withLockValue { isOpen } }

    func open() {
        let pending = lock.withLockValue { () -> [CheckedContinuation<Void, Never>] in
            guard !isOpen else { return [] }
            isOpen = true
            let all = waiters
            waiters = []
            return all
        }
        pending.forEach { $0.resume() }
    }

    func wait() async {
        await withCheckedContinuation { continuation in
            let alreadyOpen = lock.withLockValue { () -> Bool in
                if isOpen { return true }
                waiters.append(continuation)
                return false
            }
            if alreadyOpen {
                continuation.resume()
            }
        }
    }
}

/// 「drained」等待者按（节点 + 会话代）登记：同一节点先后两个会话的 drain 不会互相误开。
final class NodeDrainWaiters: @unchecked Sendable {
    static let shared = NodeDrainWaiters()
    private let lock = NSLock()
    private var waiters: [UUID: (NodeSessionRef, OneShotGate)] = [:]

    func add(session: NodeSessionRef, gate: OneShotGate) -> UUID {
        let id = UUID()
        lock.withLockVoid { waiters[id] = (session, gate) }
        return id
    }

    func remove(_ id: UUID) {
        lock.withLockVoid { waiters.removeValue(forKey: id) }
    }

    func open(session: NodeSessionRef) {
        let targets = lock.withLockValue {
            waiters.values.compactMap { $0.0 == session ? $0.1 : nil }
        }
        targets.forEach { $0.open() }
    }
}

final class PongClock: @unchecked Sendable {
    private let lock = NSLock()
    private var at: Double
    init(start: Double) { at = start }
    var value: Double { lock.withLockValue { at } }
    func set(_ value: Double) { lock.withLockVoid { at = value } }
}

@available(macOS 27.0, *)
extension NodeClient {
    static func selfTest() -> [String] {
        var failures: [String] = []
        func check(_ ok: Bool, _ name: String) {
            if !ok { failures.append(name) }
        }
        // 心跳：10s 一发，发出后 10s 无 pong 判死
        check(HeartbeatPolicy.step(now: 5, lastPingSentAt: 0, lastPongAt: 0) == .none, "心跳：未到间隔不发 ping")
        check(HeartbeatPolicy.step(now: 10, lastPingSentAt: 0, lastPongAt: 0) == .sendPing, "心跳：到间隔发 ping")
        check(HeartbeatPolicy.step(now: 15, lastPingSentAt: 10, lastPongAt: 0) == .none, "心跳：ping 未回但未超时")
        check(HeartbeatPolicy.step(now: 20, lastPingSentAt: 10, lastPongAt: 0) == .dead, "心跳：ping 10s 无 pong 判故障")
        check(HeartbeatPolicy.step(now: 15, lastPingSentAt: 10, lastPongAt: 12) == .none, "心跳：已回 pong 不算故障")
        check(HeartbeatPolicy.step(now: 20, lastPingSentAt: 10, lastPongAt: 12) == .sendPing, "心跳：回 pong 后到期再发 ping，不误判 dead")
        // 断档补零
        check(gapPadSamples(offset: 100, sentSamples: 16_000, audioStart: 101) == 0, "时钟连续不补零")
        check(gapPadSamples(offset: 100, sentSamples: 16_000, audioStart: 101.05) == 0, "容差内不补零")
        check(gapPadSamples(offset: 100, sentSamples: 16_000, audioStart: 107) == 96_000, "丢 6s 帧纯函数仍算 96000 样本")
        check(boundedGapPadSamples(offset: 100, sentSamples: 16_000, audioStart: 104) == 48_000, "R4-2：阈值内（3s）照旧补零")
        check(boundedGapPadSamples(offset: 100, sentSamples: 16_000, audioStart: 106) == 80_000, "R4-2：恰好 5s 仍补零")
        check(boundedGapPadSamples(offset: 100, sentSamples: 16_000, audioStart: 107) == nil, "R4-2：6s 超阈值不补零（结束会话）")
        check(boundedGapPadSamples(offset: 100, sentSamples: 16_000, audioStart: 1900) == nil, "R4-2：30 分钟断档不补零")
        check(boundedGapPadSamples(offset: 100, sentSamples: 16_000, audioStart: 101) == 0, "R4-2：连续仍为 0")
        check(gapPadSamples(offset: 100, sentSamples: 16_000, audioStart: 100.5) == 0, "时钟重叠不补零")
        // drained 按节点+会话代匹配
        let waiters = NodeDrainWaiters()
        let oldGate = OneShotGate()
        let newGate = OneShotGate()
        _ = waiters.add(session: NodeSessionRef(nodeID: "A", generation: 1), gate: oldGate)
        _ = waiters.add(session: NodeSessionRef(nodeID: "A", generation: 2), gate: newGate)
        waiters.open(session: NodeSessionRef(nodeID: "A", generation: 1))
        check(oldGate.isOpened && !newGate.isOpened, "旧会话 drained 不应放行同节点新会话的等待者")
        waiters.open(session: NodeSessionRef(nodeID: "B", generation: 2))
        check(!newGate.isOpened, "其他节点 drained 不应放行")
        check(UDSWebSocketTask.webSocketAccept(key: "dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
              "UDS WebSocket Accept 应符合 RFC6455 示例向量")
        do {
            let small = try UDSWebSocketTask.makeClientFrame(opcode: 0x2, payload: Data([1, 2, 3]), maskKey: [1, 2, 3, 4])
            check(small.count == 9 && small[1] == 0x83, "UDS WebSocket 客户端帧必须掩码")
            let clientClose = try UDSWebSocketTask.makeClientCloseFrameForSelfTest(
                closeCode: .normalClosure,
                reason: Data("ok".utf8),
                maskKey: [0, 0, 0, 0]
            )
            check(clientClose.elementsEqual([0x88, 0x84, 0, 0, 0, 0, 0x03, 0xE8, 0x6F, 0x6B]),
                  "UDS WebSocket cancel close 帧应为 masked opcode=0x8 + 大端 close code + reason")
            let len126 = UDSWebSocketTask.makeServerFrameForSelfTest(opcode: 0x2, payload: Data(count: 126))
            check(len126[1] == 126 && len126[2] == 0 && len126[3] == 126, "UDS WebSocket 126 长度编码")
            let len127 = UDSWebSocketTask.makeServerFrameForSelfTest(opcode: 0x2, payload: Data(count: 65_536))
            check(len127[1] == 127 && len127[7] == 1 && len127[8] == 0 && len127[9] == 0, "UDS WebSocket 127 长度编码")
            if case .data(let payload) = try UDSWebSocketTask.decodeServerMessagesForSelfTest(len127).first {
                check(payload.count == 65_536, "UDS WebSocket 单帧 64KB 应允许")
            } else {
                check(false, "UDS WebSocket 单帧 64KB 应解码为 data")
            }
            let tooLarge = UDSWebSocketTask.makeServerFrameForSelfTest(opcode: 0x2, payload: Data(count: 65_537))
            var rejectedLargeFrame = false
            do {
                _ = try UDSWebSocketTask.decodeServerMessagesForSelfTest(tooLarge)
            } catch NodeClientError.protocolError {
                rejectedLargeFrame = true
            }
            check(rejectedLargeFrame, "UDS WebSocket 单帧超过 64KB 应协议错误")
            let frag = UDSWebSocketTask.makeServerFrameForSelfTest(fin: false, opcode: 0x1, payload: Data("hel".utf8))
                + UDSWebSocketTask.makeServerFrameForSelfTest(fin: true, opcode: 0x0, payload: Data("lo".utf8))
            if case .string(let text) = try UDSWebSocketTask.decodeServerMessagesForSelfTest(frag).first {
                check(text == "hello", "UDS WebSocket 分片文本应拼接")
            } else {
                check(false, "UDS WebSocket 分片文本应解码为 string")
            }
            let oversizedFragments = UDSWebSocketTask.makeServerFrameForSelfTest(fin: false, opcode: 0x2, payload: Data(count: 32_768))
                + UDSWebSocketTask.makeServerFrameForSelfTest(fin: true, opcode: 0x0, payload: Data(count: 32_769))
            var rejectedFragmentTotal = false
            do {
                _ = try UDSWebSocketTask.decodeServerMessagesForSelfTest(oversizedFragments)
            } catch NodeClientError.protocolError {
                rejectedFragmentTotal = true
            }
            check(rejectedFragmentTotal, "UDS WebSocket 分片重组超过 64KB 应协议错误")
            let close = UDSWebSocketTask.makeServerFrameForSelfTest(opcode: 0x8, payload: Data([0x03, 0xF1]))
            var closeCodeSeen = false
            do {
                _ = try UDSWebSocketTask.decodeServerMessagesForSelfTest(close)
            } catch NodeClientError.connection(let message) {
                closeCodeSeen = message == "close 1009"
            }
            check(closeCodeSeen, "UDS WebSocket close 码应解析")
            let busyClose = UDSWebSocketTask.makeServerFrameForSelfTest(opcode: 0x8, payload: Data([0x03, 0xF5]))
            let busyResult = UDSWebSocketTask.receiveOneForSelfTest(busyClose)
            check(busyResult.closeCode == 1013, "UDS WebSocket 线上 receiveBlocking 应记录 1013 close 码")
            if let error = busyResult.error, case NodeClientError.busy = error {
                check(true, "UDS WebSocket 线上 receiveBlocking 1013 应分类为 busy")
            } else {
                check(false, "UDS WebSocket 线上 receiveBlocking 1013 应分类为 busy")
            }
            let close1009Result = UDSWebSocketTask.receiveOneForSelfTest(close)
            if let error = close1009Result.error, case NodeClientError.connection(let message) = error {
                check(message == "close 1009", "UDS WebSocket 线上 receiveBlocking 1009 应保留 close 1009")
            } else {
                check(false, "UDS WebSocket 线上 receiveBlocking 1009 应保留 close 1009")
            }
            check(UDSWebSocketTask.cancelPreservesRemoteCloseCodeForSelfTest(),
                  "UDS WebSocket cancel 不应覆盖已记录的对端 close 码")
        } catch {
            check(false, "UDS WebSocket 帧自检异常：\(error)")
        }
        check(Self.directSessionConfiguration(timeout: 1).connectionProxyDictionary?.isEmpty == true,
              "NodeClient URLSession 应显式直连不走系统代理")
        return failures
    }

    func udsPathForSelfTest() -> String? {
        udsPath
    }
}

extension NSLock {
    func withLockValue<T>(_ body: () -> T) -> T {
        lock()
        defer { unlock() }
        return body()
    }

    func withLockVoid(_ body: () -> Void) {
        lock()
        defer { unlock() }
        body()
    }
}

private extension Duration {
    var secondsValue: Double {
        Double(components.seconds) + Double(components.attoseconds) / 1e18
    }
}

protocol NodeWebSocketTask: Sendable {
    var nodeCloseCode: Int { get }
    func resume()
    func send(_ message: URLSessionWebSocketTask.Message) async throws
    func receive() async throws -> URLSessionWebSocketTask.Message
    func sendPing(pongReceiveHandler: @escaping @Sendable (Error?) -> Void)
    func cancel(with closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?)
}

extension URLSessionWebSocketTask: NodeWebSocketTask {
    var nodeCloseCode: Int { closeCode.rawValue }
}
