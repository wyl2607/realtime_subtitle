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
}
