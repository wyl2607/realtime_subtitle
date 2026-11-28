import AVFoundation
import AudioToolbox
import CoreMedia
import Foundation

/// 直接 PCM 入口的回归：麦克风 tap 给的是 AVAudioPCMBuffer，不能再依赖 CMSampleBuffer。
func runPCMConverterSelftest(_ check: (Bool, String) -> Void) {
    let converter = PCMConverter()

    // 0.25 秒 @ 48kHz = 12000 个 Float32 单声道采样；重采样后正好是 8000 字节。
    let first = converter.append(pcmBuffer: makeFloat32MonoBuffer(sampleCount: 12_000))
    check(first.count == 2, "48kHz Float32 单声道 0.25 秒应产出 2 帧（实际 \(first.count)）")
    check(first.allSatisfy { $0.count == PCMConverter.frameBytes }, "直接 PCM 入口每帧应为 3200 字节")

    // 首段恰好整除 100ms，余量为零；两段 0.05 秒分别积累半帧，第二段才吐出第 3 帧。
    // 这样同时钉住 pending 不会丢失、也不会提前吐出半帧的行为。
    let halfFrame = converter.append(pcmBuffer: makeFloat32MonoBuffer(sampleCount: 2_400))
    check(halfFrame.isEmpty, "首个 0.05 秒应留在 pending，不能吐出半帧")
    let third = converter.append(pcmBuffer: makeFloat32MonoBuffer(sampleCount: 2_400))
    check(third.count == 1, "第二个 0.05 秒应与 pending 合成第 3 帧（实际 \(third.count)）")
    check(third.first?.count == PCMConverter.frameBytes, "合成的第 3 帧应为 3200 字节")

    // 换耳机/蓝牙后 tap 的格式会变，而麦克风路由变化时不再重建字幕引擎（也就不重建转换器），
    // 同一个转换器必须能直接吃下新格式：0.2 秒 @ 16kHz Int16 双声道 → 2 帧。
    let afterRouteChange = converter.append(pcmBuffer: makeInt16StereoBuffer(sampleRate: 16_000, frames: 3_200))
    check(afterRouteChange.count == 2, "格式中途改变后同一转换器应继续出帧（实际 \(afterRouteChange.count)）")
    check(afterRouteChange.allSatisfy { $0.count == PCMConverter.frameBytes }, "格式改变后每帧仍为 3200 字节")

    // 奇数样本数不是完整的双声道帧：只转换完整帧，尾部一个样本明确丢弃。
    let evenSamples = Array(repeating: Int16(100), count: 3_200)
    let oddSamples = evenSamples + [Int16(30_000)]
    let evenResult = PCMConverter.convert(interleavedInt16: evenSamples, sampleRate: 16_000, channels: 2)
    let oddResult = PCMConverter.convert(interleavedInt16: oddSamples, sampleRate: 16_000, channels: 2)
    check(oddResult.count == PCMConverter.frameBytes && oddResult == evenResult,
          "奇数样本数应丢弃尾部半帧且只转换完整帧（实际 \(oddResult.count) 字节）")
    check(PCMConverter.convert(interleavedInt16: [Int16(100)], sampleRate: 16_000, channels: 2).isEmpty,
          "不足一帧的奇数样本应直接返回空数据")

    // 声明了 1600 帧但只提供一帧数据：必须整段丢弃，不能从截断 block buffer 读越界。
    if let truncated = makeSampleBuffer(frameCount: 1_600, channels: 2, byteCount: 4) {
        check(PCMConverter().append(truncated).isEmpty,
              "截断 CMSampleBuffer 应被丢弃且不越界（来源 bug：raw 字节数未校验）")
    } else {
        check(false, "截断 CMSampleBuffer 构造失败")
    }

    // 非交错双声道的两个平面都应按完整帧读取，不崩溃也不越界。
    if let nonInterleaved = makeSampleBuffer(frameCount: 1_600, channels: 2, byteCount: 6_400, nonInterleaved: true) {
        let frames = PCMConverter().append(nonInterleaved)
        check(frames.count == 1 && frames[0].count == PCMConverter.frameBytes,
              "非交错双声道完整数据应产出一帧 3200 字节（实际 \(frames.count) 帧）")
    } else {
        check(false, "非交错双声道 CMSampleBuffer 构造失败")
    }
}

private func makeInt16StereoBuffer(sampleRate: Double, frames: Int) -> AVAudioPCMBuffer {
    let format = AVAudioFormat(
        commonFormat: .pcmFormatInt16,
        sampleRate: sampleRate,
        channels: 2,
        interleaved: true
    )!
    let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frames))!
    buffer.frameLength = AVAudioFrameCount(frames)
    let samples = buffer.int16ChannelData![0]
    for index in 0..<(frames * 2) {
        samples[index] = Int16(index % 97) * 100
    }
    return buffer
}

private func makeFloat32MonoBuffer(sampleCount: Int) -> AVAudioPCMBuffer {
    let format = AVAudioFormat(
        commonFormat: .pcmFormatFloat32,
        sampleRate: 48_000,
        channels: 1,
        interleaved: false
    )!
    let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(sampleCount))!
    buffer.frameLength = AVAudioFrameCount(sampleCount)
    let samples = buffer.floatChannelData![0]
    for index in 0..<sampleCount {
        samples[index] = Float(index % 97) / 97.0
    }
    return buffer
}

private func makeSampleBuffer(
    frameCount: Int,
    channels: Int,
    byteCount: Int,
    nonInterleaved: Bool = false
) -> CMSampleBuffer? {
    var asbd = AudioStreamBasicDescription(
        mSampleRate: 16_000,
        mFormatID: kAudioFormatLinearPCM,
        mFormatFlags: kAudioFormatFlagIsSignedInteger | kAudioFormatFlagIsPacked |
            (nonInterleaved ? kAudioFormatFlagIsNonInterleaved : 0),
        mBytesPerPacket: UInt32(nonInterleaved ? 2 : 2 * channels),
        mFramesPerPacket: 1,
        mBytesPerFrame: UInt32(nonInterleaved ? 2 : 2 * channels),
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

    let samples = [Int16](repeating: 100, count: (byteCount + 1) / 2)
    let copied = samples.withUnsafeBytes { raw -> OSStatus in
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
        duration: CMTime(value: 1, timescale: 16_000),
        presentationTimeStamp: .zero,
        decodeTimeStamp: .invalid
    )
    var sampleBuffer: CMSampleBuffer?
    guard CMSampleBufferCreate(
        allocator: nil,
        dataBuffer: blockBuffer,
        dataReady: true,
        makeDataReadyCallback: nil,
        refcon: nil,
        formatDescription: formatDescription,
        sampleCount: frameCount,
        sampleTimingEntryCount: 1,
        sampleTimingArray: &timing,
        sampleSizeEntryCount: 0,
        sampleSizeArray: nil,
        sampleBufferOut: &sampleBuffer
    ) == noErr else { return nil }
    return sampleBuffer
}
