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

/// v2 节点会话客户端。不自动重连、不做 TailnetPin（以后做）。
/// 日志/打印绝不输出 token 与字幕正文——本类不做任何日志输出。
final class NodeSession: NSObject, URLSessionWebSocketDelegate {
    private let url: URL
    private let token: String
    private let src: String
    private let dst: String
    private let onEvent: (NodeEvent) -> Void
    private let onClose: (Int) -> Void

    private let lock = NSLock()
    private var session: URLSession?
    private var socket: URLSessionWebSocketTask?
    private var closeNotified = false

    init(
        url: URL,
        token: String,
        src: String,
        dst: String,
        onEvent: @escaping (NodeEvent) -> Void,
        onClose: @escaping (Int) -> Void
    ) {
        self.url = url
        self.token = token
        self.src = src
        self.dst = dst
        self.onEvent = onEvent
        self.onClose = onClose
    }

    func start() {
        guard let hello = NodeHello(src: src, dst: dst) else {
            onClose(1008)
            return
        }
        var request = URLRequest(url: url)
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        let configuration = URLSessionConfiguration.ephemeral
        let session = URLSession(configuration: configuration, delegate: self, delegateQueue: nil)
        self.session = session
        let socket = session.webSocketTask(with: request)
        self.socket = socket
        socket.resume()
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
        socket?.cancel(with: .normalClosure, reason: nil)
        session?.invalidateAndCancel()
    }

    private func receiveLoop() {
        guard let socket else { return }
        socket.receive { [weak self] result in
            switch result {
            case .success(let message):
                if case .string(let text) = message, let event = NodeEvent.parse(text) {
                    self?.onEvent(event)
                }
                self?.receiveLoop()
            case .failure:
                self?.notifyClosed(1006)
            }
        }
    }

    private func notifyClosed(_ code: Int) {
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
