import AVFoundation

// 极简省电版的音频来源契约。采集（Capture/）和识别/界面两边只通过它对接。
// buffers 按到达顺序产出，格式在 start() 之后固定为 format；stop() 后流结束。
protocol AudioSource: AnyObject {
    var format: AVAudioFormat { get }
    func start() throws -> AsyncStream<AVAudioPCMBuffer>
    func stop()
}
