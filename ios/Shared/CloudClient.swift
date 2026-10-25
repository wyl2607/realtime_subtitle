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
}

enum CloudClientError: Error, CustomStringConvertible {
    case httpStatus(Int, retryAfter: Int?, xGroqID: String?)
    case network(String)
    case invalidResponse
    case decodeFailed
    case cancelled

    var description: String {
        switch self {
        case .httpStatus(let status, let retryAfter, _):
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
        case running(URLSessionDataTask)
        case cancelRequested
    }

    init(
        key: String,
        endpoint: URL = CloudClient.endpoint,
        sessionConfiguration: URLSessionConfiguration = CloudClient.defaultSessionConfiguration()
    ) {
        self.key = key
        self.endpointURL = endpoint
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
        let active = lockValue { () -> [URLSessionDataTask] in
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
        let task = lockValue { () -> URLSessionDataTask? in
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
        // duration / segmentID 只给下一轮的上传调度记账与诊断用，这一层不发日志也不改请求。
        _ = duration
        _ = segmentID
        let request = Self.makeRequest(
            key: key,
            pcm16Mono16k: pcm16Mono16k,
            language: language,
            endpoint: endpointURL
        )
        let (data, response) = try await data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw CloudClientError.invalidResponse
        }
        return try Self.parse(
            statusCode: http.statusCode,
            headers: http.allHeaderFields,
            body: data,
            a0: a0
        )
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
        guard (200..<300).contains(statusCode) else {
            throw CloudClientError.httpStatus(
                statusCode,
                retryAfter: headerValue("retry-after", in: headers).flatMap(Int.init),
                xGroqID: groqID
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
                remainingRequests: QuotaLedger.remainingRequests(from: headers)
            )
        } catch {
            throw CloudClientError.decodeFailed
        }
    }

    private func data(for request: URLRequest) async throws -> (Data, URLResponse) {
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
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<(Data, URLResponse), Error>) in
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
                let task = session.dataTask(with: request) { data, response, error in
                    self.lockVoid { self.slots.removeValue(forKey: id) }
                    if let error {
                        if (error as NSError).code == NSURLErrorCancelled {
                            continuation.resume(throwing: CloudClientError.cancelled)
                        } else {
                            continuation.resume(
                                throwing: CloudClientError.network(Self.errorType(error))
                            )
                        }
                        return
                    }
                    guard let data, let response else {
                        continuation.resume(throwing: CloudClientError.invalidResponse)
                        return
                    }
                    continuation.resume(returning: (data, response))
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
                    slots[id] = .running(task)
                    return false
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
