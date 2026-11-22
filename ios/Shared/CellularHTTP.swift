import Foundation
import Network

/// 指定走蜂窝发一次 HTTPS 请求。
///
/// URLSession 选不了网卡：它只能禁止蜂窝，不能禁止 Wi-Fi。Wi-Fi 连着但卡死时，
/// 想换 5G 重发同一段只能在 NWConnection 上（requiredInterfaceType = .cellular）写一个最小 HTTP/1.1。
/// 只服务 CloudClient 的单次 POST：Connection: close、不复用连接、不跟随重定向。
final class CellularHTTPExchange: @unchecked Sendable {
    typealias Completion = @Sendable (Result<HTTPReply, CloudClientError>) -> Void

    private static let maxResponseBytes = 1 << 20
    private let connection: NWConnection
    private let payload: Data
    private let timeout: TimeInterval
    private let complete: Completion
    private let queue = DispatchQueue(label: "rs.cellular-http")
    // 下面两个只在 queue 上读写。
    private var buffer = Data()
    private var done = false

    init?(request: URLRequest, timeout: TimeInterval, complete: @escaping Completion) {
        guard let url = request.url, url.scheme == "https", let host = url.host(), let body = request.httpBody else {
            return nil
        }
        let tls = NWProtocolTLS.Options()
        sec_protocol_options_add_tls_application_protocol(tls.securityProtocolOptions, "http/1.1")
        let parameters = NWParameters(tls: tls, tcp: NWProtocolTCP.Options())
        parameters.requiredInterfaceType = .cellular
        connection = NWConnection(
            host: NWEndpoint.Host(host),
            port: NWEndpoint.Port(rawValue: UInt16(url.port ?? 443)) ?? .https,
            using: parameters
        )
        payload = Self.serialize(request, host: host, body: body)
        self.timeout = timeout
        self.complete = complete
    }

    func start() {
        connection.stateUpdateHandler = { [self] state in
            switch state {
            case .ready: send()
            // waiting = 蜂窝此刻没路：不等它恢复，这一段直接算失败。
            case .waiting: finish(.failure(.network("cellular_unavailable")))
            case .failed: finish(.failure(.network("cellular_failed")))
            case .cancelled: finish(.failure(.cancelled))
            default: break
            }
        }
        connection.start(queue: queue)
        queue.asyncAfter(deadline: .now() + timeout) { [self] in finish(.failure(.network("timeout"))) }
    }

    func cancel() {
        queue.async { [self] in finish(.failure(.cancelled)) }
    }

    private func send() {
        connection.send(content: payload, completion: .contentProcessed { [self] error in
            if error != nil {
                finish(.failure(.network("cellular_send")))
            } else {
                receive()
            }
        })
    }

    private func receive() {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 64 * 1024) { [self] data, _, isComplete, error in
            if let data { buffer.append(data) }
            guard buffer.count <= Self.maxResponseBytes else {
                finish(.failure(.invalidResponse))
                return
            }
            switch HTTPResponseParser.parse(buffer, atEOF: isComplete || error != nil) {
            case .complete(let reply): finish(.success(reply))
            case .invalid: finish(.failure(error == nil ? .invalidResponse : .network("cellular_receive")))
            case .incomplete: receive()
            }
        }
    }

    private func finish(_ result: Result<HTTPReply, CloudClientError>) {
        guard !done else { return }
        done = true
        // 清掉回调再 cancel：handler 强引用 self，不清就是循环引用；cancel 触发的 .cancelled 也不会再进来。
        connection.stateUpdateHandler = nil
        connection.cancel()
        complete(result)
    }

    static func serialize(_ request: URLRequest, host: String, body: Data) -> Data {
        let url = request.url!
        var target = url.path(percentEncoded: true)
        if target.isEmpty { target = "/" }
        if let query = url.query(percentEncoded: true) { target += "?\(query)" }
        var head = "\(request.httpMethod ?? "POST") \(target) HTTP/1.1\r\nHost: \(host)\r\n"
        for (name, value) in (request.allHTTPHeaderFields ?? [:]).sorted(by: { $0.key < $1.key }) {
            head += "\(name): \(value)\r\n"
        }
        head += "Content-Length: \(body.count)\r\nConnection: close\r\nAccept-Encoding: identity\r\n\r\n"
        return Data(head.utf8) + body
    }
}

struct HTTPReply: Sendable, Equatable {
    var statusCode: Int
    /// 键一律小写；CloudClient.parse 按不区分大小写查。
    var headers: [String: String]
    var body: Data
}

/// HTTP/1.1 响应解析：支持 Content-Length、chunked 和「读到连接关闭为止」三种定界。
enum HTTPResponseParser {
    enum Outcome: Equatable {
        case incomplete
        case invalid
        case complete(HTTPReply)
    }

    private static let crlf = Data("\r\n".utf8)

    static func parse(_ data: Data, atEOF: Bool) -> Outcome {
        guard let headerEnd = data.range(of: Data("\r\n\r\n".utf8)) else {
            return atEOF ? .invalid : .incomplete
        }
        guard let head = String(data: data[data.startIndex..<headerEnd.lowerBound], encoding: .utf8) else {
            return .invalid
        }
        var lines = head.components(separatedBy: "\r\n")
        let statusParts = lines.removeFirst().split(separator: " ")
        guard statusParts.count >= 2, statusParts[0].hasPrefix("HTTP/1."), let status = Int(statusParts[1]) else {
            return .invalid
        }
        var headers: [String: String] = [:]
        for line in lines {
            guard let colon = line.firstIndex(of: ":") else { continue }
            let name = line[..<colon].trimmingCharacters(in: .whitespaces).lowercased()
            headers[name] = line[line.index(after: colon)...].trimmingCharacters(in: .whitespaces)
        }
        let rest = Data(data[headerEnd.upperBound...])

        if headers["transfer-encoding"]?.lowercased().contains("chunked") == true {
            if let body = decodeChunked(rest) {
                return .complete(HTTPReply(statusCode: status, headers: headers, body: body))
            }
            return atEOF ? .invalid : .incomplete
        }
        if let lengthText = headers["content-length"] {
            guard let length = Int(lengthText), length >= 0 else { return .invalid }
            if rest.count >= length {
                return .complete(HTTPReply(statusCode: status, headers: headers, body: rest.prefix(length)))
            }
            return atEOF ? .invalid : .incomplete
        }
        return atEOF ? .complete(HTTPReply(statusCode: status, headers: headers, body: rest)) : .incomplete
    }

    /// nil = 还没收完（或格式坏了，由调用方在 EOF 时判 invalid）。
    static func decodeChunked(_ data: Data) -> Data? {
        var body = Data()
        var index = data.startIndex
        while true {
            guard let lineEnd = data[index...].range(of: crlf),
                  let sizeLine = String(data: data[index..<lineEnd.lowerBound], encoding: .ascii),
                  let sizeText = sizeLine.split(separator: ";", omittingEmptySubsequences: false).first,
                  let size = Int(sizeText.trimmingCharacters(in: .whitespaces), radix: 16),
                  size >= 0 else {
                return nil
            }
            index = lineEnd.upperBound
            if size == 0 { return body }
            guard data.distance(from: index, to: data.endIndex) >= size + 2 else { return nil }
            let chunkEnd = data.index(index, offsetBy: size)
            body.append(data[index..<chunkEnd])
            index = data.index(chunkEnd, offsetBy: 2)
        }
    }
}
