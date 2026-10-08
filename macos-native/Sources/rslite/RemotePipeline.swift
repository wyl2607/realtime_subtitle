@preconcurrency import AVFoundation
import Foundation

// 远程识别：本机只抓声音和显示，识别/翻译在 Mac mini 服务端完成（协议 v1）。
// 任何一步连不上远程都回退到本机 Pipeline，字幕不中断；验证版不自动重连。

/// 两种识别后端的共同接口。main.swift 按参数二选一，界面不关心背后是远程还是本机。
@available(macOS 27.0, *)
protocol SubtitleEngine: AnyObject, Sendable {
    func start() async throws
    func pause() async
    func resume() async
    func stop() async
    func waitUntilFinished() async
}

// 只补 conformance，Pipeline 自身行为不变。
@available(macOS 27.0, *)
extension Pipeline: SubtitleEngine {}

/// 当前识别在哪里进行。界面菜单栏和 headless 的 status 行都用这个标签。
enum SubtitleMode: Sendable {
    case connecting
    case remote
    case local

    var label: String {
        switch self {
        case .connecting: return "连接中"
        case .remote: return "远程"
        case .local: return "本机"
        }
    }
}

struct RemoteConfig: Sendable {
    let url: URL
    /// nil 表示读不到 token 文件。按远程不可用处理（回退本机），不直接退出。
    let token: String?
    let tokenPath: String
    /// 回退本机时使用的配置，与不带 --remote 时的本地识别完全相同。
    let base: PipelineConfig
}

/// 读取 token 文件。内容永不打印、不写日志；权限不是 600 只警告，不拒绝运行。
enum RemoteTokenFile {
    static func read(path: String) -> String? {
        let expanded = (path as NSString).expandingTildeInPath
        guard let data = FileManager.default.contents(atPath: expanded),
              let text = String(data: data, encoding: .utf8)?
                .trimmingCharacters(in: .whitespacesAndNewlines),
              !text.isEmpty
        else {
            return nil
        }
        if let attributes = try? FileManager.default.attributesOfItem(atPath: expanded),
           let permissions = attributes[.posixPermissions] as? NSNumber,
           permissions.intValue & 0o777 != 0o600 {
            let current = String(permissions.intValue & 0o777, radix: 8)
            fputs("warning: token 文件权限是 \(current)，建议执行 chmod 600 \(expanded)\n", stderr)
        }
        return text
    }
}

/// 远程不可用的原因。reason 直接显示给用户，所以写成人话。
private enum RemoteUnavailable: Error {
    case noToken(String)
    case unauthorized
    case busy
    case timeout
    case connection(String)

    var reason: String {
        switch self {
        case .noToken(let path): return "读不到 token 文件 \(path)"
        case .unauthorized: return "鉴权失败（token 不对）"
        case .busy: return "服务端忙（busy）"
        case .timeout: return "5 秒内没有 ready"
        case .connection(let message): return "连接失败：\(message)"
        }
    }
}

/// 只能打开一次的门。open 之后所有 wait 立即返回；用来把"远程结束""本机结束""超时"汇到一处。
private final class OneShotGate: @unchecked Sendable {
    private let lock = NSLock()
    private var isOpen = false
    private var waiters: [CheckedContinuation<Void, Never>] = []

    func open() {
        let pending = lock.withLock { () -> [CheckedContinuation<Void, Never>] in
            guard !isOpen else { return [] }
            isOpen = true
            let all = waiters
            waiters = []
            return all
        }
        pending.forEach { $0.resume() }
    }

    func wait() async {
        await withCheckedContinuation { continuation in
            let alreadyOpen = lock.withLock { () -> Bool in
                if isOpen { return true }
                waiters.append(continuation)
                return false
            }
            if alreadyOpen {
                continuation.resume()
            }
        }
    }
}

/// 盯住 WebSocket 是否真的关闭。cancel(with:) 只是排队发送 close 帧，
/// 进程若在发送前退出，服务端只会看到异常断开，收不到 1000。
private final class SocketCloseWatcher: NSObject, URLSessionWebSocketDelegate, URLSessionTaskDelegate, @unchecked Sendable {
    /// 对端发来的 close 帧已到（它的 code 是 busy 判定的依据）。
    let closeFrameReceived = OneShotGate()
    /// 连接已经结束（正常关闭或出错），用来判断关闭是否完成。
    let finished = OneShotGate()
    private let lock = NSLock()
    private var code: Int?

    var remoteCloseCode: Int? {
        lock.withLock { code }
    }

    func urlSession(
        _ session: URLSession,
        webSocketTask: URLSessionWebSocketTask,
        didCloseWith closeCode: URLSessionWebSocketTask.CloseCode,
        reason: Data?
    ) {
        lock.withLock { code = Self.effectiveCode(closeCode, reason) }
        closeFrameReceived.open()
        finished.open()
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        finished.open()
    }

    /// 实测 URLSession 收到服务端 1013 时 closeCode 报成 1005，而 reason 载荷的前两个字节
    /// 才是真正的 code（大端，后面是文字原因，如 "busy"）。这里以载荷为准，免得 busy 被误判成普通断线。
    private static func effectiveCode(_ closeCode: URLSessionWebSocketTask.CloseCode, _ reason: Data?) -> Int {
        guard closeCode.rawValue == 1005, let reason, reason.count >= 2 else {
            return closeCode.rawValue
        }
        return Int(reason[reason.startIndex]) << 8 | Int(reason[reason.startIndex + 1])
    }
}

@available(macOS 27.0, *)
final class RemotePipeline: SubtitleEngine, @unchecked Sendable {
    // 16kHz 单声道 s16le。100ms 对应 1600 帧 × 2 字节 = 3200 字节，和协议约定一致。
    private static let wireFormat = AVAudioFormat(
        commonFormat: .pcmFormatInt16, sampleRate: 16_000, channels: 1, interleaved: true
    )!
    private static let frameBytes = 3200
    private static let readyTimeout: Duration = .seconds(5)
    private static let tailTimeout: Duration = .seconds(5)
    private static let closeTimeout: Duration = .seconds(2)
    private static let flushMessage = #"{"type":"flush"}"#

    private let config: RemoteConfig
    private let callbacks: PipelineCallbacks
    private let onMode: @Sendable (SubtitleMode) -> Void
    private let finished = OneShotGate()
    private let lock = NSLock()

    // 以下状态只在 lock 内读写。
    private var ws: URLSessionWebSocketTask?
    private var session: URLSession?
    private var closeWatcher: SocketCloseWatcher?
    private var source: AudioSource?
    private var pumpTask: Task<Void, Never>?
    private var local: Pipeline?
    private var isPaused = false
    private var isStopped = false
    private var isFellBack = false
    // 我们主动关连接之后，receive 报的错不算断线，不能触发回退。
    private var isRemoteClosed = false
    // 文件读完进入尾句阶段。此时断线不能再回退到本机，否则本地会从头重读整个文件。
    private var isInputEnded = false
    private var pendingTranslations = Set<Int>()
    private var finalsAfterFlush = 0
    private var tailGate: OneShotGate?

    init(
        config: RemoteConfig,
        callbacks: PipelineCallbacks,
        onMode: @escaping @Sendable (SubtitleMode) -> Void
    ) {
        self.config = config
        self.callbacks = callbacks
        self.onMode = onMode
    }

    func start() async throws {
        locked {
            isStopped = false
            isPaused = false
        }
        onMode(.connecting)

        let socket: URLSessionWebSocketTask
        do {
            socket = try await connect()
        } catch let error as RemoteUnavailable {
            try await fallBack(reason: error.reason)
            return
        }

        locked { ws = socket }
        onMode(.remote)
        callbacks.onStatus("远程已连接，识别在服务端进行")
        Task { await self.receiveLoop(socket) }

        do {
            try startAudio(socket)
        } catch {
            // 音频来源本身起不来（比如文件不存在）回退本机也不会好，直接报错。
            await closeRemote(code: .goingAway)
            throw error
        }
    }

    func pause() async {
        let shouldPause = locked { () -> Bool in
            guard !isPaused, !isStopped else { return false }
            isPaused = true
            return true
        }
        guard shouldPause else { return }

        let local = locked { self.local }
        if let local {
            await local.pause()
            return
        }

        // 先标记暂停，再停来源：pump 结束时看到 isPaused 就不会误当成"文件读完"。
        let source = locked { self.source }
        let pump = locked { pumpTask }
        source?.stop()
        await pump?.value
        let socket = locked { ws }
        try? await socket?.send(.string(Self.flushMessage))
        callbacks.onStatus("已暂停")
    }

    func resume() async {
        let shouldResume = locked { () -> Bool in
            guard isPaused, !isStopped else { return false }
            isPaused = false
            return true
        }
        guard shouldResume else { return }

        let local = locked { self.local }
        if let local {
            await local.resume()
            return
        }

        guard let socket = locked({ ws }) else { return }
        do {
            try startAudio(socket)
            callbacks.onStatus("已继续")
        } catch {
            callbacks.onStatus("恢复失败：\(error.localizedDescription)")
        }
    }

    func stop() async {
        locked { isStopped = true }
        let source = locked { self.source }
        let pump = locked { pumpTask }
        source?.stop()
        await pump?.value

        let local = locked { self.local }
        if let local {
            await local.stop()
        }
        await closeRemote(code: .normalClosure)
        finished.open()
    }

    func waitUntilFinished() async {
        await finished.wait()
    }

    // MARK: - 连接与握手

    private func connect() async throws -> URLSessionWebSocketTask {
        guard let token = config.token else {
            throw RemoteUnavailable.noToken(config.tokenPath)
        }

        var request = URLRequest(url: config.url)
        // token 只放在请求头里，不进 URL、不进日志。
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")

        let sessionConfiguration = URLSessionConfiguration.ephemeral
        // 暂停时音频静默，连接可能几分钟没有任何帧。默认 60 秒请求超时会把会话切断，所以放宽。
        sessionConfiguration.timeoutIntervalForRequest = 24 * 60 * 60
        let watcher = SocketCloseWatcher()
        let session = URLSession(configuration: sessionConfiguration, delegate: watcher, delegateQueue: nil)
        locked {
            self.session = session
            closeWatcher = watcher
        }
        let socket = session.webSocketTask(with: request)
        socket.resume()

        let hello: [String: Any] = [
            "type": "hello",
            "v": 1,
            "src": Self.languageCode(config.base.sourceLocaleID),
            "dst": Self.languageCode(config.base.targetLanguageID),
            "sample_rate": 16_000,
            "format": "s16le",
        ]
        // hello 发送失败不在这里报：握手失败和 busy 的真实原因由接收端的错误给出。
        try? await socket.send(.string(Self.jsonString(hello)))

        do {
            try await awaitReady(socket)
        } catch {
            let unavailable = await classify(error, socket)
            socket.cancel(with: .goingAway, reason: nil)
            throw unavailable
        }
        return socket
    }

    private func awaitReady(_ socket: URLSessionWebSocketTask) async throws {
        try await withThrowingTaskGroup(of: Void.self) { group in
            group.addTask {
                while true {
                    let message = try await socket.receive()
                    if case .string(let text) = message,
                       Self.parseEvent(text)?["ev"] as? String == "ready" {
                        return
                    }
                }
            }
            group.addTask {
                do {
                    try await Task.sleep(for: Self.readyTimeout)
                } catch {
                    // 已经收到 ready，超时计时器被取消，正常退出。
                    return
                }
                // 先关 socket，让阻塞在 receive 上的那个子任务也能返回，组才能结束。
                socket.cancel(with: .goingAway, reason: nil)
                throw RemoteUnavailable.timeout
            }
            try await group.next()
            group.cancelAll()
        }
    }

    private func classify(_ error: Error, _ socket: URLSessionWebSocketTask) async -> RemoteUnavailable {
        if let unavailable = error as? RemoteUnavailable {
            return unavailable
        }
        if let http = socket.response as? HTTPURLResponse, http.statusCode == 401 {
            return .unauthorized
        }
        // 1013 的 close 帧可能晚于 receive 报出的错误到达，所以最多等 1 秒再判定。
        // 不能直接读 socket.closeCode：那时常常还是未设置，结果会误报成普通连接失败。
        if let watcher = locked({ closeWatcher }) {
            let timeout = Task {
                try? await Task.sleep(for: .seconds(1))
                watcher.closeFrameReceived.open()
            }
            await watcher.closeFrameReceived.wait()
            timeout.cancel()
            // 服务端在握手后以 1013（Try Again Later）拒绝，表示已有其他客户端在用
            if watcher.remoteCloseCode == 1013 || socket.closeCode.rawValue == 1013 {
                return .busy
            }
        }
        return .connection(error.localizedDescription)
    }

    // MARK: - 音频上行

    private func startAudio(_ socket: URLSessionWebSocketTask) throws {
        let source = try makeAudioSource(config.base.sourceSpec)
        let stream = try source.start()
        guard let converter = AVAudioConverter(from: source.format, to: Self.wireFormat) else {
            source.stop()
            throw PipelineError.unavailable("无法创建音频格式转换器")
        }
        // 同一个音频来源的生命周期内复用一个转换器，保持重采样状态连续。
        // 直接强引用 self：pump 随音频流结束而结束，不会长期持有对象。
        let pump = Task {
            await self.pump(stream: stream, converter: converter, socket: socket)
        }
        locked {
            self.source = source
            self.pumpTask = pump
        }
    }

    private func pump(
        stream: AsyncStream<AVAudioPCMBuffer>,
        converter: AVAudioConverter,
        socket: URLSessionWebSocketTask
    ) async {
        var pending = Data()
        do {
            for await buffer in stream {
                pending.append(try Self.convert(buffer, using: converter))
                while pending.count >= Self.frameBytes {
                    let frame = Data(pending.prefix(Self.frameBytes))
                    pending.removeFirst(Self.frameBytes)
                    try await socket.send(.data(frame))
                }
            }
            if !pending.isEmpty {
                try await socket.send(.data(pending))
            }
        } catch {
            await handleRemoteLost()
            return
        }

        // 流自然结束只会出现在文件来源读完时。暂停、停止、回退都是我们先 stop 来源，
        // 这三种情况不能当成"读完"，否则会提前结束进程、把本机识别截断。
        let shouldFinish = locked { !isPaused && !isStopped && !isFellBack }
        if shouldFinish {
            await finishInput()
        }
    }

    private static func convert(_ buffer: AVAudioPCMBuffer, using converter: AVAudioConverter) throws -> Data {
        let ratio = wireFormat.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 1024
        guard let output = AVAudioPCMBuffer(pcmFormat: wireFormat, frameCapacity: capacity) else {
            throw PipelineError.unavailable("无法分配转换后的音频缓冲区")
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
            return buffer
        }
        if let convertError {
            throw convertError
        }
        if status == .error {
            throw PipelineError.unavailable("音频格式转换失败")
        }
        guard output.frameLength > 0, let channel = output.int16ChannelData else {
            return Data()
        }
        return Data(bytes: channel[0], count: Int(output.frameLength) * MemoryLayout<Int16>.size)
    }

    /// 文件读完：发 flush 让服务端定稿尾句，等最后一条译文（或 5 秒超时）再结束。
    private func finishInput() async {
        let gate = OneShotGate()
        let socket = locked { () -> URLSessionWebSocketTask? in
            isInputEnded = true
            finalsAfterFlush = 0
            tailGate = gate
            return ws
        }
        try? await socket?.send(.string(Self.flushMessage))

        let timeout = Task {
            try? await Task.sleep(for: Self.tailTimeout)
            gate.open()
        }
        await gate.wait()
        timeout.cancel()

        let stopped = locked { isStopped }
        if !stopped {
            callbacks.onFinished()
        }
        await closeRemote(code: .normalClosure)
        finished.open()
    }

    // MARK: - 下行事件

    private func receiveLoop(_ socket: URLSessionWebSocketTask) async {
        while true {
            do {
                let message = try await socket.receive()
                if case .string(let text) = message {
                    handle(text)
                }
            } catch {
                await handleRemoteLost()
                return
            }
        }
    }

    private func handle(_ text: String) {
        guard let event = Self.parseEvent(text), let name = event["ev"] as? String else {
            return
        }
        let body = event["text"] as? String ?? ""
        let id = event["id"] as? Int

        switch name {
        case "volatile":
            callbacks.onVolatile(body)
        case "final":
            guard let id else { return }
            locked {
                pendingTranslations.insert(id)
                if tailGate != nil {
                    finalsAfterFlush += 1
                }
            }
            callbacks.onFinal(id, body)
        case "translation":
            guard let id else { return }
            locked {
                pendingTranslations.remove(id)
                // 必须见过 flush 之后的 final 才算尾句到齐；否则上一句的译文先到会误判结束。
                if finalsAfterFlush > 0, pendingTranslations.isEmpty {
                    tailGate?.open()
                }
            }
            callbacks.onTranslation(id, body)
        case "status":
            callbacks.onStatus(body)
        default:
            // draft 按协议忽略；ready 只在握手时用到
            return
        }
    }

    // MARK: - 回退与收尾

    /// 连接中途断开（服务端被杀、网络断）。只回退一次，之后不自动重连。
    private func handleRemoteLost() async {
        enum Action { case ignore, endInput, fallBack }
        let action = locked { () -> Action in
            if isStopped || isRemoteClosed || isFellBack { return .ignore }
            if isInputEnded { return .endInput }
            isFellBack = true
            return .fallBack
        }

        switch action {
        case .ignore:
            return
        case .endInput:
            // 文件已读完，只差尾句；让结束流程直接走完，不再回退。
            locked { tailGate }?.open()
        case .fallBack:
            stopRemoteAudio()
            await closeRemote(code: .goingAway)
            do {
                try await fallBack(reason: "连接中断")
            } catch {
                callbacks.onStatus("本机识别启动失败：\(error.localizedDescription)")
                finished.open()
            }
        }
    }

    private func fallBack(reason: String) async throws {
        locked { isFellBack = true }
        callbacks.onStatus("远程不可用：\(reason)，已切到本机识别")
        onMode(.local)

        let pipeline = Pipeline(config: config.base, callbacks: callbacks)
        locked { local = pipeline }
        try await pipeline.start()

        let paused = locked { isPaused }
        if paused {
            await pipeline.pause()
        }
        let finished = self.finished
        Task {
            await pipeline.waitUntilFinished()
            finished.open()
        }
    }

    private func stopRemoteAudio() {
        let (source, pump) = locked { (self.source, pumpTask) }
        source?.stop()
        pump?.cancel()
    }

    private func closeRemote(code: URLSessionWebSocketTask.CloseCode) async {
        let (socket, session, watcher) = locked { () -> (URLSessionWebSocketTask?, URLSession?, SocketCloseWatcher?) in
            isRemoteClosed = true
            let current = ws
            ws = nil
            return (current, self.session, closeWatcher)
        }
        socket?.cancel(with: code, reason: nil)
        // 等 URLSession 报告关闭完成（最多 2 秒），否则 main 紧接着退出进程，close 帧可能发不出去。
        if let watcher {
            let timeout = Task {
                try? await Task.sleep(for: Self.closeTimeout)
                watcher.finished.open()
            }
            await watcher.finished.wait()
            timeout.cancel()
        }
        session?.finishTasksAndInvalidate()
    }

    // MARK: - 小工具

    private func locked<T>(_ body: () -> T) -> T {
        lock.withLock(body)
    }

    private static func languageCode(_ localeID: String) -> String {
        localeID.split(separator: "-").first.map(String.init) ?? localeID
    }

    private static func jsonString(_ object: [String: Any]) -> String {
        guard let data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]),
              let text = String(data: data, encoding: .utf8)
        else {
            return "{}"
        }
        return text
    }

    private static func parseEvent(_ text: String) -> [String: Any]? {
        guard let data = text.data(using: .utf8) else {
            return nil
        }
        return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
    }
}
