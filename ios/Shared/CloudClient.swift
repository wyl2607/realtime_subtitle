import CryptoKit
import Foundation

/// Groq 返回的一段。t0/t1 已由解析函数按音频起点映回样本时钟，下一轮接广播扩展时不必再换算。
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
    /// `x-ratelimit-reset-requests` 解析后的秒数；默认值兼容既有构造点。
    var resetRequestsS: Double? = nil
    /// 这次请求实际使用的 key 的指纹（哈希，不是 key）；账本靠它绑定恢复记录。
    var keyFingerprint: String? = nil
    /// 请求发出时的墙钟秒；账本靠它识别上个周期在飞的迟到响应。
    var sentAt: Double? = nil
}

struct CloudQuotaHeaders: Equatable {
    var remainingRequests: Int?
    var resetRequestsS: Double?
    var keyFingerprint: String? = nil
    var sentAt: Double? = nil

    init(
        remainingRequests: Int? = nil,
        resetRequestsS: Double? = nil,
        keyFingerprint: String? = nil,
        sentAt: Double? = nil
    ) {
        self.remainingRequests = remainingRequests
        self.resetRequestsS = resetRequestsS
        self.keyFingerprint = keyFingerprint
        self.sentAt = sentAt
    }
}

enum CloudClientError: Error, CustomStringConvertible {
    case httpStatus(Int, retryAfter: Int?, xGroqID: String?, quota: CloudQuotaHeaders? = nil)
    case network(String)
    case invalidResponse
    case decodeFailed(quota: CloudQuotaHeaders? = nil)
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

/// 常量直接对齐 rslite/src/rslite/CloudClient.swift：模型、响应格式、超时与文件字段名都不在这里另起一套，
/// 否则 iOS 端和 mac 端对同一个 Groq 接口会得出不同结果。
final class CloudClient: @unchecked Sendable {
    static let endpoint = URL(string: "https://api.groq.com/openai/v1/audio/transcriptions")!
    static let connectTimeout: TimeInterval = 5
    static let totalTimeout: TimeInterval = 10
    static let model = "whisper-large-v3"
    static let responseFormat = "verbose_json"
    static let fileField = "file"
    static let filename = "segment.wav"
    static let fileContentType = "audio/wav"

    private let key: String
    private let endpointURL: URL
    private let session: URLSession
    private let lock = NSLock()
    private var slots: [Int: InflightSlot] = [:]
    private var nextTaskID = 0
    /// 取消代次。cancelInflightRequests 递增后，尚未登记 slot 的请求看到代次变化就不再 resume。
    private var cancelGeneration = 0

    private enum InflightSlot {
        case reserved
        case running(@Sendable () -> Void)
        case cancelRequested
    }

    /// 一次请求的启动与取消。URLSession 和蜂窝直连各给一份，共用同一套 slot / 取消代次。
    private struct Launch {
        let start: () -> Void
        let cancel: @Sendable () -> Void
    }

    init(
        key: String,
        endpoint: URL = CloudClient.endpoint,
        sessionConfiguration: URLSessionConfiguration = CloudClient.defaultSessionConfiguration()
    ) {
        self.key = key
        self.endpointURL = endpoint
        // NWPathMonitor 启动后首个回调是异步的；在这里先把单例建起来，
        // 否则第一次上传失败时快照还是默认的 wifi=false/cellular=false，蜂窝重发必然不触发。
        _ = NetworkPaths.shared
        self.session = URLSession(configuration: sessionConfiguration)
    }

    /// 云端请求必须严格不落盘：广播扩展与 app 共享容器，缓存和 cookie 都不是这里要的东西。
    /// 系统代理沿用 ephemeral 配置的默认行为（不禁用），否则只能走代理的用户会连不上 Groq。
    static func defaultSessionConfiguration() -> URLSessionConfiguration {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = connectTimeout
        configuration.timeoutIntervalForResource = totalTimeout
        return configuration
    }

    /// 开关关闭时丢掉全部在飞请求。单个任务取消走 task.cancel()，不会进这里。
    func cancelInflightRequests() {
        let active = lockValue { () -> [@Sendable () -> Void] in
            cancelGeneration += 1
            var found: [@Sendable () -> Void] = []
            var pending: [Int: InflightSlot] = [:]
            for (id, slot) in slots {
                switch slot {
                case .running(let cancel):
                    found.append(cancel)
                case .reserved, .cancelRequested:
                    pending[id] = .cancelRequested
                }
            }
            slots = pending
            return found
        }
        active.forEach { $0() }
    }

    private func cancelTask(_ id: Int) {
        let cancel = lockValue { () -> (@Sendable () -> Void)? in
            switch slots[id] {
            case .running(let cancel):
                slots.removeValue(forKey: id)
                return cancel
            case .reserved:
                slots[id] = .cancelRequested
                return nil
            case .cancelRequested, .none:
                return nil
            }
        }
        cancel?()
    }

    func transcribe(
        pcm16Mono16k: Data,
        a0: Double,
        duration: Double,
        language: String,
        segmentID: Int? = nil,
        beforeCellularRetry: (@Sendable () -> Void)? = nil
    ) async throws -> CloudTranscriptionResult {
        // duration / segmentID 只给下一轮的上传调度记账与诊断用，这一层不发日志也不改请求。
        _ = duration
        _ = segmentID
        let request = Self.makeRequest(
            key: key,
            pcm16Mono16k: pcm16Mono16k,
            language: language,
            endpoint: endpointURL
        )
        // 请求发出时刻与 key 指纹随结果/错误一起返回；账本用它们做恢复记录的绑定与乱序判定。
        var sentAt = Date().timeIntervalSince1970
        let fingerprint = Self.keyFingerprint(key)
        let reply: HTTPReply
        do {
            reply = try await perform(urlSessionLaunch(request))
        } catch let error as CloudClientError where CellularFallback.shouldRetry(
            after: error,
            paths: NetworkPaths.shared.snapshot,
            systemProxyConfigured: CellularFallback.systemProxyConfigured()
        ) {
            // Wi-Fi 连着但这一段没传上去：同一段换蜂窝重发一次，字幕不断。
            // 重发是另一个计费请求，调用方在回调里记账。
            beforeCellularRetry?()
            sentAt = Date().timeIntervalSince1970
            reply = try await perform(cellularLaunch(request))
        }
        do {
            var result = try Self.parse(
                statusCode: reply.statusCode,
                headers: reply.headers,
                body: reply.body,
                a0: a0
            )
            result.keyFingerprint = fingerprint
            result.sentAt = sentAt
            return result
        } catch let error as CloudClientError {
            if case .httpStatus(let status, let retryAfter, let groqID, let quota) = error {
                var stamped = quota ?? CloudQuotaHeaders()
                stamped.keyFingerprint = fingerprint
                stamped.sentAt = sentAt
                throw CloudClientError.httpStatus(
                    status,
                    retryAfter: retryAfter,
                    xGroqID: groqID,
                    quota: stamped
                )
            }
            if case .decodeFailed(let quota) = error {
                var stamped = quota ?? CloudQuotaHeaders()
                stamped.keyFingerprint = fingerprint
                stamped.sentAt = sentAt
                throw CloudClientError.decodeFailed(quota: stamped)
            }
            throw error
        }
    }

    /// key 指纹：只用于账本绑定同一把 key 的恢复记录，不参与鉴权、不进日志。
    static func keyFingerprint(_ key: String) -> String {
        SHA256.hash(data: Data(key.utf8)).map { String(format: "%02x", $0) }.joined()
    }

    /// 构造请求是纯函数：自测可以逐字节比对 multipart，不需要起网络栈。
    static func makeRequest(key: String, pcm16Mono16k: Data, language: String, endpoint: URL) -> URLRequest {
        let wav = wavData(pcm16Mono16k: pcm16Mono16k, sampleRate: 16_000, channels: 1)
        let boundary = "rslite-\(UUID().uuidString)"
        var request = URLRequest(url: endpoint)
        request.httpMethod = "POST"
        // key 只放 Authorization 头，绝不进 URL / query / body，避免被日志或代理记录下来。
        request.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        request.httpBody = multipartBody(
            boundary: boundary,
            fields: [
                "model": model,
                "language": language,
                "response_format": responseFormat,
            ],
            fileField: fileField,
            filename: filename,
            contentType: fileContentType,
            fileData: wav
        )
        return request
    }

    /// 解析响应也是纯函数。4xx/5xx 一律走 httpStatus，body 只有 2xx 才尝试解码。
    static func parse(
        statusCode: Int,
        headers: [AnyHashable: Any],
        body: Data,
        a0: Double
    ) throws -> CloudTranscriptionResult {
        let groqID = headerValue("x_groq.id", in: headers)
            ?? headerValue("x-groq-id", in: headers)
            ?? headerValue("x-request-id", in: headers)
        let quota = CloudQuotaHeaders(
            remainingRequests: QuotaLedger.remainingRequests(from: headers),
            resetRequestsS: QuotaLedger.resetRequestsSeconds(from: headers)
        )
        guard (200..<300).contains(statusCode) else {
            throw CloudClientError.httpStatus(
                statusCode,
                retryAfter: headerValue("retry-after", in: headers).flatMap(Int.init),
                xGroqID: groqID,
                quota: quota
            )
        }
        do {
            let decoded = try JSONDecoder().decode(GroqVerboseResponse.self, from: body)
            return CloudTranscriptionResult(
                text: decoded.text ?? decoded.segments.map(\.text).joined(separator: " "),
                segments: decoded.segments.map {
                    CloudSegment(t0: a0 + $0.start, t1: a0 + $0.end, text: $0.text)
                },
                statusCode: statusCode,
                xGroqID: groqID,
                remainingRequests: quota.remainingRequests,
                resetRequestsS: quota.resetRequestsS
            )
        } catch {
            throw CloudClientError.decodeFailed(quota: quota)
        }
    }

    private func urlSessionLaunch(_ request: URLRequest) -> (@escaping CellularHTTPExchange.Completion) -> Launch {
        { complete in
            let task = self.session.dataTask(with: request) { data, response, error in
                if let error {
                    complete(.failure((error as NSError).code == NSURLErrorCancelled
                        ? .cancelled : .network(Self.errorType(error))))
                    return
                }
                guard let data, let http = response as? HTTPURLResponse else {
                    complete(.failure(.invalidResponse))
                    return
                }
                var headers: [String: String] = [:]
                for (key, value) in http.allHeaderFields {
                    headers[String(describing: key).lowercased()] = String(describing: value)
                }
                complete(.success(HTTPReply(statusCode: http.statusCode, headers: headers, body: data)))
            }
            return Launch(start: { task.resume() }, cancel: { task.cancel() })
        }
    }

    private func cellularLaunch(_ request: URLRequest) -> (@escaping CellularHTTPExchange.Completion) -> Launch {
        { complete in
            guard let exchange = CellularHTTPExchange(request: request, timeout: Self.totalTimeout, complete: complete) else {
                return Launch(start: { complete(.failure(.invalidResponse)) }, cancel: {})
            }
            return Launch(start: { exchange.start() }, cancel: { exchange.cancel() })
        }
    }

    /// 登记 slot → 启动 → 完成时注销。取消代次在登记前、登记时各查一次，
    /// cancelInflightRequests 和 Task 取消都能打断尚未启动或正在跑的请求。
    private func perform(_ makeLaunch: (@escaping CellularHTTPExchange.Completion) -> Launch) async throws -> HTTPReply {
        let generation = lockValue { cancelGeneration }
        let id = lockValue { () -> Int? in
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
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<HTTPReply, Error>) in
                let aborted = lockValue { () -> Bool in
                    if cancelGeneration != generation {
                        slots.removeValue(forKey: id)
                        return true
                    }
                    if case .cancelRequested = slots[id] {
                        slots.removeValue(forKey: id)
                        return true
                    }
                    return false
                }
                if aborted {
                    continuation.resume(throwing: CloudClientError.cancelled)
                    return
                }
                let launch = makeLaunch { result in
                    self.lockVoid { self.slots.removeValue(forKey: id) }
                    continuation.resume(with: result.mapError { $0 as any Error })
                }
                let cancelNow = lockValue { () -> Bool in
                    if cancelGeneration != generation {
                        slots.removeValue(forKey: id)
                        return true
                    }
                    if case .cancelRequested = slots[id] {
                        slots.removeValue(forKey: id)
                        return true
                    }
                    slots[id] = .running(launch.cancel)
                    return false
                }
                launch.start()
                if cancelNow {
                    launch.cancel()
                }
            }
        } onCancel: {
            self.cancelTask(id)
        }
    }

    private func lockValue<T>(_ body: () throws -> T) rethrows -> T {
        lock.lock()
        defer { lock.unlock() }
        return try body()
    }

    private func lockVoid(_ body: () -> Void) {
        lock.lock()
        defer { lock.unlock() }
        body()
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
        // 字段按名字排序，边界与文件段位置才能逐字节复现；rslite 也是这么写的。
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

    private static func headerValue(_ name: String, in headers: [AnyHashable: Any]) -> String? {
        for (key, value) in headers where String(describing: key).lowercased() == name.lowercased() {
            return String(describing: value)
        }
        return nil
    }

    private static func errorType(_ error: Error) -> String {
        let ns = error as NSError
        if ns.domain == NSURLErrorDomain {
            switch ns.code {
            case NSURLErrorTimedOut:
                return "timeout"
            case NSURLErrorCancelled:
                return "cancelled"
            case NSURLErrorCannotFindHost, NSURLErrorCannotConnectToHost,
                 NSURLErrorNetworkConnectionLost, NSURLErrorNotConnectedToInternet:
                return "network"
            default:
                return "url_error_\(ns.code)"
            }
        }
        return String(describing: type(of: error))
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
