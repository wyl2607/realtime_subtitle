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
            cont.yield(buffer)
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
}
