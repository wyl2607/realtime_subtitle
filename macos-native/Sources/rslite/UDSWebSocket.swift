import CryptoKit
import Darwin
import Foundation

@available(macOS 27.0, *)
final class UDSWebSocketTask: @unchecked Sendable, NodeWebSocketTask {
    private static let guid = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    private static let defaultTimeoutMS: Int32 = 5_000
    private static let maxHTTPHeaderBytes = 64 * 1024

    private let stateLock = NSLock()
    private let writeLock = NSLock()
    private var fd: Int32
    private var isCancelled = false
    private var closeCodeValue = 1005
    private var pongHandlers: [(Error?) -> Void] = []
    private var continuationOpcode: UInt8?
    private var continuationPayload = Data()

    var nodeCloseCode: Int { stateLock.withLockValue { closeCodeValue } }

    init(path: String, timeoutMS: Int32 = defaultTimeoutMS) throws {
        fd = try Self.openValidatedSocket(path: path, timeoutMS: timeoutMS)
        do {
            try performUpgrade(timeoutMS: timeoutMS)
        } catch {
            Darwin.close(fd)
            fd = -1
            throw error
        }
    }

    deinit {
        let closeFD = stateLock.withLockValue { () -> Int32 in
            let value = fd
            fd = -1
            return value
        }
        if closeFD >= 0 {
            Darwin.close(closeFD)
        }
    }

    static func fetchInfo(path: String, timeoutMS: Int32 = defaultTimeoutMS) async throws -> NodeInfo {
        try await withCheckedThrowingContinuation { continuation in
            DispatchQueue.global(qos: .utility).async {
                do {
                    let fd = try openValidatedSocket(path: path, timeoutMS: timeoutMS)
                    defer { Darwin.close(fd) }
                    let request = Data("GET /v1/info HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".utf8)
                    try writeAll(fd: fd, data: request, timeoutMS: timeoutMS)
                    let response = try readHTTPResponse(fd: fd, timeoutMS: timeoutMS)
                    guard response.status == 200 else {
                        if response.status == 403 {
                            throw NodeClientError.unauthorized
                        }
                        throw NodeClientError.connection("info http \(response.status)")
                    }
                    let info = try JSONDecoder().decode(NodeInfo.self, from: response.body)
                    guard info.v == 2 else {
                        throw NodeClientError.protocolError("info v=\(info.v)")
                    }
                    continuation.resume(returning: info)
                } catch {
                    continuation.resume(throwing: error)
                }
            }
        }
    }

    func resume() {}

    func send(_ message: URLSessionWebSocketTask.Message) async throws {
        try Task.checkCancellation()
        switch message {
        case .string(let text):
            try sendFrame(opcode: 0x1, payload: Data(text.utf8))
        case .data(let data):
            try sendFrame(opcode: 0x2, payload: data)
        @unknown default:
            throw NodeClientError.protocolError("unsupported websocket message")
        }
    }

    func receive() async throws -> URLSessionWebSocketTask.Message {
        try await withCheckedThrowingContinuation { continuation in
            DispatchQueue.global(qos: .utility).async {
                do {
                    continuation.resume(returning: try self.receiveBlocking())
                } catch {
                    continuation.resume(throwing: error)
                }
            }
        }
    }

    func sendPing(pongReceiveHandler: @escaping @Sendable (Error?) -> Void) {
        stateLock.withLockVoid { pongHandlers.append(pongReceiveHandler) }
        do {
            try sendFrame(opcode: 0x9, payload: Data())
        } catch {
            let handlers = stateLock.withLockValue {
                let all = pongHandlers
                pongHandlers.removeAll()
                return all
            }
            handlers.forEach { $0(error) }
        }
    }

    func cancel(with closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?) {
        let closeFD = stateLock.withLockValue { () -> Int32 in
            guard !isCancelled else { return -1 }
            isCancelled = true
            closeCodeValue = closeCode.rawValue
            return fd
        }
        if closeFD >= 0 {
            _ = Darwin.shutdown(closeFD, SHUT_RDWR)
        }
        let handlers = stateLock.withLockValue {
            let all = pongHandlers
            pongHandlers.removeAll()
            return all
        }
        handlers.forEach { $0(URLError(.cancelled)) }
    }

    static func webSocketAccept(key: String) -> String {
        let digest = Insecure.SHA1.hash(data: Data((key + guid).utf8))
        return Data(digest).base64EncodedString()
    }

    static func makeClientFrame(opcode: UInt8, payload: Data, maskKey: [UInt8]) throws -> Data {
        guard maskKey.count == 4 else {
            throw NodeClientError.protocolError("mask key length")
        }
        return makeFrame(fin: true, opcode: opcode, payload: payload, maskKey: maskKey)
    }

    static func makeServerFrameForSelfTest(opcode: UInt8, payload: Data) -> Data {
        makeFrame(fin: true, opcode: opcode, payload: payload, maskKey: nil)
    }

    static func makeServerFrameForSelfTest(fin: Bool, opcode: UInt8, payload: Data) -> Data {
        makeFrame(fin: fin, opcode: opcode, payload: payload, maskKey: nil)
    }

    static func decodeServerMessagesForSelfTest(_ data: Data) throws -> [URLSessionWebSocketTask.Message] {
        var offset = 0
        var continuationOpcode: UInt8?
        var continuationPayload = Data()
        var messages: [URLSessionWebSocketTask.Message] = []
        while offset < data.count {
            let frame = try parseFrame(data, offset: &offset)
            switch frame.opcode {
            case 0x1, 0x2:
                if frame.fin {
                    if frame.opcode == 0x1 {
                        messages.append(.string(String(data: frame.payload, encoding: .utf8) ?? ""))
                    } else {
                        messages.append(.data(frame.payload))
                    }
                } else {
                    continuationOpcode = frame.opcode
                    continuationPayload = frame.payload
                }
            case 0x0:
                guard let opcode = continuationOpcode else {
                    throw NodeClientError.protocolError("unexpected continuation")
                }
                continuationPayload.append(frame.payload)
                if frame.fin {
                    if opcode == 0x1 {
                        messages.append(.string(String(data: continuationPayload, encoding: .utf8) ?? ""))
                    } else {
                        messages.append(.data(continuationPayload))
                    }
                    continuationOpcode = nil
                    continuationPayload.removeAll(keepingCapacity: true)
                }
            case 0x8:
                let code = closeCode(from: frame.payload)
                throw NodeClientError.connection("close \(code)")
            case 0x9, 0xA:
                continue
            default:
                throw NodeClientError.protocolError("opcode \(frame.opcode)")
            }
        }
        return messages
    }

    static func closeCodeForSelfTest(_ payload: Data) -> Int {
        closeCode(from: payload)
    }

    private func performUpgrade(timeoutMS: Int32) throws {
        let keyBytes = (0..<16).map { _ in UInt8.random(in: 0...255) }
        let key = Data(keyBytes).base64EncodedString()
        var requestText = "GET /v2/session HTTP/1.1\r\n"
        requestText += "Host: localhost\r\n"
        requestText += "Upgrade: websocket\r\n"
        requestText += "Connection: Upgrade\r\n"
        requestText += "Sec-WebSocket-Key: \(key)\r\n"
        requestText += "Sec-WebSocket-Version: 13\r\n\r\n"
        let request = Data(requestText.utf8)
        try Self.writeAll(fd: fd, data: request, timeoutMS: timeoutMS)
        let response = try Self.readHTTPResponse(fd: fd, timeoutMS: timeoutMS, headersOnly: true)
        guard response.status == 101 else {
            throw NodeClientError.connection("http \(response.status)")
        }
        let accept = response.headers["sec-websocket-accept"] ?? ""
        guard accept == Self.webSocketAccept(key: key) else {
            throw NodeClientError.protocolError("bad websocket accept")
        }
    }

    private func sendFrame(opcode: UInt8, payload: Data) throws {
        let maskKey = (0..<4).map { _ in UInt8.random(in: 0...255) }
        let frame = Self.makeFrame(fin: true, opcode: opcode, payload: payload, maskKey: maskKey)
        writeLock.lock()
        defer { writeLock.unlock() }
            try Self.writeAll(fd: fd, data: frame, timeoutMS: Self.defaultTimeoutMS)
    }

    private func receiveBlocking() throws -> URLSessionWebSocketTask.Message {
        while true {
            let frame = try readFrame()
            switch frame.opcode {
            case 0x0:
                guard let opcode = continuationOpcode else {
                    throw NodeClientError.protocolError("unexpected continuation")
                }
                continuationPayload.append(frame.payload)
                if frame.fin {
                    let payload = continuationPayload
                    continuationOpcode = nil
                    continuationPayload.removeAll(keepingCapacity: true)
                    return try message(opcode: opcode, payload: payload)
                }
            case 0x1, 0x2:
                if frame.fin {
                    return try message(opcode: frame.opcode, payload: frame.payload)
                }
                continuationOpcode = frame.opcode
                continuationPayload = frame.payload
            case 0x8:
                let code = Self.closeCode(from: frame.payload)
                stateLock.withLockVoid { closeCodeValue = code }
                throw code == 1013 ? NodeClientError.busy : NodeClientError.connection("close \(code)")
            case 0x9:
                try sendFrame(opcode: 0xA, payload: frame.payload)
            case 0xA:
                let handlers = stateLock.withLockValue {
                    let all = pongHandlers
                    pongHandlers.removeAll()
                    return all
                }
                handlers.forEach { $0(nil) }
            default:
                throw NodeClientError.protocolError("opcode \(frame.opcode)")
            }
        }
    }

    private func message(opcode: UInt8, payload: Data) throws -> URLSessionWebSocketTask.Message {
        if opcode == 0x1 {
            return .string(String(data: payload, encoding: .utf8) ?? "")
        }
        if opcode == 0x2 {
            return .data(payload)
        }
        throw NodeClientError.protocolError("opcode \(opcode)")
    }

    private func readFrame() throws -> WebSocketFrame {
        var header = try Self.readExact(fd: fd, count: 2, timeoutMS: Self.defaultTimeoutMS)
        let fin = (header[0] & 0x80) != 0
        let opcode = header[0] & 0x0F
        let masked = (header[1] & 0x80) != 0
        var length = UInt64(header[1] & 0x7F)
        if length == 126 {
            header = try Self.readExact(fd: fd, count: 2, timeoutMS: Self.defaultTimeoutMS)
            length = (UInt64(header[0]) << 8) | UInt64(header[1])
        } else if length == 127 {
            header = try Self.readExact(fd: fd, count: 8, timeoutMS: Self.defaultTimeoutMS)
            length = header.reduce(UInt64(0)) { ($0 << 8) | UInt64($1) }
        }
        guard length <= UInt64(Int.max) else {
            throw NodeClientError.protocolError("frame too large")
        }
        let mask = masked ? try Self.readExact(fd: fd, count: 4, timeoutMS: Self.defaultTimeoutMS) : []
        var payload = try Self.readExact(fd: fd, count: Int(length), timeoutMS: Self.defaultTimeoutMS)
        if masked {
            for i in payload.indices {
                payload[i] ^= mask[i % 4]
            }
        }
        return WebSocketFrame(fin: fin, opcode: opcode, payload: Data(payload))
    }

    private static func makeFrame(fin: Bool, opcode: UInt8, payload: Data, maskKey: [UInt8]?) -> Data {
        var frame = Data()
        frame.append((fin ? 0x80 : 0x00) | (opcode & 0x0F))
        let masked: UInt8 = maskKey == nil ? 0 : 0x80
        let length = payload.count
        if length < 126 {
            frame.append(masked | UInt8(length))
        } else if length <= 0xFFFF {
            frame.append(masked | 126)
            frame.append(UInt8((length >> 8) & 0xFF))
            frame.append(UInt8(length & 0xFF))
        } else {
            frame.append(masked | 127)
            let value = UInt64(length)
            for shift in stride(from: 56, through: 0, by: -8) {
                frame.append(UInt8((value >> UInt64(shift)) & 0xFF))
            }
        }
        if let maskKey {
            frame.append(contentsOf: maskKey)
            var bytes = [UInt8](payload)
            for i in bytes.indices {
                bytes[i] ^= maskKey[i % 4]
            }
            frame.append(contentsOf: bytes)
        } else {
            frame.append(payload)
        }
        return frame
    }

    private static func parseFrame(_ data: Data, offset: inout Int) throws -> WebSocketFrame {
        guard offset + 2 <= data.count else { throw NodeClientError.protocolError("short frame") }
        let b0 = data[offset]
        let b1 = data[offset + 1]
        offset += 2
        let fin = (b0 & 0x80) != 0
        let opcode = b0 & 0x0F
        let masked = (b1 & 0x80) != 0
        var length = UInt64(b1 & 0x7F)
        if length == 126 {
            guard offset + 2 <= data.count else { throw NodeClientError.protocolError("short len126") }
            length = (UInt64(data[offset]) << 8) | UInt64(data[offset + 1])
            offset += 2
        } else if length == 127 {
            guard offset + 8 <= data.count else { throw NodeClientError.protocolError("short len127") }
            length = data[offset..<(offset + 8)].reduce(UInt64(0)) { ($0 << 8) | UInt64($1) }
            offset += 8
        }
        let mask: [UInt8]
        if masked {
            guard offset + 4 <= data.count else { throw NodeClientError.protocolError("short mask") }
            mask = Array(data[offset..<(offset + 4)])
            offset += 4
        } else {
            mask = []
        }
        guard length <= UInt64(Int.max), offset + Int(length) <= data.count else {
            throw NodeClientError.protocolError("short payload")
        }
        var payload = Array(data[offset..<(offset + Int(length))])
        offset += Int(length)
        if masked {
            for i in payload.indices {
                payload[i] ^= mask[i % 4]
            }
        }
        return WebSocketFrame(fin: fin, opcode: opcode, payload: Data(payload))
    }

    private static func closeCode(from payload: Data) -> Int {
        guard payload.count >= 2 else { return 1005 }
        return (Int(payload[payload.startIndex]) << 8) | Int(payload[payload.startIndex + 1])
    }

    private static func openValidatedSocket(path: String, timeoutMS: Int32) throws -> Int32 {
        var before = stat()
        guard lstat(path, &before) == 0 else {
            throw NodeClientError.connection("uds_lstat")
        }
        guard (before.st_mode & S_IFMT) == S_IFSOCK else {
            throw NodeClientError.connection("uds_not_socket")
        }
        guard before.st_uid == getuid() else {
            throw NodeClientError.unauthorized
        }

        let fd = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
        var noSigPipe: Int32 = 1
        _ = setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &noSigPipe, socklen_t(MemoryLayout<Int32>.size))
        do {
            var addr = try makeAddress(path)
            let addrLen = socklen_t(MemoryLayout<sockaddr_un>.size)
            let connected = withUnsafePointer(to: &addr) {
                $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                    connectRetry(fd: fd, addr: $0, len: addrLen)
                }
            }
            guard connected == 0 else {
                throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
            }
            var after = stat()
            guard fstat(fd, &after) == 0 else {
                throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
            }
            guard before.st_dev == after.st_dev, before.st_ino == after.st_ino else {
                throw NodeClientError.connection("uds_socket_changed")
            }
            return fd
        } catch {
            Darwin.close(fd)
            throw error
        }
    }

    private static func makeAddress(_ path: String) throws -> sockaddr_un {
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let capacity = MemoryLayout.size(ofValue: addr.sun_path)
        let copied = path.withCString { src in
            withUnsafeMutablePointer(to: &addr.sun_path) {
                $0.withMemoryRebound(to: CChar.self, capacity: capacity) {
                    strlcpy($0, src, capacity)
                }
            }
        }
        guard copied < capacity else {
            throw NodeClientError.badURL(path)
        }
        return addr
    }

    private static func connectRetry(fd: Int32, addr: UnsafePointer<sockaddr>, len: socklen_t) -> Int32 {
        while true {
            let rc = Darwin.connect(fd, addr, len)
            if rc == 0 || errno != EINTR {
                return rc
            }
        }
    }

    private static func readHTTPResponse(
        fd: Int32,
        timeoutMS: Int32,
        headersOnly: Bool = false
    ) throws -> (status: Int, headers: [String: String], body: Data) {
        var head = Data()
        while !head.suffix(4).elementsEqual([13, 10, 13, 10]) {
            let byte = try readExact(fd: fd, count: 1, timeoutMS: timeoutMS)
            head.append(byte[0])
            if head.count > maxHTTPHeaderBytes {
                throw NodeClientError.protocolError("http header too large")
            }
        }
        guard let text = String(data: head, encoding: .utf8) else {
            throw NodeClientError.protocolError("http header utf8")
        }
        let lines = text.split(separator: "\r\n", omittingEmptySubsequences: false)
        guard let statusLine = lines.first,
              let status = Int(statusLine.split(separator: " ").dropFirst().first ?? "") else {
            throw NodeClientError.protocolError("http status")
        }
        var headers: [String: String] = [:]
        for line in lines.dropFirst() where !line.isEmpty {
            guard let colon = line.firstIndex(of: ":") else { continue }
            let name = line[..<colon].trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            let value = line[line.index(after: colon)...].trimmingCharacters(in: .whitespacesAndNewlines)
            headers[name] = value
        }
        if headersOnly {
            return (status, headers, Data())
        }
        let body: Data
        if let lengthText = headers["content-length"], let length = Int(lengthText), length >= 0 {
            body = Data(try readExact(fd: fd, count: length, timeoutMS: timeoutMS))
        } else {
            var bytes = Data()
            while true {
                do {
                    let chunk = try readSome(fd: fd, maxCount: 4096, timeoutMS: timeoutMS)
                    if chunk.isEmpty { break }
                    bytes.append(chunk)
                } catch NodeClientError.timeout {
                    break
                }
            }
            body = bytes
        }
        return (status, headers, body)
    }

    private static func readExact(fd: Int32, count: Int, timeoutMS: Int32) throws -> [UInt8] {
        guard count > 0 else { return [] }
        var bytes = [UInt8](repeating: 0, count: count)
        var offset = 0
        while offset < count {
            try wait(fd: fd, events: Int16(POLLIN), timeoutMS: timeoutMS)
            let n = bytes.withUnsafeMutableBytes {
                Darwin.read(fd, $0.baseAddress!.advanced(by: offset), count - offset)
            }
            if n < 0 {
                if errno == EINTR { continue }
                throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
            }
            if n == 0 {
                throw NodeClientError.connection("eof")
            }
            offset += n
        }
        return bytes
    }

    private static func readSome(fd: Int32, maxCount: Int, timeoutMS: Int32) throws -> Data {
        try wait(fd: fd, events: Int16(POLLIN), timeoutMS: timeoutMS)
        var bytes = [UInt8](repeating: 0, count: maxCount)
        while true {
            let n = Darwin.read(fd, &bytes, maxCount)
            if n < 0 {
                if errno == EINTR { continue }
                throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
            }
            if n == 0 { return Data() }
            return Data(bytes.prefix(n))
        }
    }

    private static func writeAll(fd: Int32, data: Data, timeoutMS: Int32) throws {
        try data.withUnsafeBytes { raw in
            guard let base = raw.baseAddress else { return }
            var offset = 0
            while offset < raw.count {
                try wait(fd: fd, events: Int16(POLLOUT), timeoutMS: timeoutMS)
                let n = Darwin.write(fd, base.advanced(by: offset), raw.count - offset)
                if n < 0 {
                    if errno == EINTR { continue }
                    throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
                }
                if n == 0 {
                    throw POSIXError(.EIO)
                }
                offset += n
            }
        }
    }

    private static func wait(fd: Int32, events: Int16, timeoutMS: Int32) throws {
        var pfd = pollfd(fd: fd, events: events, revents: 0)
        while true {
            let rc = poll(&pfd, 1, timeoutMS)
            if rc > 0 {
                if (pfd.revents & events) != 0 {
                    return
                }
                let errorEvents = Int16(POLLERR | POLLHUP | POLLNVAL)
                if (pfd.revents & errorEvents) != 0 {
                    throw NodeClientError.connection("poll")
                }
            } else if rc == 0 {
                throw NodeClientError.timeout
            } else if errno != EINTR {
                throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
            }
        }
    }
}

private struct WebSocketFrame {
    var fin: Bool
    var opcode: UInt8
    var payload: Data
}
