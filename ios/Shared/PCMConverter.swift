import AVFoundation
import AudioToolbox
import CoreMedia
import Foundation

/// 把任意采样率/声道/格式（Int16 大小端、Float32、交错或非交错）的 CMSampleBuffer
/// 转成 16kHz 单声道 s16le 小端 PCM，并按 100ms（3200 字节）攒帧吐出。
final class PCMConverter {
    /// 一帧 = 100ms @ 16kHz 单声道 s16le = 1600 样本 × 2 字节。
    static let frameBytes = 3200

    private static let outputFormat = AVAudioFormat(
        commonFormat: .pcmFormatInt16,
        sampleRate: 16_000,
        channels: 1,
        interleaved: true
    )!

    private var pending = Data()
    private var cachedInputFormat: AVAudioFormat?
    private var cachedConverter: AVAudioConverter?

    /// 转换一帧 CMSampleBuffer 并吐出攒满的 3200 字节帧（0 或多帧）。
    func append(_ sampleBuffer: CMSampleBuffer) -> [Data] {
        guard let input = extractPCMBuffer(from: sampleBuffer) else {
            return []
        }
        let converted = convertToWire(input, format: input.format)
        guard !converted.isEmpty else {
            return []
        }
        pending.append(converted)
        var frames: [Data] = []
        while pending.count >= Self.frameBytes {
            frames.append(Data(pending.prefix(Self.frameBytes)))
            pending.removeFirst(Self.frameBytes)
        }
        return frames
    }

    /// 不依赖 CMSampleBuffer 的入口（自测用）：交错的 Int16 小端样本 → 16kHz 单声道 s16le。
    static func convert(interleavedInt16 samples: [Int16], sampleRate: Double, channels: Int) -> Data {
        let frames = samples.count / channels
        guard frames > 0, channels > 0,
              let format = AVAudioFormat(
                commonFormat: .pcmFormatInt16,
                sampleRate: sampleRate,
                channels: AVAudioChannelCount(channels),
                interleaved: true),
              let input = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frames)) else {
            return Data()
        }
        input.frameLength = AVAudioFrameCount(frames)
        samples.withUnsafeBytes { raw in
            guard let base = raw.baseAddress, let dst = input.int16ChannelData?[0] else { return }
            memcpy(dst, base, samples.count * MemoryLayout<Int16>.size)
        }
        let converter = PCMConverter()
        return converter.convertToWire(input, format: format)
    }

    private func convertToWire(_ input: AVAudioPCMBuffer, format: AVAudioFormat) -> Data {
        guard input.frameLength > 0, let converter = converter(for: format) else {
            return Data()
        }
        let ratio = Self.outputFormat.sampleRate / format.sampleRate
        let capacity = AVAudioFrameCount(Double(input.frameLength) * ratio) + 1024
        guard let output = AVAudioPCMBuffer(pcmFormat: Self.outputFormat, frameCapacity: capacity) else {
            return Data()
        }
        var didProvideInput = false
        var convertError: NSError?
        let status = converter.convert(to: output, error: &convertError) { _, outStatus in
            if didProvideInput {
                outStatus.pointee = .noDataNow
                return nil
            }
            didProvideInput = true
            outStatus.pointee = .haveData
            return input
        }
        if convertError != nil || status == .error {
            return Data()
        }
        guard output.frameLength > 0, let channel = output.int16ChannelData else {
            return Data()
        }
        return Data(bytes: channel[0], count: Int(output.frameLength) * MemoryLayout<Int16>.size)
    }

    private func converter(for format: AVAudioFormat) -> AVAudioConverter? {
        if let cachedInputFormat, let cachedConverter, sameFormat(cachedInputFormat, format) {
            return cachedConverter
        }
        guard let converter = AVAudioConverter(from: format, to: Self.outputFormat) else {
            return nil
        }
        cachedInputFormat = format
        cachedConverter = converter
        return converter
    }

    private func sameFormat(_ a: AVAudioFormat, _ b: AVAudioFormat) -> Bool {
        a.commonFormat == b.commonFormat &&
            a.sampleRate == b.sampleRate &&
            a.channelCount == b.channelCount &&
            a.isInterleaved == b.isInterleaved
    }

    private func extractPCMBuffer(from sampleBuffer: CMSampleBuffer) -> AVAudioPCMBuffer? {
        guard let formatDescription = CMSampleBufferGetFormatDescription(sampleBuffer),
              let asbdPointer = CMAudioFormatDescriptionGetStreamBasicDescription(formatDescription) else {
            return nil
        }
        let asbd = asbdPointer.pointee
        let flags = asbd.mFormatFlags
        let channels = Int(asbd.mChannelsPerFrame)
        let frames = Int(CMSampleBufferGetNumSamples(sampleBuffer))
        guard channels > 0, frames > 0 else {
            return nil
        }
        let isFloat = (flags & kAudioFormatFlagIsFloat) != 0
        let isBigEndian = (flags & kAudioFormatFlagIsBigEndian) != 0
        let isNonInterleaved = (flags & kAudioFormatFlagIsNonInterleaved) != 0
        // 只支持 Int16 / Float32，其它位深不接。
        guard (isFloat && asbd.mBitsPerChannel == 32) || (!isFloat && asbd.mBitsPerChannel == 16) else {
            return nil
        }

        let commonFormat: AVAudioCommonFormat = isFloat ? .pcmFormatFloat32 : .pcmFormatInt16
        guard let format = AVAudioFormat(
            commonFormat: commonFormat,
            sampleRate: asbd.mSampleRate,
            channels: AVAudioChannelCount(channels),
            interleaved: !isNonInterleaved),
            let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frames)) else {
            return nil
        }
        buffer.frameLength = AVAudioFrameCount(frames)

        guard let raw = sampleBufferData(sampleBuffer) else {
            return nil
        }

        let bytesPerSample = isFloat ? 4 : 2
        raw.withUnsafeBytes { (src: UnsafeRawBufferPointer) in
            guard let base = src.baseAddress else { return }
            let byteBase = base.assumingMemoryBound(to: UInt8.self)
            if isFloat {
                if isNonInterleaved {
                    guard let ptrs = buffer.floatChannelData else { return }
                    for ch in 0..<channels {
                        let dst = ptrs[ch]
                        for f in 0..<frames {
                            dst[f] = float32(from: byteBase, offset: ch * frames * bytesPerSample + f * bytesPerSample, bigEndian: isBigEndian)
                        }
                    }
                } else {
                    guard let dst = buffer.floatChannelData?[0] else { return }
                    for ch in 0..<channels {
                        for f in 0..<frames {
                            dst[f * channels + ch] = float32(from: byteBase, offset: f * channels * bytesPerSample + ch * bytesPerSample, bigEndian: isBigEndian)
                        }
                    }
                }
            } else {
                if isNonInterleaved {
                    guard let ptrs = buffer.int16ChannelData else { return }
                    for ch in 0..<channels {
                        let dst = ptrs[ch]
                        for f in 0..<frames {
                            dst[f] = int16(from: byteBase, offset: ch * frames * bytesPerSample + f * bytesPerSample, bigEndian: isBigEndian)
                        }
                    }
                } else {
                    guard let dst = buffer.int16ChannelData?[0] else { return }
                    for ch in 0..<channels {
                        for f in 0..<frames {
                            dst[f * channels + ch] = int16(from: byteBase, offset: f * channels * bytesPerSample + ch * bytesPerSample, bigEndian: isBigEndian)
                        }
                    }
                }
            }
        }
        return buffer
    }

    private func sampleBufferData(_ sampleBuffer: CMSampleBuffer) -> Data? {
        guard let blockBuffer = CMSampleBufferGetDataBuffer(sampleBuffer) else {
            return nil
        }
        let length = CMBlockBufferGetDataLength(blockBuffer)
        guard length > 0 else {
            return nil
        }
        var data = Data(count: length)
        let status = data.withUnsafeMutableBytes { (dst: UnsafeMutableRawBufferPointer) -> OSStatus in
            guard let base = dst.baseAddress else { return -1 }
            return CMBlockBufferCopyDataBytes(blockBuffer, atOffset: 0, dataLength: length, destination: base)
        }
        guard status == noErr else {
            return nil
        }
        return data
    }

    private func int16(from bytes: UnsafePointer<UInt8>, offset: Int, bigEndian: Bool) -> Int16 {
        let b0 = bytes[offset]
        let b1 = bytes[offset + 1]
        let value: UInt16 = bigEndian ? (UInt16(b0) << 8) | UInt16(b1) : UInt16(b0) | (UInt16(b1) << 8)
        return Int16(bitPattern: value)
    }

    private func float32(from bytes: UnsafePointer<UInt8>, offset: Int, bigEndian: Bool) -> Float {
        if bigEndian {
            let u = (UInt32(bytes[offset]) << 24) | (UInt32(bytes[offset + 1]) << 16) |
                (UInt32(bytes[offset + 2]) << 8) | UInt32(bytes[offset + 3])
            return Float(bitPattern: u)
        }
        let u = UInt32(bytes[offset]) | (UInt32(bytes[offset + 1]) << 8) |
            (UInt32(bytes[offset + 2]) << 16) | (UInt32(bytes[offset + 3]) << 24)
        return Float(bitPattern: u)
    }
}
