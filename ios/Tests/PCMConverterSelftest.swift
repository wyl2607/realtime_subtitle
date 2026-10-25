import AVFoundation
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
