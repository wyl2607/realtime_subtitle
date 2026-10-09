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

struct NodeCallbacks: Sendable {
    var onReady: @Sendable (NodeInfo) -> Void = { _ in }
    var onFinal: @Sendable (String, Int, String, Double?, Double?) -> Void = { _, _, _, _, _ in }
    var onTranslation: @Sendable (String, Int, String) -> Void = { _, _, _ in }
    var onStatus: @Sendable (String) -> Void = { _ in }
    var onClosed: @Sendable (Error?) -> Void = { _ in }
}

/// v2 节点会话客户端。token 只从 0600 文件读，只放 Authorization 头，不进 URL/argv/日志。
@available(macOS 27.0, *)
final class NodeClient: NSObject, URLSessionWebSocketDelegate, URLSessionTaskDelegate, @unchecked Sendable {
    private static let wireFormat = AVAudioFormat(
        commonFormat: .pcmFormatInt16, sampleRate: 16_000, channels: 1, interleaved: true
    )!
    private static let frameBytes = 3200
    private static let readyTimeout: Duration = .seconds(5)
    private static let closeTimeout: Duration = .seconds(2)

    private let config: NodeConfig
    private let token: String
    private let info: NodeInfo
    private let sourceLocaleID: String
    private let targetLanguageID: String
    private let offsetSeconds: Double
    private let callbacks: NodeCallbacks
    private let lock = NSLock()

    private var socket: URLSessionWebSocketTask?
    private var session: URLSession?
    private var converter: AVAudioConverter?
    private var isClosed = false
    private var remoteCloseCode: Int?
    private let closeFrameReceived = OneShotGate()
    private let finished = OneShotGate()

    init(
        config: NodeConfig,
        token: String,
        info: NodeInfo,
        sourceLocaleID: String,
        targetLanguageID: String,
        offsetSeconds: Double,
        callbacks: NodeCallbacks
    ) {
        self.config = config
        self.token = token
        self.info = info
        self.sourceLocaleID = sourceLocaleID
        self.targetLanguageID = targetLanguageID
        self.offsetSeconds = offsetSeconds
        self.callbacks = callbacks
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
        let (data, response) = try await URLSession.shared.data(for: request)
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
        guard let url = URL(string: config.url) else {
            throw NodeClientError.badURL(config.url)
        }
        var request = URLRequest(url: url)
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        let sessionConfiguration = URLSessionConfiguration.ephemeral
        sessionConfiguration.timeoutIntervalForRequest = 24 * 60 * 60
        let session = URLSession(configuration: sessionConfiguration, delegate: self, delegateQueue: nil)
        let socket = session.webSocketTask(with: request)
        lock.withLockVoid {
            self.session = session
            self.socket = socket
        }
        socket.resume()
        do {
            let hello: [String: Any] = [
                "type": "hello",
                "v": 2,
                "src": Self.languageCode(sourceLocaleID),
                "dst": Self.languageCode(targetLanguageID),
                "sample_rate": 16_000,
                "format": "s16le",
            ]
            try await socket.send(.string(Self.jsonString(hello)))
            try await awaitReady(socket)
            callbacks.onReady(info)
            Task { await receiveLoop(socket) }
        } catch {
            throw await classify(error, socket)
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
        guard !data.isEmpty else {
            return
        }
        var pending = data
        while !pending.isEmpty {
            let n = min(Self.frameBytes, pending.count)
            try await socket.send(.data(Data(pending.prefix(n))))
            pending.removeFirst(n)
        }
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
        let token = NodeDrainWaiters.shared.add(nodeID: config.id, gate: gate)
        let timer = Task {
            try? await Task.sleep(for: timeout)
            gate.open()
        }
        await gate.wait()
        timer.cancel()
        NodeDrainWaiters.shared.remove(token)
    }

    func close() async {
        let pair = lock.withLockValue { () -> (URLSessionWebSocketTask?, URLSession?)? in
            guard !isClosed else {
                return nil
            }
            isClosed = true
            return (socket, session)
        }
        guard let (socket, session) = pair else {
            return
        }
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

    private func awaitReady(_ socket: URLSessionWebSocketTask) async throws {
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
                throw await classify(error, socket)
            }
        }
    }

    private func receiveLoop(_ socket: URLSessionWebSocketTask) async {
        while true {
            do {
                let message = try await socket.receive()
                guard case .string(let text) = message else {
                    continue
                }
                handle(text)
            } catch {
                let closed = lock.withLockValue { isClosed }
                callbacks.onClosed(closed ? nil : error)
                return
            }
        }
    }

    private func handle(_ text: String) {
        guard let event = Self.parseEvent(text), let ev = event["ev"] as? String else {
            return
        }
        switch ev {
        case "final":
            guard let id = event["id"] as? Int, let body = event["text"] as? String else { return }
            let t0 = (event["a0"] as? Double).map { $0 + offsetSeconds }
            let t1 = (event["a1"] as? Double).map { $0 + offsetSeconds }
            callbacks.onFinal(config.id, id, body, t0, t1)
        case "translation":
            guard let id = event["id"] as? Int, let body = event["text"] as? String else { return }
            callbacks.onTranslation(config.id, id, body)
        case "status":
            if let code = event["code"] as? String {
                callbacks.onStatus("节点 \(config.id)：\(code)")
            }
        case "drained":
            NodeDrainWaiters.shared.open(nodeID: config.id)
        default:
            return
        }
    }

    private func classify(_ error: Error, _ socket: URLSessionWebSocketTask) async -> NodeClientError {
        if let e = error as? NodeClientError {
            return e
        }
        if let http = socket.response as? HTTPURLResponse {
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
        if code == 1013 || socket.closeCode.rawValue == 1013 {
            return .busy
        }
        if let code, code != 1005 {
            return .connection("close \(code)")
        }
        if socket.closeCode.rawValue != 1005 {
            return .connection("close \(socket.closeCode.rawValue)")
        }
        return .connection(error.localizedDescription)
    }

    private func currentSocket() -> URLSessionWebSocketTask? {
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

private final class NodeDrainWaiters: @unchecked Sendable {
    static let shared = NodeDrainWaiters()
    private let lock = NSLock()
    private var waiters: [UUID: (String, OneShotGate)] = [:]

    func add(nodeID: String, gate: OneShotGate) -> UUID {
        let id = UUID()
        lock.withLockVoid { waiters[id] = (nodeID, gate) }
        return id
    }

    func remove(_ id: UUID) {
        lock.withLockVoid { waiters.removeValue(forKey: id) }
    }

    func open(nodeID: String) {
        let targets = lock.withLockValue {
            waiters.values.compactMap { $0.0 == nodeID ? $0.1 : nil }
        }
        targets.forEach { $0.open() }
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
