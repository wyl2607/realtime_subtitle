import AVFoundation

/// 系统默认输入设备（麦克风）。用 AVAudioEngine 的 inputNode 采集。
final class MicSource: AudioSource {
    private let engine = AVAudioEngine()
    private var continuation: AsyncStream<AVAudioPCMBuffer>.Continuation?
    private var _format: AVAudioFormat?

    var format: AVAudioFormat {
        guard let _format else { preconditionFailure("MicSource.format 只在 start() 之后有效") }
        return _format
    }

    func start() throws -> AsyncStream<AVAudioPCMBuffer> {
        precondition(continuation == nil, "MicSource 已经 start，需要先 stop")
        let input = engine.inputNode
        // 用输入节点的实际输出格式安装 tap；采样率或声道为 0 时说明没有可用输入设备
        // （或麦克风权限被拒），此时安装 tap 会直接抛 NSException，所以必须先检查
        let fmt = input.outputFormat(forBus: 0)
        guard fmt.sampleRate > 0, fmt.channelCount > 0 else {
            throw CaptureError(message: "麦克风输入格式无效（采样率 \(fmt.sampleRate)，声道 \(fmt.channelCount)）：没有可用的默认输入设备，或没有麦克风权限")
        }

        let (stream, cont) = AsyncStream<AVAudioPCMBuffer>.makeStream()
        input.installTap(onBus: 0, bufferSize: 4096, format: fmt) { buffer, _ in
            // AVAudioEngine 会复用 tap 交来的缓冲；不复制就跨异步边界，消费端读到的
            // 可能已被下一块覆盖或释放（HC-S01）。和 ProcessTapSource 一样先拷贝。
            guard let copy = Self.copy(buffer) else { return }
            cont.yield(copy)
        }
        do {
            engine.prepare()
            try engine.start()
        } catch {
            input.removeTap(onBus: 0)
            cont.finish()
            throw CaptureError(message: "启动麦克风采集失败：\(error.localizedDescription)")
        }
        continuation = cont
        _format = fmt
        return stream
    }

    func stop() {
        guard let cont = continuation else { return }
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        cont.finish()
        continuation = nil
    }

    /// 把 tap 缓冲按帧拷进新的 AVAudioPCMBuffer。在音频线程上运行，不做日志；结构对不上就丢这一块。
    static func copy(_ buffer: AVAudioPCMBuffer) -> AVAudioPCMBuffer? {
        guard buffer.frameLength > 0,
              let out = AVAudioPCMBuffer(pcmFormat: buffer.format, frameCapacity: buffer.frameLength)
        else { return nil }
        out.frameLength = buffer.frameLength
        let src = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: buffer.audioBufferList))
        let dst = UnsafeMutableAudioBufferListPointer(out.mutableAudioBufferList)
        guard src.count == dst.count else { return nil }
        for i in 0..<src.count {
            let n = Int(src[i].mDataByteSize)
            guard n <= Int(dst[i].mDataByteSize), let from = src[i].mData, let to = dst[i].mData else { return nil }
            memcpy(to, from, n)
        }
        return out
    }
}
