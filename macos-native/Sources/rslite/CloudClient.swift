import Foundation

/// 仅供云端延迟诊断使用的单调时钟。headless 运行时由 main.swift 注入与 events.jsonl
/// 相同的起点；非 headless 运行没有 events.jsonl，则退回到进程 uptime。
final class CloudTiming: @unchecked Sendable {
    static let shared = CloudTiming()

    private let lock = NSLock()
    private var nowProvider: @Sendable () -> Double = { ProcessInfo.processInfo.systemUptime }

    func setNowProvider(_ provider: @escaping @Sendable () -> Double) {
        lock.withLockVoid { nowProvider = provider }
    }

    func write(_ line: @escaping (Double) -> String) {
        lock.withLockVoid {
            FileHandle.standardError.write(Data(line(nowProvider()).utf8))
        }
    }
}

struct CloudSegment: Codable, Equatable {
    var t0: Double
    var t1: Double
    var text: String
}

struct CloudTranscriptionResult: Equatable {
    var text: String
    var segments: [CloudSegment]
    var statusCode: Int
    var xGroqID: String?
    var remainingRequests: Int?
    /// `x-ratelimit-reset-requests` 解析后的秒数；保持默认值以兼容既有 selftest 构造点。
    var resetRequestsS: Double? = nil
}

struct CloudQuotaHeaders: Equatable {
    var remainingRequests: Int?
    var resetRequestsS: Double?

    init(remainingRequests: Int? = nil, resetRequestsS: Double? = nil) {
        self.remainingRequests = remainingRequests
        self.resetRequestsS = resetRequestsS
    }
}

struct CloudLogEntry: Equatable {
    var xGroqID: String?
    var duration: Double
    var statusCode: Int?
    var reason: String
    var errorType: String?
}

enum CloudClientError: Error, CustomStringConvertible {
    case httpStatus(Int, retryAfter: Int?, xGroqID: String?, quota: CloudQuotaHeaders? = nil)
    case network(String)
    case invalidResponse
    case decodeFailed
    case cancelled

    var description: String {
        switch self {
        case .httpStatus(let status, let retryAfter, _, _):
            if let retryAfter {
                return "Groq HTTP \(status), retry_after=\(retryAfter)"
            }
            return "Groq HTTP \(status)"
        case .network(let type):
            return "Groq network error: \(type)"
        case .invalidResponse:
            return "Groq response invalid"
        case .decodeFailed:
            return "Groq response decode failed"
        case .cancelled:
            return "Groq request cancelled"
        }
    }
}

final class CloudClient: @unchecked Sendable {
    static let endpoint = URL(string: "https://api.groq.com/openai/v1/audio/transcriptions")!
    static let connectTimeout: TimeInterval = 5
    static let totalTimeout: TimeInterval = 10

    private let key: String
    private let endpointURL: URL
    private let session: URLSession
    private let logger: @Sendable (CloudLogEntry) -> Void
    private let lock = NSLock()
    private var slots: [Int: InflightSlot] = [:]
    private var nextTaskID = 0
    /// 取消代次。cancelInflightRequests 递增后，尚未登记 slot 的 transcribe 看到代次变化就不再 resume。
    private var cancelGeneration = 0
    /// selftest 在登记 slot 之前插入取消。生产路径保持 nil。
    nonisolated(unsafe) static var testBeforeRegister: (@Sendable () -> Void)?

    private enum InflightSlot {
        case reserved
        case running(URLSessionDataTask)
        case cancelRequested
    }

    init(
        key: String,
        endpoint: URL = CloudClient.endpoint,
        sessionConfiguration: URLSessionConfiguration = CloudClient.defaultSessionConfiguration(),
        logger: @escaping @Sendable (CloudLogEntry) -> Void = { _ in }
    ) {
        self.key = key
        self.endpointURL = endpoint
        self.session = URLSession(configuration: sessionConfiguration)
        self.logger = logger
    }

    static func defaultSessionConfiguration() -> URLSessionConfiguration {
        let configuration = URLSessionConfiguration.default
        configuration.timeoutIntervalForRequest = connectTimeout
        configuration.timeoutIntervalForResource = totalTimeout
        // RFC S2：不设置 delegate、不关闭 TLS 校验；default configuration 继续使用系统代理。
        return configuration
    }

    /// 开关关闭时丢掉全部在飞请求。单个任务取消走 `cancelTask`，不会进这里。
    func cancelInflightRequests() {
        let active = lock.withLockValue { () -> [URLSessionDataTask] in
            cancelGeneration += 1
            var found: [URLSessionDataTask] = []
            var pending: [Int: InflightSlot] = [:]
            for (id, slot) in slots {
                switch slot {
                case .running(let task):
                    found.append(task)
                case .reserved, .cancelRequested:
                    pending[id] = .cancelRequested
                }
            }
            slots = pending
            return found
        }
        active.forEach { $0.cancel() }
    }

    private func cancelTask(_ id: Int) {
        let task = lock.withLockValue { () -> URLSessionDataTask? in
            switch slots[id] {
            case .running(let task):
                slots.removeValue(forKey: id)
                return task
            case .reserved:
                slots[id] = .cancelRequested
                return nil
            case .cancelRequested, .none:
                return nil
            }
        }
        task?.cancel()
    }

    func transcribe(
        pcm16Mono16k: Data,
        a0: Double,
        duration: Double,
        language: String,
        segmentID: Int? = nil
    ) async throws -> CloudTranscriptionResult {
        let wav = Self.wavData(pcm16Mono16k: pcm16Mono16k, sampleRate: 16_000, channels: 1)
        var request = URLRequest(url: endpointURL)
        request.httpMethod = "POST"
        let boundary = "rslite-\(UUID().uuidString)"
        request.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        request.httpBody = Self.multipartBody(
            boundary: boundary,
            fields: [
                "model": "whisper-large-v3",
                "language": language,
                "response_format": "verbose_json",
            ],
            fileField: "file",
            filename: "segment.wav",
            contentType: "audio/wav",
            fileData: wav
        )

        let generation = lock.withLockValue { cancelGeneration }
        let (data, response) = try await data(for: request, duration: duration, generation: generation, segmentID: segmentID)
        guard let http = response as? HTTPURLResponse else {
            logger(CloudLogEntry(xGroqID: nil, duration: duration, statusCode: nil, reason: "invalid_response", errorType: nil))
            throw CloudClientError.invalidResponse
        }
        if let segmentID {
            CloudTiming.shared.write { now in
                String(
                    format: "rslite.cloud recv id=%d status=%d t=%.3f\n",
                    locale: Locale(identifier: "en_US_POSIX"),
                    segmentID,
                    http.statusCode,
                    now
                )
            }
        }
        let groqID = Self.groqID(from: http)
        let quota = CloudQuotaHeaders(
            remainingRequests: QuotaLedger.remainingRequests(from: http.allHeaderFields),
            resetRequestsS: QuotaLedger.resetRequestsSeconds(from: http.allHeaderFields)
        )
        guard (200..<300).contains(http.statusCode) else {
            let retryAfter = Self.retryAfter(from: http)
            logger(CloudLogEntry(xGroqID: groqID, duration: duration, statusCode: http.statusCode, reason: Self.reason(for: http.statusCode), errorType: nil))
            throw CloudClientError.httpStatus(http.statusCode, retryAfter: retryAfter, xGroqID: groqID, quota: quota)
        }
        do {
            let decoded = try JSONDecoder().decode(GroqVerboseResponse.self, from: data)
            logger(CloudLogEntry(xGroqID: groqID, duration: duration, statusCode: http.statusCode, reason: "ok", errorType: nil))
            let remaining = quota.remainingRequests
            if let remaining {
                // 只写这个整数，不写 key / URL / 正文 / 其它响应头。风格照抄 rslite.route。
                FileHandle.standardError.write(Data("rslite.cloud remaining_requests=\(remaining)\n".utf8))
            }
            return CloudTranscriptionResult(
                text: decoded.text ?? decoded.segments.map(\.text).joined(separator: " "),
                segments: decoded.segments.map {
                    CloudSegment(t0: a0 + $0.start, t1: a0 + $0.end, text: $0.text)
                },
                statusCode: http.statusCode,
                xGroqID: groqID,
                remainingRequests: remaining,
                resetRequestsS: quota.resetRequestsS
            )
        } catch {
            logger(CloudLogEntry(xGroqID: groqID, duration: duration, statusCode: http.statusCode, reason: "decode_failed", errorType: "decode"))
            throw CloudClientError.decodeFailed
        }
    }

    private func data(
        for request: URLRequest,
        duration: Double,
        generation: Int,
        segmentID: Int?
    ) async throws -> (Data, URLResponse) {
        Self.testBeforeRegister?()
        let id = lock.withLockValue { () -> Int? in
            if cancelGeneration != generation {
                return nil
            }
            nextTaskID += 1
            slots[nextTaskID] = .reserved
            return nextTaskID
        }
        guard let id else {
            throw CloudClientError.cancelled
        }
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<(Data, URLResponse), Error>) in
                let aborted = self.lock.withLockValue { () -> Bool in
                    if self.cancelGeneration != generation {
                        self.slots.removeValue(forKey: id)
                        return true
                    }
                    if case .cancelRequested = self.slots[id] {
                        self.slots.removeValue(forKey: id)
                        return true
                    }
                    return false
                }
                if aborted {
                    continuation.resume(throwing: CloudClientError.cancelled)
                    return
                }
                let task = self.session.dataTask(with: request) { data, response, error in
                    self.lock.withLockVoid { self.slots.removeValue(forKey: id) }
                    if let error {
                        let type = Self.errorType(error)
                        self.logger(CloudLogEntry(xGroqID: nil, duration: duration, statusCode: nil, reason: "network_error", errorType: type))
                        if (error as NSError).code == NSURLErrorCancelled {
                            continuation.resume(throwing: CloudClientError.cancelled)
                        } else {
                            continuation.resume(throwing: CloudClientError.network(type))
                        }
                        return
                    }
                    guard let data, let response else {
                        self.logger(CloudLogEntry(xGroqID: nil, duration: duration, statusCode: nil, reason: "invalid_response", errorType: nil))
                        continuation.resume(throwing: CloudClientError.invalidResponse)
                        return
                    }
                    continuation.resume(returning: (data, response))
                }
                let cancelNow = self.lock.withLockValue { () -> Bool in
                    if self.cancelGeneration != generation {
                        self.slots.removeValue(forKey: id)
                        return true
                    }
                    if case .cancelRequested = self.slots[id] {
                        self.slots.removeValue(forKey: id)
                        return true
                    }
                    self.slots[id] = .running(task)
                    return false
                }
                if let segmentID {
                    CloudTiming.shared.write { now in
                        String(
                            format: "rslite.cloud send id=%d t=%.3f\n",
                            locale: Locale(identifier: "en_US_POSIX"),
                            segmentID,
                            now
                        )
                    }
                }
                task.resume()
                if cancelNow {
                    task.cancel()
                }
            }
        } onCancel: {
            self.cancelTask(id)
        }
    }

    static func wavData(pcm16Mono16k: Data, sampleRate: Int, channels: Int) -> Data {
        var data = Data()
        let byteRate = sampleRate * channels * 2
        let blockAlign = channels * 2
        data.appendAscii("RIFF")
        data.appendUInt32LE(UInt32(36 + pcm16Mono16k.count))
        data.appendAscii("WAVE")
        data.appendAscii("fmt ")
        data.appendUInt32LE(16)
        data.appendUInt16LE(1)
        data.appendUInt16LE(UInt16(channels))
        data.appendUInt32LE(UInt32(sampleRate))
        data.appendUInt32LE(UInt32(byteRate))
        data.appendUInt16LE(UInt16(blockAlign))
        data.appendUInt16LE(16)
        data.appendAscii("data")
        data.appendUInt32LE(UInt32(pcm16Mono16k.count))
        data.append(pcm16Mono16k)
        return data
    }

    static func multipartBody(
        boundary: String,
        fields: [String: String],
        fileField: String,
        filename: String,
        contentType: String,
        fileData: Data
    ) -> Data {
        var body = Data()
        for (name, value) in fields.sorted(by: { $0.key < $1.key }) {
            body.appendAscii("--\(boundary)\r\n")
            body.appendAscii("Content-Disposition: form-data; name=\"\(name)\"\r\n\r\n")
            body.appendAscii("\(value)\r\n")
        }
        body.appendAscii("--\(boundary)\r\n")
        body.appendAscii("Content-Disposition: form-data; name=\"\(fileField)\"; filename=\"\(filename)\"\r\n")
        body.appendAscii("Content-Type: \(contentType)\r\n\r\n")
        body.append(fileData)
        body.appendAscii("\r\n--\(boundary)--\r\n")
        return body
    }

    private static func groqID(from response: HTTPURLResponse) -> String? {
        response.value(forHTTPHeaderField: "x_groq.id")
            ?? response.value(forHTTPHeaderField: "x-groq-id")
            ?? response.value(forHTTPHeaderField: "x-request-id")
    }

    private static func retryAfter(from response: HTTPURLResponse) -> Int? {
        response.value(forHTTPHeaderField: "retry-after").flatMap(Int.init)
    }

    private static func reason(for status: Int) -> String {
        switch status {
        case 401, 403:
            return "invalid_key"
        case 429:
            return "rate_limited"
        case 500...599:
            return "server_error"
        default:
            return "http_error"
        }
    }

    private static func errorType(_ error: Error) -> String {
        let ns = error as NSError
        if ns.domain == NSURLErrorDomain {
            switch ns.code {
            case NSURLErrorTimedOut:
                return "timeout"
            case NSURLErrorCancelled:
                return "cancelled"
            case NSURLErrorCannotFindHost, NSURLErrorCannotConnectToHost, NSURLErrorNetworkConnectionLost, NSURLErrorNotConnectedToInternet:
                return "network"
            default:
                return "url_error_\(ns.code)"
            }
        }
        return String(describing: type(of: error))
    }

    fileprivate static func bodyData(from request: URLRequest) -> Data {
        if let body = request.httpBody {
            return body
        }
        guard let stream = request.httpBodyStream else {
            return Data()
        }
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 4096)
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            if count <= 0 { break }
            data.append(buffer, count: count)
        }
        return data
    }

    static func selfTest() -> [String] {
        testBeforeRegister = nil
        let failures = CloudFailureBox()
        let check: @Sendable (Bool, String) -> Void = { ok, name in
            if !ok { failures.append(name) }
        }
        let fakeKey = "gsk_selftest_fake_key_abcdefghijklmnopqrstuvwxyz"
        let config = defaultSessionConfiguration()
        config.protocolClasses = [CloudURLProtocolStub.self]
        let logs = CloudLogBox()
        let client = CloudClient(key: fakeKey, endpoint: endpoint, sessionConfiguration: config) { entry in
            logs.append(entry)
        }
        let probeBody = multipartBody(
            boundary: "selftest",
            fields: ["model": "whisper-large-v3", "language": "de", "response_format": "verbose_json"],
            fileField: "file",
            filename: "segment.wav",
            contentType: "audio/wav",
            fileData: wavData(pcm16Mono16k: Data([0, 0]), sampleRate: 16_000, channels: 1)
        )
        check(probeBody.range(of: Data("verbose_json".utf8)) != nil, "multipart 应请求 verbose_json")
        check(probeBody.range(of: Data("audio/wav".utf8)) != nil, "multipart 应包含 WAV 文件")
        check(probeBody.range(of: Data("RIFF".utf8)) != nil, "multipart 应包含 WAV RIFF 头")

        func run(_ operation: @escaping @Sendable () async throws -> Void) {
            let semaphore = DispatchSemaphore(value: 0)
            Task.detached {
                do {
                    try await operation()
                } catch {
                    failures.append("CloudClient async selftest 异常：\(error)")
                }
                semaphore.signal()
            }
            if semaphore.wait(timeout: .now() + 5) == .timedOut {
                failures.append("CloudClient async selftest 超时")
            }
        }

        run {
            CloudURLProtocolStub.handler = { request in
                check(request.value(forHTTPHeaderField: "Authorization") == "Bearer \(fakeKey)", "请求应设置 Authorization header")
                check(request.timeoutInterval != Self.totalTimeout, "请求不应设置 timeoutInterval 覆盖 session 总超时")
                return .success(
                    status: 200,
                    headers: [
                        "x_groq.id": "groq-ok",
                        "x-ratelimit-remaining-requests": "1980",
                        "x-ratelimit-reset-requests": "2m59.56s",
                    ],
                    body: Data(#"{"text":"Hallo","segments":[{"start":0.1,"end":0.4,"text":"Hallo"}]}"#.utf8)
                )
            }
            let result = try await client.transcribe(
                pcm16Mono16k: Data([0, 0, 1, 0]),
                a0: 12,
                duration: 0.3,
                language: "de"
            )
            check(result.segments == [CloudSegment(t0: 12.1, t1: 12.4, text: "Hallo")], "segments 应映射到 a0 时间轴")
            check(result.remainingRequests == 1980, "成功响应应带上 remainingRequests")
            check(abs((result.resetRequestsS ?? 0) - 179.56) < 1e-9, "成功响应应带上 resetRequestsS")
        }

        run {
            CloudURLProtocolStub.handler = { _ in
                .success(status: 429, headers: [
                    "x-ratelimit-remaining-requests": "0",
                    "x-ratelimit-reset-requests": "120ms",
                ], body: Data())
            }
            do {
                _ = try await client.transcribe(pcm16Mono16k: Data([0, 0]), a0: 0, duration: 1, language: "de")
                check(false, "429 应抛出带配额头的错误")
            } catch let CloudClientError.httpStatus(_, _, _, quota) {
                check(quota?.remainingRequests == 0, "429 错误应带 remaining=0")
                check(abs((quota?.resetRequestsS ?? 0) - 0.12) < 1e-9, "429 错误应带 resetRequestsS=0.12")
            } catch {
                check(false, "429 应保留为 CloudClientError.httpStatus")
            }
        }

        for status in [401, 429, 500] {
            run {
                logs.removeAll()
                CloudURLProtocolStub.handler = { _ in
                    .success(
                        status: status,
                        headers: ["x_groq.id": "groq-\(status)", "retry-after": "61"],
                        body: Data(#"{"error":"nope"}"#.utf8)
                    )
                }
                do {
                    _ = try await client.transcribe(pcm16Mono16k: Data([0, 0]), a0: 0, duration: 2, language: "de")
                    check(false, "HTTP \(status) 应抛错")
                } catch {
                    let description = "\(error)"
                    check(!description.contains(fakeKey), "HTTP \(status) 错误描述不得泄露 key")
                    check(!String(describing: logs.all()).contains(fakeKey), "HTTP \(status) 日志不得泄露 key")
                    check(logs.last?.statusCode == status, "HTTP \(status) 日志应记录状态码")
                }
            }
        }

        run {
            logs.removeAll()
            CloudURLProtocolStub.handler = { _ in .failure(URLError(.timedOut)) }
            do {
                _ = try await client.transcribe(pcm16Mono16k: Data([0, 0]), a0: 0, duration: 2, language: "de")
                check(false, "超时应抛错")
            } catch {
                check(!"\(error)".contains(fakeKey), "超时错误描述不得泄露 key")
                check(logs.last?.errorType == "timeout", "超时日志应只记录错误类型")
                check(!String(describing: logs.all()).contains(fakeKey), "超时日志不得泄露 key")
            }
        }

        run {
            logs.removeAll()
            CloudURLProtocolStub.handler = { _ in .delayed }
            let task = Task {
                try await client.transcribe(pcm16Mono16k: Data([0, 0]), a0: 0, duration: 2, language: "de")
            }
            try await Task.sleep(nanoseconds: 50_000_000)
            client.cancelInflightRequests()
            do {
                _ = try await task.value
                check(false, "取消在飞请求应抛错")
            } catch {
                check(!"\(error)".contains(fakeKey), "取消错误描述不得泄露 key")
            }
        }

        run {
            logs.removeAll()
            CloudURLProtocolStub.handler = { request in
                let body = CloudClient.bodyData(from: request)
                if body.range(of: Data("name=\"language\"\r\n\r\nja".utf8)) != nil {
                    return .delayed
                }
                return .parked
            }
            let doomed = Task {
                try await client.transcribe(pcm16Mono16k: Data([0, 0]), a0: 0, duration: 1, language: "ja")
            }
            let kept = Task {
                try await client.transcribe(pcm16Mono16k: Data([0, 0]), a0: 1, duration: 1, language: "de")
            }
            try await Task.sleep(nanoseconds: 100_000_000)
            doomed.cancel()
            var cancelThrew = false
            do {
                _ = try await doomed.value
            } catch {
                cancelThrew = true
                check(!"\(error)".contains(fakeKey), "取消单个任务的错误描述不得泄露 key")
            }
            check(cancelThrew, "取消其中一个并发请求应抛错")
            let resumed = CloudURLProtocolStub.resumeParked(
                bodyNeedle: "name=\"language\"\r\n\r\nde",
                status: 200,
                headers: [
                    "x_groq.id": "groq-keep",
                    "x-ratelimit-remaining-requests": "1970",
                ],
                body: Data(#"{"text":"Hallo","segments":[{"start":0.0,"end":0.2,"text":"Hallo"}]}"#.utf8)
            )
            check(resumed == 1, "取消一个请求后另一个应仍在飞行")
            do {
                let keptResult = try await kept.value
                check(keptResult.text == "Hallo", "未取消的并发请求应正常完成")
                check(keptResult.remainingRequests == 1970, "未取消的成功响应应带上 remainingRequests")
            } catch {
                check(false, "未取消的并发请求应正常完成，实际失败")
            }
        }

        run {
            CloudURLProtocolStub.starts = 0
            CloudURLProtocolStub.handler = { _ in
                .success(status: 200, headers: [:], body: Data(#"{"text":"x","segments":[]}"#.utf8))
            }
            let client = CloudClient(key: fakeKey, endpoint: endpoint, sessionConfiguration: config, logger: { _ in })
            testBeforeRegister = { client.cancelInflightRequests() }
            defer { testBeforeRegister = nil }
            var threw = false
            do {
                _ = try await client.transcribe(pcm16Mono16k: Data([0, 0]), a0: 0, duration: 0.2, language: "de")
            } catch {
                threw = true
                check(!"\(error)".contains(fakeKey), "登记前取消的错误描述不得泄露 key")
            }
            check(threw, "登记前取消应直接抛取消")
            check(CloudURLProtocolStub.starts == 0, "登记前取消时 URLProtocol 桩计数应为 0")
        }

        let session = defaultSessionConfiguration()
        check(session.timeoutIntervalForRequest == connectTimeout, "session timeoutIntervalForRequest 应为 5 秒")
        check(session.timeoutIntervalForResource == totalTimeout, "session timeoutIntervalForResource 应为 10 秒")
        check(session.connectionProxyDictionary == nil, "CloudClient 不应禁用系统代理")
        return failures.all()
    }
}

private final class CloudFailureBox: @unchecked Sendable {
    private let lock = NSLock()
    private var entries: [String] = []

    func append(_ entry: String) {
        lock.withLockVoid { entries.append(entry) }
    }

    func all() -> [String] {
        lock.withLockValue { entries }
    }
}

private struct GroqVerboseResponse: Decodable {
    var text: String?
    var segments: [GroqSegment]
}

private struct GroqSegment: Decodable {
    var start: Double
    var end: Double
    var text: String
}

private extension Data {
    mutating func appendAscii(_ text: String) {
        append(Data(text.utf8))
    }

    mutating func appendUInt16LE(_ value: UInt16) {
        var little = value.littleEndian
        Swift.withUnsafeBytes(of: &little) { append(contentsOf: $0) }
    }

    mutating func appendUInt32LE(_ value: UInt32) {
        var little = value.littleEndian
        Swift.withUnsafeBytes(of: &little) { append(contentsOf: $0) }
    }
}

private final class CloudLogBox: @unchecked Sendable {
    private let lock = NSLock()
    private var entries: [CloudLogEntry] = []

    func append(_ entry: CloudLogEntry) {
        lock.withLockVoid { entries.append(entry) }
    }

    func removeAll() {
        lock.withLockVoid { entries.removeAll() }
    }

    var last: CloudLogEntry? {
        lock.withLockValue { entries.last }
    }

    func all() -> [CloudLogEntry] {
        lock.withLockValue { entries }
    }
}

private enum CloudURLProtocolResult {
    case success(status: Int, headers: [String: String], body: Data)
    case failure(Error)
    case delayed
    case parked
}

private final class CloudURLProtocolStub: URLProtocol {
    nonisolated(unsafe) static var handler: ((URLRequest) -> CloudURLProtocolResult)?
    nonisolated(unsafe) static var starts = 0
    private static let parkLock = NSLock()
    nonisolated(unsafe) private static var parked: [ObjectIdentifier: CloudURLProtocolStub] = [:]

    override class func canInit(with request: URLRequest) -> Bool {
        true
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest {
        request
    }

    override func startLoading() {
        Self.starts += 1
        guard let result = Self.handler?(request) else {
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
            return
        }
        switch result {
        case .success(let status, let headers, let body):
            emitSuccess(status: status, headers: headers, body: body)
        case .failure(let error):
            client?.urlProtocol(self, didFailWithError: error)
        case .delayed:
            break
        case .parked:
            Self.park(self)
        }
    }

    override func stopLoading() {
        Self.unpark(self)
        client?.urlProtocol(self, didFailWithError: URLError(.cancelled))
    }

    @discardableResult
    static func resumeParked(
        bodyNeedle: String,
        status: Int,
        headers: [String: String],
        body: Data
    ) -> Int {
        let needle = Data(bodyNeedle.utf8)
        let matches: [CloudURLProtocolStub] = parkLock.withLockValue {
            let hits = parked.values.filter {
                CloudClient.bodyData(from: $0.request).range(of: needle) != nil
            }
            for hit in hits {
                parked.removeValue(forKey: ObjectIdentifier(hit))
            }
            return Array(hits)
        }
        for match in matches {
            match.emitSuccess(status: status, headers: headers, body: body)
        }
        return matches.count
    }

    private static func park(_ proto: CloudURLProtocolStub) {
        parkLock.withLockVoid { parked[ObjectIdentifier(proto)] = proto }
    }

    private static func unpark(_ proto: CloudURLProtocolStub) {
        parkLock.withLockVoid { parked.removeValue(forKey: ObjectIdentifier(proto)) }
    }

    private func emitSuccess(status: Int, headers: [String: String], body: Data) {
        let response = HTTPURLResponse(
            url: request.url!,
            statusCode: status,
            httpVersion: "HTTP/1.1",
            headerFields: headers
        )!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: body)
        client?.urlProtocolDidFinishLoading(self)
    }
}
