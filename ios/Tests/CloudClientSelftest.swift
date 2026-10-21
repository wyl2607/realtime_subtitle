import Foundation

/// CloudClient 的纯逻辑自测：只验 multipart 构造与响应解析，不起 URLSession、不发网络。
/// 真网络路径（超时、取消）留到接进广播扩展后用假服务器验。
func runCloudClientSelftest(_ check: @escaping (Bool, String) -> Void) {
    let fakeKey = "gsk_test_fake_key_abcdefghijklmnopqrstuvwxyz"

    do {
        let pcm = Data([1, 0, 2, 0, 3, 0, 4, 0])
        let wav = CloudClient.wavData(pcm16Mono16k: pcm, sampleRate: 16_000, channels: 1)
        check(wav.count == 44 + pcm.count, "WAV 头应为 44 字节，实际 \(wav.count - pcm.count)")
        check(Data(wav.prefix(4)) == Data("RIFF".utf8), "WAV 应以 RIFF 开头")
        check(Data(wav[8..<12]) == Data("WAVE".utf8), "WAV 应为 WAVE")
        check(Data(wav[36..<40]) == Data("data".utf8), "WAV 应包含 data 块")
        let dataLength = wav[40..<44].reversed().reduce(UInt32(0)) { ($0 << 8) | UInt32($1) }
        check(dataLength == UInt32(pcm.count), "data 块长度应等于 PCM 字节数，实际 \(dataLength)")
    }

    do {
        let fileData = CloudClient.wavData(pcm16Mono16k: Data([0xDE, 0xAD, 0xBE, 0xEF]), sampleRate: 16_000, channels: 1)
        let body = CloudClient.multipartBody(
            boundary: "selftest",
            fields: [
                "model": "whisper-large-v3",
                "language": "de",
                "response_format": "verbose_json",
            ],
            fileField: "file",
            filename: "segment.wav",
            contentType: "audio/wav",
            fileData: fileData
        )
        // 字段按名字排序：language、model、response_format；文件段固定在最后。
        let expectedText = "--selftest\r\n"
            + "Content-Disposition: form-data; name=\"language\"\r\n\r\n"
            + "de\r\n"
            + "--selftest\r\n"
            + "Content-Disposition: form-data; name=\"model\"\r\n\r\n"
            + "whisper-large-v3\r\n"
            + "--selftest\r\n"
            + "Content-Disposition: form-data; name=\"response_format\"\r\n\r\n"
            + "verbose_json\r\n"
            + "--selftest\r\n"
            + "Content-Disposition: form-data; name=\"file\"; filename=\"segment.wav\"\r\n"
            + "Content-Type: audio/wav\r\n\r\n"
        var expected = Data(expectedText.utf8)
        expected.append(fileData)
        expected.append(Data("\r\n--selftest--\r\n".utf8))
        check(body == expected, "multipart 字段、顺序与结束边界应与 rslite 逐字节一致")
        check(body.range(of: Data("verbose_json".utf8)) != nil, "multipart 应请求 verbose_json")
        check(body.range(of: Data("audio/wav".utf8)) != nil, "multipart 应包含 WAV 文件类型")
        check(body.range(of: Data("RIFF".utf8)) != nil, "multipart 应包含 WAV RIFF 头")
    }

    do {
        let request = CloudClient.makeRequest(
            key: fakeKey,
            pcm16Mono16k: Data([0, 0, 1, 0]),
            language: "de",
            endpoint: CloudClient.endpoint
        )
        check(request.url?.absoluteString == "https://api.groq.com/openai/v1/audio/transcriptions",
              "请求应打到 Groq transcriptions，实际 \(request.url?.absoluteString ?? "nil")")
        check(request.httpMethod == "POST", "请求方法应为 POST")
        check(request.value(forHTTPHeaderField: "Authorization") == "Bearer \(fakeKey)",
              "Authorization 头应携带 Bearer key")
        let contentType = request.value(forHTTPHeaderField: "Content-Type") ?? ""
        check(contentType.hasPrefix("multipart/form-data; boundary=rslite-"),
              "Content-Type 应带 rslite- 前缀的 boundary，实际 \(contentType)")
        check(!(request.url?.absoluteString.contains(fakeKey) ?? false), "key 不得出现在 URL")
        check(!(request.url?.absoluteString.contains("key=") ?? false), "URL 不应带 key 查询参数")
        let bodyText = String(decoding: request.httpBody ?? Data(), as: UTF8.self)
        check(!bodyText.contains(fakeKey), "key 不得出现在 multipart body")
        check(bodyText.contains("name=\"language\"\r\n\r\nde"), "body 应带语言字段")
        let boundary = contentType.replacingOccurrences(of: "multipart/form-data; boundary=", with: "")
        check(bodyText.hasPrefix("--\(boundary)\r\n"), "body 应以当前 boundary 开头")
        check(bodyText.hasSuffix("\r\n--\(boundary)--\r\n"), "body 应以结束 boundary 收尾")
    }

    func parseError(
        _ status: Int,
        headers: [AnyHashable: Any],
        body: String,
        _ check: (Bool, String) -> Void
    ) -> CloudClientError? {
        do {
            _ = try CloudClient.parse(statusCode: status, headers: headers, body: Data(body.utf8), a0: 0)
            check(false, "HTTP \(status) 应抛错")
            return nil
        } catch let error as CloudClientError {
            check(!String(describing: error).contains(fakeKey), "HTTP \(status) 错误描述不得泄露 key")
            return error
        } catch {
            check(false, "HTTP \(status) 应抛 CloudClientError，实际 \(error)")
            return nil
        }
    }

    do {
        let result = try? CloudClient.parse(
            statusCode: 200,
            headers: [
                "x_groq.id": "groq-ok",
                "x-ratelimit-remaining-requests": "1980",
            ],
            body: Data(#"{"text":"Hallo Welt","segments":[{"start":0.1,"end":0.4,"text":"Hallo"},{"start":0.5,"end":0.9,"text":"Welt"}]}"#.utf8),
            a0: 12
        )
        check(result?.statusCode == 200, "200 响应应带上状态码")
        check(result?.text == "Hallo Welt", "200 响应应保留顶层 text")
        check(result?.segments == [
            CloudSegment(t0: 12.1, t1: 12.4, text: "Hallo"),
            CloudSegment(t0: 12.5, t1: 12.9, text: "Welt"),
        ], "句时间应映射回 a0 样本时钟")
        check(result?.xGroqID == "groq-ok", "应读出 x_groq.id")
        check(result?.remainingRequests == 1980, "应读出 x-ratelimit-remaining-requests")
    }

    do {
        let result = try? CloudClient.parse(
            statusCode: 200,
            headers: [:],
            body: Data(#"{"text":"Nur Text","segments":[]}"#.utf8),
            a0: 3
        )
        check(result?.text == "Nur Text", "没有句时间时应保留顶层 text")
        check(result?.segments.isEmpty == true, "没有句时间时 segments 应为空")
        check(result?.remainingRequests == nil, "没有额度头时 remainingRequests 应为 nil")
    }

    do {
        let result = try? CloudClient.parse(
            statusCode: 200,
            headers: [:],
            body: Data(#"{"text":null,"segments":[{"start":0,"end":0.2,"text":"Hallo"}]}"#.utf8),
            a0: 0
        )
        check(result?.text == "Hallo", "text 缺失时应由 segments 拼接")
    }

    do {
        let error = parseError(
            401,
            headers: ["x_groq.id": "groq-401"],
            body: #"{"error":"invalid key"}"#,
            check
        )
        if case .httpStatus(let status, let retryAfter, let groqID)? = error {
            check(status == 401, "401 应作为 httpStatus 抛出")
            check(retryAfter == nil, "401 不应带 retry-after")
            check(groqID == "groq-401", "401 应保留请求 ID")
        } else if error != nil {
            check(false, "401 应抛 httpStatus，实际 \(String(describing: error))")
        }
    }

    do {
        let error = parseError(
            429,
            headers: ["retry-after": "61", "x_groq.id": "groq-429"],
            body: #"{"error":"rate limit"}"#,
            check
        )
        if case .httpStatus(let status, let retryAfter, let groqID)? = error {
            check(status == 429, "429 应作为 httpStatus 抛出")
            check(retryAfter == 61, "429 应解析 retry-after 整数秒，实际 \(String(describing: retryAfter))")
            check(groqID == "groq-429", "429 应保留请求 ID")
        } else if error != nil {
            check(false, "429 应抛 httpStatus，实际 \(String(describing: error))")
        }
    }

    do {
        let error = parseError(200, headers: [:], body: "not json", check)
        if case .decodeFailed? = error {
            // 预期。
        } else if error != nil {
            check(false, "非法 JSON 应抛 decodeFailed，实际 \(String(describing: error))")
        }
    }

    do {
        let configuration = CloudClient.defaultSessionConfiguration()
        check(configuration.timeoutIntervalForRequest == CloudClient.connectTimeout,
              "连接超时应为 5 秒，实际 \(configuration.timeoutIntervalForRequest)")
        check(configuration.timeoutIntervalForResource == CloudClient.totalTimeout,
              "总超时应为 10 秒，实际 \(configuration.timeoutIntervalForResource)")
        check(configuration.urlCache?.diskCapacity == 0, "ephemeral 会话不应落盘 URL 缓存")
    }
}
