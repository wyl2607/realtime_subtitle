import AVFoundation
import AudioToolbox
import CoreMedia
import Foundation

@main
struct NodeSelftest {
    static func main() {
        var failures: [String] = []
        func check(_ condition: Bool, _ name: String) {
            if !condition { failures.append(name) }
        }

        testHello(check)
        testParse(check)
        testIsFatal(check)
        testCloseReason(check)
        pcmChecks(sampleRate: 48_000, channels: 2, check: check)
        pcmChecks(sampleRate: 44_100, channels: 1, check: check)
        testCMSampleBufferPaths(check)
        runPairingSelftest(check)
        runTailnetPinSelftest(check)

        if failures.isEmpty {
            print("SELFTEST OK")
        } else {
            for f in failures { print("FAIL: \(f)") }
            exit(1)
        }
    }

    static func testHello(_ check: (Bool, String) -> Void) {
        guard let hello = NodeHello(src: "de", dst: "zh") else {
            check(false, "NodeHello 合法语言码应可构造")
            return
        }
        guard let data = hello.json().data(using: .utf8),
              let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
            check(false, "hello JSON 应可解析")
            return
        }
        check(obj["type"] as? String == "hello", "hello JSON type 字段")
        check((obj["v"] as? NSNumber)?.intValue == 2, "hello JSON v 字段")
        check(obj["src"] as? String == "de", "hello JSON src 字段")
        check(obj["dst"] as? String == "zh", "hello JSON dst 字段")
        check((obj["sample_rate"] as? NSNumber)?.intValue == 16000, "hello JSON sample_rate 字段")
        check(obj["format"] as? String == "s16le", "hello JSON format 字段")

        check(NodeHello(src: "zh-Hans", dst: "de") != nil, "合法 zh-Hans")
        check(NodeHello(src: "eng", dst: "de") != nil, "合法 eng")
        check(NodeHello(src: "de-DE", dst: "zh") != nil, "合法 de-DE")
        check(NodeHello(src: "DE", dst: "zh") == nil, "大写 DE 非法")
        check(NodeHello(src: "deutsch", dst: "zh") == nil, "过长语言码非法")
        check(NodeHello(src: "de", dst: "zh_CN") == nil, "下划线非法")
        check(NodeHello(src: "de", dst: "z") == nil, "单字符非法")
        check(NodeHello(src: "", dst: "zh") == nil, "空串非法")
        check(NodeHello(src: "de", dst: "") == nil, "空目标语言非法")
    }

    static func testParse(_ check: (Bool, String) -> Void) {
        if case .ready(let cold)? = NodeEvent.parse(#"{"ev":"ready","cold":true}"#) {
            check(cold, "ready cold=true")
        } else { check(false, "ready cold=true 应解析") }
        if case .ready(let cold)? = NodeEvent.parse(#"{"ev":"ready","cold":false}"#) {
            check(!cold, "ready cold=false")
        } else { check(false, "ready cold=false 应解析") }

        if case .final(let id, let a0, let a1, let text)? = NodeEvent.parse(#"{"ev":"final","id":3,"a0":1.25,"a1":2.5,"text":"Hallo"}"#) {
            check(id == 3 && a0 == 1.25 && a1 == 2.5 && text == "Hallo", "final 字段")
        } else { check(false, "final 应解析") }

        if case .translation(let id, let text)? = NodeEvent.parse(#"{"ev":"translation","id":3,"text":"你好"}"#) {
            check(id == 3 && text == "你好", "translation 字段")
        } else { check(false, "translation 应解析") }

        if case .status(let code, let text, let id)? = NodeEvent.parse(#"{"ev":"status","code":"translate_failed","text":"x","id":7}"#) {
            check(code == "translate_failed" && text == "x" && id == 7, "status 带 id")
        } else { check(false, "status 带 id 应解析") }
        if case .status(_, _, let id)? = NodeEvent.parse(#"{"ev":"status","code":"audio_dropped","text":"y"}"#) {
            check(id == nil, "status 无 id")
        } else { check(false, "status 无 id 应解析") }

        if case .drained? = NodeEvent.parse(#"{"ev":"drained"}"#) {
            check(true, "drained 应解析")
        } else { check(false, "drained 应解析") }

        check(NodeEvent.parse(#"{"ev":"ready"}"#) == nil, "ready 缺 cold → nil")
        check(NodeEvent.parse(#"{"ev":"final","id":3,"a0":1.0,"text":"x"}"#) == nil, "final 缺 a1 → nil")
        check(NodeEvent.parse(#"{"ev":"final","id":3,"a0":1.0,"a1":2.0}"#) == nil, "final 缺 text → nil")
        check(NodeEvent.parse(#"{"ev":"translation","text":"x"}"#) == nil, "translation 缺 id → nil")
        check(NodeEvent.parse(#"{"ev":"status","code":"c"}"#) == nil, "status 缺 text → nil")
        check(NodeEvent.parse(#"{"ev":"unknown"}"#) == nil, "未知 ev → nil")
        check(NodeEvent.parse("not json") == nil, "非 JSON → nil")
        check(NodeEvent.parse(#"{"ev":"final","id":"abc","a0":1.0,"a1":2.0,"text":"x"}"#) == nil, "id 类型错 → nil")
        check(NodeEvent.parse(#"{"ev":"ready","cold":1}"#) == nil, "cold 非布尔 → nil")
    }

    static func testIsFatal(_ check: (Bool, String) -> Void) {
        check(NodeEvent.isFatal(code: "protocol_error"), "protocol_error fatal")
        check(NodeEvent.isFatal(code: "engine_load_failed"), "engine_load_failed fatal")
        check(NodeEvent.isFatal(code: "internal_error"), "internal_error fatal")
        check(!NodeEvent.isFatal(code: "worker_crashed"), "worker_crashed 非 fatal")
        check(!NodeEvent.isFatal(code: "translate_failed"), "translate_failed 非 fatal")
        check(!NodeEvent.isFatal(code: "audio_dropped"), "audio_dropped 非 fatal")
    }

    static func testCloseReason(_ check: (Bool, String) -> Void) {
        let busyReason = Data([0x03, 0xF5]) + Data("busy".utf8)
        check(NodeEvent.closeReason(code: 1005, reasonData: busyReason) == 1013, "closeReason 1005+busy → 1013")
        check(NodeEvent.closeReason(code: 1005, reasonData: Data()) == 1005, "closeReason 1005 空载荷 → 1005")
        check(NodeEvent.closeReason(code: 1005, reasonData: nil) == 1005, "closeReason 1005 nil 载荷 → 1005")
        check(NodeEvent.closeReason(code: 1000, reasonData: nil) == 1000, "closeReason 1000 → 1000")
        check(NodeEvent.closeReason(code: 1005, reasonData: Data([0x03])) == 1005, "closeReason 载荷 <2 字节 → 原样")
        check(NodeEvent.closeReason(code: 1011, reasonData: busyReason) == 1011, "closeReason 1011 → 原样")
    }

    static func pcmChecks(sampleRate: Double, channels: Int, check: (Bool, String) -> Void) {
        let frameCount = Int(sampleRate)
        let amplitude = 8000.0
        let freq = 440.0
        var samples: [Int16] = []
        samples.reserveCapacity(frameCount * channels)
        for f in 0..<frameCount {
            let t = Double(f) / sampleRate
            let v = Int16(amplitude * sin(2.0 * Double.pi * freq * t))
            for _ in 0..<channels { samples.append(v) }
        }
        let inputRMS = rms(samples)

        let converted = PCMConverter.convert(interleavedInt16: samples, sampleRate: sampleRate, channels: channels)
        check(abs(converted.count - 32_000) <= 3_200,
              "PCM \(Int(sampleRate))Hz \(channels)ch 输出总字节 ≈ 32000（实际 \(converted.count)）")
        let outputRMS = rmsInt16Data(converted)
        let relDiff = inputRMS > 0 ? abs(outputRMS - inputRMS) / inputRMS : 0
        check(relDiff < 0.20, "PCM \(Int(sampleRate))Hz \(channels)ch RMS 相差 <20%（实际 \(relDiff)）")

        guard let sampleBuffer = makeSampleBuffer(samples, sampleRate: sampleRate, channels: channels) else {
            check(false, "PCM \(Int(sampleRate))Hz \(channels)ch 构造 CMSampleBuffer 失败")
            return
        }
        let frames = PCMConverter().append(sampleBuffer)
        var total = 0
        var all3200 = true
        for fr in frames {
            total += fr.count
            if fr.count != 3200 { all3200 = false }
        }
        check(all3200, "PCM \(Int(sampleRate))Hz \(channels)ch 每帧 3200 字节")
        check(!frames.isEmpty, "PCM \(Int(sampleRate))Hz \(channels)ch append 有帧")
        check(abs(total - 32_000) <= 3_200,
              "PCM \(Int(sampleRate))Hz \(channels)ch append 总字节 ≈ 32000（实际 \(total)）")
    }

    static func rms(_ samples: [Int16]) -> Double {
        guard !samples.isEmpty else { return 0 }
        var sum = 0.0
        for s in samples {
            let v = Double(s)
            sum += v * v
        }
        return (sum / Double(samples.count)).squareRoot()
    }

    static func rmsInt16Data(_ data: Data) -> Double {
        let count = data.count / 2
        guard count > 0 else { return 0 }
        var sum = 0.0
        data.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
            guard let base = raw.baseAddress else { return }
            let p = base.assumingMemoryBound(to: Int16.self)
            for i in 0..<count {
                let v = Double(p[i])
                sum += v * v
            }
        }
        return (sum / Double(count)).squareRoot()
    }

    static func makeSampleBuffer(_ samples: [Int16], sampleRate: Double, channels: Int) -> CMSampleBuffer? {
        var asbd = AudioStreamBasicDescription(
            mSampleRate: sampleRate,
            mFormatID: kAudioFormatLinearPCM,
            mFormatFlags: kAudioFormatFlagIsSignedInteger | kAudioFormatFlagIsPacked,
            mBytesPerPacket: UInt32(2 * channels),
            mFramesPerPacket: 1,
            mBytesPerFrame: UInt32(2 * channels),
            mChannelsPerFrame: UInt32(channels),
            mBitsPerChannel: 16,
            mReserved: 0
        )
        var formatDescription: CMAudioFormatDescription?
        guard CMAudioFormatDescriptionCreate(
            allocator: nil,
            asbd: &asbd,
            layoutSize: 0,
            layout: nil,
            magicCookieSize: 0,
            magicCookie: nil,
            extensions: nil,
            formatDescriptionOut: &formatDescription
        ) == noErr, let formatDescription else { return nil }

        let byteCount = samples.count * 2
        var blockBuffer: CMBlockBuffer?
        guard CMBlockBufferCreateWithMemoryBlock(
            allocator: nil,
            memoryBlock: nil,
            blockLength: byteCount,
            blockAllocator: nil,
            customBlockSource: nil,
            offsetToData: 0,
            dataLength: byteCount,
            flags: 0,
            blockBufferOut: &blockBuffer
        ) == kCMBlockBufferNoErr, let blockBuffer else { return nil }

        let copied = samples.withUnsafeBytes { (raw: UnsafeRawBufferPointer) -> OSStatus in
            guard let base = raw.baseAddress else { return -1 }
            return CMBlockBufferReplaceDataBytes(
                with: base,
                blockBuffer: blockBuffer,
                offsetIntoDestination: 0,
                dataLength: byteCount
            )
        }
        guard copied == kCMBlockBufferNoErr else { return nil }

        let frames = samples.count / channels
        var timing = CMSampleTimingInfo(
            duration: CMTime(value: 1, timescale: Int32(sampleRate)),
            presentationTimeStamp: CMTime(value: 0, timescale: Int32(sampleRate)),
            decodeTimeStamp: CMTime.invalid
        )
        var sampleBuffer: CMSampleBuffer?
        guard CMSampleBufferCreate(
            allocator: nil,
            dataBuffer: blockBuffer,
            dataReady: true,
            makeDataReadyCallback: nil,
            refcon: nil,
            formatDescription: formatDescription,
            sampleCount: frames,
            sampleTimingEntryCount: 1,
            sampleTimingArray: &timing,
            sampleSizeEntryCount: 0,
            sampleSizeArray: nil,
            sampleBufferOut: &sampleBuffer
        ) == noErr, let sampleBuffer else { return nil }
        return sampleBuffer
    }

    /// 造指定格式的 1 秒 440Hz 正弦 CMSampleBuffer，用于测试 PCMConverter.append
    /// 若 leftChannelOnly=true，仅左声道有信号，右声道全 0
    static func makeTestSampleBuffer(
        sampleRate: Double,
        channels: Int,
        isFloat: Bool,
        isBigEndian: Bool,
        isNonInterleaved: Bool,
        leftChannelOnly: Bool = false
    ) -> CMSampleBuffer? {
        let frames = Int(sampleRate)
        let amplitude = 0.5
        let freq = 440.0
        var samples: [Float] = []
        samples.reserveCapacity(frames * channels)
        for f in 0..<frames {
            let t = Double(f) / sampleRate
            let v = Float(amplitude * sin(2.0 * Double.pi * freq * t))
            for ch in 0..<channels {
                if leftChannelOnly && ch > 0 {
                    samples.append(0.0)
                } else {
                    samples.append(v)
                }
            }
        }

        let bytesPerSample = isFloat ? 4 : 2
        let byteCount = samples.count * bytesPerSample
        var data = Data(count: byteCount)

        data.withUnsafeMutableBytes { (dst: UnsafeMutableRawBufferPointer) in
            guard let base = dst.baseAddress else { return }
            let bytePtr = base.assumingMemoryBound(to: UInt8.self)

            if isFloat {
                if isNonInterleaved {
                    for ch in 0..<channels {
                        for f in 0..<frames {
                            let sample = samples[f * channels + ch]
                            let offset = (ch * frames + f) * bytesPerSample
                            let bits = sample.bitPattern
                            if isBigEndian {
                                bytePtr[offset] = UInt8(bits >> 24)
                                bytePtr[offset + 1] = UInt8((bits >> 16) & 0xFF)
                                bytePtr[offset + 2] = UInt8((bits >> 8) & 0xFF)
                                bytePtr[offset + 3] = UInt8(bits & 0xFF)
                            } else {
                                bytePtr[offset] = UInt8(bits & 0xFF)
                                bytePtr[offset + 1] = UInt8((bits >> 8) & 0xFF)
                                bytePtr[offset + 2] = UInt8((bits >> 16) & 0xFF)
                                bytePtr[offset + 3] = UInt8(bits >> 24)
                            }
                        }
                    }
                } else {
                    for f in 0..<frames {
                        for ch in 0..<channels {
                            let sample = samples[f * channels + ch]
                            let offset = (f * channels + ch) * bytesPerSample
                            let bits = sample.bitPattern
                            if isBigEndian {
                                bytePtr[offset] = UInt8(bits >> 24)
                                bytePtr[offset + 1] = UInt8((bits >> 16) & 0xFF)
                                bytePtr[offset + 2] = UInt8((bits >> 8) & 0xFF)
                                bytePtr[offset + 3] = UInt8(bits & 0xFF)
                            } else {
                                bytePtr[offset] = UInt8(bits & 0xFF)
                                bytePtr[offset + 1] = UInt8((bits >> 8) & 0xFF)
                                bytePtr[offset + 2] = UInt8((bits >> 16) & 0xFF)
                                bytePtr[offset + 3] = UInt8(bits >> 24)
                            }
                        }
                    }
                }
            } else {
                var intSamples: [Int16] = []
                intSamples.reserveCapacity(samples.count)
                for s in samples {
                    intSamples.append(Int16(max(-32768, min(32767, Int(s * 32767.0)))))
                }
                if isNonInterleaved {
                    for ch in 0..<channels {
                        for f in 0..<frames {
                            let sample = intSamples[f * channels + ch]
                            let offset = (ch * frames + f) * bytesPerSample
                            if isBigEndian {
                                bytePtr[offset] = UInt8((sample >> 8) & 0xFF)
                                bytePtr[offset + 1] = UInt8(sample & 0xFF)
                            } else {
                                bytePtr[offset] = UInt8(sample & 0xFF)
                                bytePtr[offset + 1] = UInt8((sample >> 8) & 0xFF)
                            }
                        }
                    }
                } else {
                    for f in 0..<frames {
                        for ch in 0..<channels {
                            let sample = intSamples[f * channels + ch]
                            let offset = (f * channels + ch) * bytesPerSample
                            if isBigEndian {
                                bytePtr[offset] = UInt8((sample >> 8) & 0xFF)
                                bytePtr[offset + 1] = UInt8(sample & 0xFF)
                            } else {
                                bytePtr[offset] = UInt8(sample & 0xFF)
                                bytePtr[offset + 1] = UInt8((sample >> 8) & 0xFF)
                            }
                        }
                    }
                }
            }
        }

        var flags = AudioFormatFlags(kAudioFormatFlagIsSignedInteger | kAudioFormatFlagIsPacked)
        if isFloat {
            flags = flags | kAudioFormatFlagIsFloat
        }
        if isBigEndian {
            flags = flags | kAudioFormatFlagIsBigEndian
        }
        if isNonInterleaved {
            flags = flags | kAudioFormatFlagIsNonInterleaved
        }

        var asbd = AudioStreamBasicDescription(
            mSampleRate: sampleRate,
            mFormatID: kAudioFormatLinearPCM,
            mFormatFlags: flags,
            mBytesPerPacket: UInt32(bytesPerSample * (isNonInterleaved ? 1 : channels)),
            mFramesPerPacket: 1,
            mBytesPerFrame: UInt32(bytesPerSample * (isNonInterleaved ? 1 : channels)),
            mChannelsPerFrame: UInt32(channels),
            mBitsPerChannel: UInt32(bytesPerSample * 8),
            mReserved: 0
        )

        var formatDescription: CMAudioFormatDescription?
        guard CMAudioFormatDescriptionCreate(
            allocator: nil,
            asbd: &asbd,
            layoutSize: 0,
            layout: nil,
            magicCookieSize: 0,
            magicCookie: nil,
            extensions: nil,
            formatDescriptionOut: &formatDescription
        ) == noErr, let formatDescription else { return nil }

        var blockBuffer: CMBlockBuffer?
        guard CMBlockBufferCreateWithMemoryBlock(
            allocator: nil,
            memoryBlock: nil,
            blockLength: byteCount,
            blockAllocator: nil,
            customBlockSource: nil,
            offsetToData: 0,
            dataLength: byteCount,
            flags: 0,
            blockBufferOut: &blockBuffer
        ) == kCMBlockBufferNoErr, let blockBuffer else { return nil }

        let copied = data.withUnsafeBytes { (raw: UnsafeRawBufferPointer) -> OSStatus in
            guard let base = raw.baseAddress else { return -1 }
            return CMBlockBufferReplaceDataBytes(
                with: base,
                blockBuffer: blockBuffer,
                offsetIntoDestination: 0,
                dataLength: byteCount
            )
        }
        guard copied == kCMBlockBufferNoErr else { return nil }

        var timing = CMSampleTimingInfo(
            duration: CMTime(value: 1, timescale: Int32(sampleRate)),
            presentationTimeStamp: CMTime(value: 0, timescale: Int32(sampleRate)),
            decodeTimeStamp: CMTime.invalid
        )
        var sampleBuffer: CMSampleBuffer?
        guard CMSampleBufferCreate(
            allocator: nil,
            dataBuffer: blockBuffer,
            dataReady: true,
            makeDataReadyCallback: nil,
            refcon: nil,
            formatDescription: formatDescription,
            sampleCount: frames,
            sampleTimingEntryCount: 1,
            sampleTimingArray: &timing,
            sampleSizeEntryCount: 0,
            sampleSizeArray: nil,
            sampleBufferOut: &sampleBuffer
        ) == noErr, let sampleBuffer else { return nil }
        return sampleBuffer
    }

    static func testCMSampleBufferPaths(_ check: (Bool, String) -> Void) {
        let expectedTotal = 32000
        let tolerance = 3200
        let expectedNormRMS = 0.5 / sqrt(2.0)
        let rmsTolerance = 0.20
        let minCorrelation = 0.95

        func runTest(
            name: String,
            sampleRate: Double,
            channels: Int,
            isFloat: Bool,
            isBigEndian: Bool,
            isNonInterleaved: Bool,
            expectedRMS: Double? = nil,
            leftChannelOnly: Bool = false
        ) {
            guard let sampleBuffer = makeTestSampleBuffer(
                sampleRate: sampleRate,
                channels: channels,
                isFloat: isFloat,
                isBigEndian: isBigEndian,
                isNonInterleaved: isNonInterleaved,
                leftChannelOnly: leftChannelOnly
            ) else {
                check(false, "\(name) 构造 CMSampleBuffer 失败")
                return
            }

            let converter = PCMConverter()
            let frames = converter.append(sampleBuffer)

            var total = 0
            var all3200 = true
            for fr in frames {
                total += fr.count
                if fr.count != 3200 { all3200 = false }
            }
            check(all3200, "\(name) 每帧 3200 字节")
            check(!frames.isEmpty, "\(name) append 有帧")
            check(abs(total - expectedTotal) <= tolerance,
                  "\(name) 总字节 ≈ 32000（实际 \(total)）")

            let outputData = Data(frames.joined())
            let outputNormRMS = rmsInt16DataNormalized(outputData)
            let targetRMS = expectedRMS ?? expectedNormRMS
            let relDiff = targetRMS > 0 ? abs(outputNormRMS - targetRMS) / targetRMS : 0
            check(relDiff < rmsTolerance, "\(name) 归一化 RMS 相差 <20%（预期 \(targetRMS)，实际 \(outputNormRMS)，相对差 \(relDiff)）")

            let corr = maxNormalizedCrossCorrelation(output: outputData)
            check(corr >= minCorrelation, "\(name) 波形相关性 ≥ 0.95（实际 \(corr)）")
        }

        // 原有三组测试
        runTest(name: "44.1kHz立体声Int16大端交错", sampleRate: 44100, channels: 2, isFloat: false, isBigEndian: true, isNonInterleaved: false)
        runTest(name: "48kHz立体声Int16小端交错", sampleRate: 48000, channels: 2, isFloat: false, isBigEndian: false, isNonInterleaved: false)
        runTest(name: "48kHz立体声Float32非交错", sampleRate: 48000, channels: 2, isFloat: true, isBigEndian: false, isNonInterleaved: true)

        // 新增：左声道 440Hz、右声道静音的 48kHz Int16 小端交错
        // 预期：混音后单声道幅度减半 → RMS = (0.5/√2)/2 = 0.5/(2√2) = 0.5/2.828 ≈ 0.1768
        let leftOnlyExpectedRMS = 0.5 / sqrt(2.0)  // PCMConverter 取左声道，不做平均混音
        runTest(
            name: "48kHz左声道440Hz右声道静音Int16小端交错",
            sampleRate: 48000,
            channels: 2,
            isFloat: false,
            isBigEndian: false,
            isNonInterleaved: false,
            expectedRMS: leftOnlyExpectedRMS,
            leftChannelOnly: true
        )
    }

    static func rmsInt16DataNormalized(_ data: Data) -> Double {
        let count = data.count / 2
        guard count > 0 else { return 0 }
        var sum = 0.0
        data.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
            guard let base = raw.baseAddress else { return }
            let p = base.assumingMemoryBound(to: Int16.self)
            for i in 0..<count {
                let v = Double(p[i]) / 32768.0
                sum += v * v
            }
        }
        return (sum / Double(count)).squareRoot()
    }

    /// 归一化互相关：在 ±maxShift 范围内寻找最大相关性
    /// 输入：输出波形与同频率理想正弦波（16kHz 采样）
    /// 返回：最大相关性系数（-1 到 1），要求 ≥ 0.95
    static func maxNormalizedCrossCorrelation(
        output: Data,
        expectedFrequency: Double = 440.0,
        outputSampleRate: Double = 16000.0,
        maxShift: Int = 40
    ) -> Double {
        let count = output.count / 2
        guard count > 0 else { return 0 }

        var outputSamples: [Double] = []
        outputSamples.reserveCapacity(count)
        output.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
            guard let base = raw.baseAddress else { return }
            let p = base.assumingMemoryBound(to: Int16.self)
            for i in 0..<count {
                outputSamples.append(Double(p[i]) / 32768.0)
            }
        }

        // 生成理想正弦波
        var idealSamples: [Double] = []
        idealSamples.reserveCapacity(count)
        for i in 0..<count {
            let t = Double(i) / outputSampleRate
            idealSamples.append(sin(2.0 * Double.pi * expectedFrequency * t))
        }

        // 归一化
        let outputMean = outputSamples.reduce(0, +) / Double(count)
        let idealMean = idealSamples.reduce(0, +) / Double(count)

        var outputCentered: [Double] = []
        var idealCentered: [Double] = []
        outputCentered.reserveCapacity(count)
        idealCentered.reserveCapacity(count)
        for i in 0..<count {
            outputCentered.append(outputSamples[i] - outputMean)
            idealCentered.append(idealSamples[i] - idealMean)
        }

        let outputNorm = (outputCentered.map { $0 * $0 }.reduce(0, +)).squareRoot()
        let idealNorm = (idealCentered.map { $0 * $0 }.reduce(0, +)).squareRoot()

        guard outputNorm > 0 && idealNorm > 0 else { return 0 }

        var maxCorr = 0.0
        for shift in -maxShift...maxShift {
            var corr = 0.0
            let startOut = max(0, shift)
            let startIdeal = max(0, -shift)
            let len = min(count - startOut, count - startIdeal)
            guard len > 100 else { continue } // 需要足够重叠

            for k in 0..<len {
                corr += outputCentered[startOut + k] * idealCentered[startIdeal + k]
            }
            corr /= outputNorm * idealNorm
            if corr > maxCorr { maxCorr = corr }
        }
        return maxCorr
    }

    /// 生成 16kHz 下的理想 440Hz 正弦波用于对比
    static func generateIdealSine16k(frames: Int, amplitude: Double = 1.0) -> [Double] {
        var samples: [Double] = []
        samples.reserveCapacity(frames)
        for i in 0..<frames {
            let t = Double(i) / 16000.0
            samples.append(amplitude * sin(2.0 * Double.pi * 440.0 * t))
        }
        return samples
    }

    static func extractFrames(from sampleBuffer: CMSampleBuffer, startFrame: Int, frameCount: Int) -> CMSampleBuffer? {
        guard let formatDescription = CMSampleBufferGetFormatDescription(sampleBuffer) else { return nil }
        let duration = CMSampleBufferGetDuration(sampleBuffer)
        let presTime = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)
        let timescale = duration.timescale
        let timing = CMSampleTimingInfo(
            duration: CMTime(value: Int64(frameCount), timescale: timescale),
            presentationTimeStamp: CMTime(value: Int64(startFrame), timescale: presTime.timescale),
            decodeTimeStamp: CMTime.invalid
        )
        var newBuffer: CMSampleBuffer?
        let status = CMSampleBufferCreateCopyWithNewTiming(
            allocator: nil,
            sampleBuffer: sampleBuffer,
            sampleTimingEntryCount: 1,
            sampleTimingArray: [timing],
            sampleBufferOut: &newBuffer
        )
        guard status == noErr, let newBuffer else { return nil }

        let totalFrames = CMSampleBufferGetNumSamples(newBuffer)
        guard totalFrames >= frameCount else { return newBuffer }

        let duration2 = CMSampleBufferGetDuration(newBuffer)
        let presTime2 = CMSampleBufferGetPresentationTimeStamp(newBuffer)
        let timescale2 = duration2.timescale
        var timing2 = CMSampleTimingInfo(
            duration: CMTime(value: Int64(frameCount), timescale: timescale2),
            presentationTimeStamp: CMTime(value: 0, timescale: presTime2.timescale),
            decodeTimeStamp: CMTime.invalid
        )
        var finalBuffer: CMSampleBuffer?
        let status2 = CMSampleBufferCreate(
            allocator: nil,
            dataBuffer: CMSampleBufferGetDataBuffer(newBuffer),
            dataReady: true,
            makeDataReadyCallback: nil,
            refcon: nil,
            formatDescription: formatDescription,
            sampleCount: frameCount,
            sampleTimingEntryCount: 1,
            sampleTimingArray: &timing2,
            sampleSizeEntryCount: 0,
            sampleSizeArray: nil,
            sampleBufferOut: &finalBuffer
        )
        guard status2 == noErr, let finalBuffer else { return nil }
        return finalBuffer
    }
}
