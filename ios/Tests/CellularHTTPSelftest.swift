import Foundation

/// Wi-Fi 失败换蜂窝重发：判定规则 + 手写 HTTP/1.1 的序列化与解析。不发网络。
func runCellularHTTPSelftest(_ check: @escaping (Bool, String) -> Void) {
    let both = NetworkPathSnapshot(wifi: true, cellular: true)
    check(CellularFallback.shouldRetry(after: .network("timeout"), paths: both, systemProxyConfigured: false),
          "Wi-Fi 和蜂窝都在、Wi-Fi 超时：应换蜂窝重发")
    check(!CellularFallback.shouldRetry(after: .network("timeout"), paths: both, systemProxyConfigured: true),
          "开了系统代理：不许直连蜂窝绕过代理")
    check(!CellularFallback.shouldRetry(after: .network("timeout"), paths: NetworkPathSnapshot(wifi: false, cellular: true), systemProxyConfigured: false),
          "没 Wi-Fi 时原请求就走蜂窝，不重发")
    check(!CellularFallback.shouldRetry(after: .network("timeout"), paths: NetworkPathSnapshot(wifi: true, cellular: false), systemProxyConfigured: false),
          "蜂窝不可用时不重发")
    check(!CellularFallback.shouldRetry(after: .httpStatus(429, retryAfter: nil, xGroqID: nil), paths: both, systemProxyConfigured: false),
          "HTTP 错误是 Groq 的回答，换网卡没用")
    check(!CellularFallback.shouldRetry(after: .network("cancelled"), paths: both, systemProxyConfigured: false),
          "取消不重发")
    check(!CellularFallback.shouldRetry(after: .cancelled, paths: both, systemProxyConfigured: false),
          "取消不重发（cancelled 错误）")

    do {
        let request = CloudClient.makeRequest(key: "gsk_fake", pcm16Mono16k: Data([0, 0]), language: "de", endpoint: CloudClient.endpoint)
        let wire = CellularHTTPExchange.serialize(request, host: "api.groq.com", body: request.httpBody!)
        let text = String(decoding: wire.prefix(400), as: UTF8.self)
        check(text.hasPrefix("POST /openai/v1/audio/transcriptions HTTP/1.1\r\nHost: api.groq.com\r\n"), "请求行与 Host 头")
        check(text.contains("Authorization: Bearer gsk_fake\r\n"), "key 只在 Authorization 头")
        check(text.contains("Content-Length: \(request.httpBody!.count)\r\n"), "Content-Length 等于 body 字节数")
        check(text.contains("Connection: close\r\n"), "单次请求不复用连接")
        check(wire.suffix(request.httpBody!.count) == request.httpBody!, "body 原样追加在头后")
    }

    do {
        let raw = Data("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nX-RateLimit-Remaining-Requests: 42\r\nContent-Length: 5\r\n\r\nhello".utf8)
        if case .complete(let reply) = HTTPResponseParser.parse(raw, atEOF: false) {
            check(reply.statusCode == 200 && reply.body == Data("hello".utf8), "Content-Length 定界")
            check(reply.headers["x-ratelimit-remaining-requests"] == "42", "头名转小写")
            check(QuotaLedger.remainingRequests(from: reply.headers) == 42, "额度头仍能被账本读到")
        } else {
            check(false, "Content-Length 响应应解析完成")
        }
        check(HTTPResponseParser.parse(raw.prefix(raw.count - 2), atEOF: false) == .incomplete, "body 没收完时等更多数据")
        check(HTTPResponseParser.parse(raw.prefix(raw.count - 2), atEOF: true) == .invalid, "连接关了 body 还不够：判坏")
    }

    do {
        let raw = Data("HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n6;ext=1\r\n world\r\n0\r\nX-Trailer: yes\r\n\r\n".utf8)
        if case .complete(let reply) = HTTPResponseParser.parse(raw, atEOF: false) {
            check(reply.body == Data("hello world".utf8), "chunked 拼接，忽略分块扩展")
        } else {
            check(false, "chunked 响应应解析完成")
        }
        check(HTTPResponseParser.parse(raw.prefix(30), atEOF: false) == .incomplete, "chunked 没收完时等")

        let oversized = Data("HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n7FFFFFFFFFFFFFFF\r\n".utf8)
        check(HTTPResponseParser.parse(oversized, atEOF: false) == .incomplete,
              "超大 chunk size 应安全返回 incomplete，不得整数溢出崩溃")
        check(HTTPResponseParser.parse(oversized, atEOF: true) == .invalid,
              "超大 chunk size EOF 时应判 invalid")

        let missingChunkCRLF = Data("HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhelloXX0\r\n\r\n".utf8)
        check(HTTPResponseParser.parse(missingChunkCRLF, atEOF: true) == .invalid,
              "chunk 数据后的 CRLF 缺失应判 invalid")

        let truncatedTerminator = Data("HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n".utf8)
        check(HTTPResponseParser.parse(truncatedTerminator, atEOF: false) == .incomplete,
              "终止块 trailer 未收完时应等待")
        check(HTTPResponseParser.parse(truncatedTerminator, atEOF: true) == .invalid,
              "截断终止块 EOF 时应判 invalid")
    }

    do {
        let raw = Data("HTTP/1.1 503 Service Unavailable\r\nRetry-After: 3\r\n\r\nbusy".utf8)
        check(HTTPResponseParser.parse(raw, atEOF: false) == .incomplete, "无长度时读到关闭为止")
        if case .complete(let reply) = HTTPResponseParser.parse(raw, atEOF: true) {
            check(reply.statusCode == 503 && reply.body == Data("busy".utf8), "关闭定界的 body")
        } else {
            check(false, "EOF 时应完成")
        }
        check(HTTPResponseParser.parse(Data("garbage\r\n\r\n".utf8), atEOF: false) == .invalid, "坏状态行判坏")
    }
}
