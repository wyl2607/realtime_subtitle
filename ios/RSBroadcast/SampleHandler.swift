import ReplayKit
import CoreMedia
import AudioToolbox
import os
import Darwin.Mach
import Foundation
import Speech
import Translation

class SampleHandler: RPBroadcastSampleHandler {
    private let logger = Logger(subsystem: "com.wyl2607.rsios.spike", category: "ext")

    private var blockCount: UInt64 = 0
    private var lastWriteTime: CFAbsoluteTime = 0
    private var lastStatusWriteTime: CFAbsoluteTime = 0
    private var lastLinesJSON: String = ""
    private var accumulatedRMS: Float = 0
    private var rmsSampleCount: Int = 0
    private var sampleRate: Double = 0
    private var channelCount: UInt32 = 0
    private var formatString: String = "unknown"
    private var formatFlags: UInt32 = 0
    private var isNonInterleaved: Bool = false

    private var converter: PCMConverter?
    private var session: NodeSession?
    private var localEngine: AnyObject?
    private var engineMode: String = "auto"
    private var autoFallback: AutoFallback?
    private var routingLocal = false
    private var localSentenceActive = false
    private var nodeName: String = ""
    private var readyReceived = false
    private var framesSent: UInt64 = 0
    private var finalsCount: Int = 0
    private var translationsCount: Int = 0
    private var preReadyDropped: UInt64 = 0
    private var lastStatusCode: String = ""
    private var fatalErrorOccurred = false
    private var fatalErrorReason: String = ""

    // 重连状态
    private var reconnectPolicy = ReconnectPolicy()
    private var reconnectCount: Int = 0
    private var nodeConfig: NodeConfig?

    private var recentLines: [RecentLine] = []
    private let maxRecentLines = 3
    private let stateQueue = DispatchQueue(label: "com.wyl2607.rsios.spike.state")
    private let connectionQueue = DispatchQueue(label: "com.wyl2607.rsios.spike.connection")
    private var broadcastActive = false

    /// 引擎会话代号：每次开一个新的引擎会话换一个新代号。
    ///
    /// 为什么需要：本机引擎的 id 每次启动都从 1 开始，节点会话的 id 也按会话重新计数。
    /// 主 App 靠 id 去重（同 id 视为同一句、译文后到就改写那一行），
    /// 只看 id 的话，后一个会话的第 1 句会把前一个会话的第 1 句覆盖掉，
    /// 用户看到的是字幕被改写而不是追加。带上代号之后主 App 用「代号 + id」当键。
    private var localSessionCode: String = ""
    private var nodeSessionCode: String = ""

    /// 本机这边原文已定稿、译文还没收尾的句子。
    /// 切回节点要等它们收尾，否则最后一句只剩原文。
    private var pendingLocalSettles: Set<Int> = []

    struct RecentLine: Codable {
        let id: Int
        let src: String
        var dst: String
        /// 产生这一句的引擎会话代号：主 App 用「代号 + id」去重。
        let session: String
    }

    override func broadcastStarted(withSetupInfo setupInfo: [String: NSObject]?) {
        logger.info("Broadcast started")
        writeStatus("started")

        engineMode = SharedKeychain.readEngineMode()

        if engineMode == "local" {
            if #available(iOS 26.0, *) {
                startLocalEngine()
            } else {
                writeStatus("last=local_needs_ios26")
                stateQueue.sync {
                    fatalErrorOccurred = true
                    fatalErrorReason = "fatal:local_needs_ios26"
                }
            }
            return
        }

        if engineMode == "auto" {
            startAutoMode()
            return
        }

        guard let json = SharedKeychain.readNodeConfig(),
              let data = json.data(using: .utf8),
              let config = try? JSONDecoder().decode(NodeConfig.self, from: data) else {
            writeStatus("no_config")
            return
        }

        nodeName = config.name
        nodeConfig = config
        converter = PCMConverter()
        stateQueue.sync {
            readyReceived = false
            framesSent = 0
            finalsCount = 0
            translationsCount = 0
            preReadyDropped = 0
            lastStatusCode = ""
            fatalErrorOccurred = false
            fatalErrorReason = ""
            recentLines = []
            session = nil
            broadcastActive = true
            reconnectPolicy = ReconnectPolicy()
            reconnectCount = 0
        }

        startPinnedSession(config)
    }

    override func broadcastFinished() {
        logger.info("Broadcast finished")
        
        if engineMode == "local" {
            stopLocalEngine()
            converter = nil
            writeStatus("finished")
            return
        }

        let currentSession = stateQueue.sync { () -> NodeSession? in
            broadcastActive = false
            // 决策器也要知道广播结束了：之后迟到的节点事件不许再拉起本机引擎。
            self.autoFallback?.broadcastEnded()
            let currentSession = session
            session = nil
            return currentSession
        }
        currentSession?.flush()
        currentSession?.close()
        if engineMode == "auto" {
            stopLocalEngine()
        }
        converter = nil
        writeStatus("finished")
    }

    // MARK: - 自动模式（C1/C2）：决策全在 AutoFallback，这里只做接线

    private var autoLocalEngineAvailable: Bool {
        if #available(iOS 26.0, *) { return true }
        return false
    }

    private func startAutoMode() {
        converter = PCMConverter()
        autoFallback = AutoFallback(nodeName: "电脑节点", localAvailable: autoLocalEngineAvailable)
        stateQueue.sync {
            readyReceived = false
            framesSent = 0
            finalsCount = 0
            translationsCount = 0
            preReadyDropped = 0
            lastStatusCode = ""
            fatalErrorOccurred = false
            fatalErrorReason = ""
            recentLines = []
            session = nil
            broadcastActive = true
            reconnectPolicy = ReconnectPolicy()
            reconnectCount = 0
            routingLocal = false
            localSentenceActive = false
            pendingLocalSettles.removeAll()
        }

        guard let json = SharedKeychain.readNodeConfig(),
              let data = json.data(using: .utf8),
              let config = try? JSONDecoder().decode(NodeConfig.self, from: data) else {
            // 没配对过节点：自动模式直接落到本机（iOS < 26 则只显示状态）。
            writeStatus("no_config")
            applyAutoDecision(handleAutoEvent(.nodeFailed))
            return
        }

        nodeName = config.name
        nodeConfig = config
        autoFallback?.nodeName = config.name
        startPinnedSession(config)
    }

    /// 在 stateQueue 上改 AutoFallback 并取回决策；动作本身在锁外执行，
    /// 因为启动/停止本机引擎也会进 stateQueue。
    private func handleAutoEvent(_ event: AutoFallbackEvent) -> AutoFallbackDecision? {
        stateQueue.sync {
            guard engineMode == "auto", var fallback = autoFallback else { return nil }
            let decision = fallback.handle(event)
            autoFallback = fallback
            return decision
        }
    }

    private func applyAutoDecision(_ decision: AutoFallbackDecision?) {
        guard let decision else { return }
        switch decision.action {
        case .keep:
            break
        case .startLocal:
            if #available(iOS 26.0, *) {
                startLocalEngine()
            }
        case .stopLocalAndSwitchToNode:
            stopLocalEngine()
            stateQueue.sync {
                routingLocal = false
                localSentenceActive = false
            }
        }
        if let status = decision.status {
            writeStatus("last=\(status)")
        }
    }

    private func stopLocalEngine() {
        if #available(iOS 26.0, *) {
            (localEngine as? LocalEngine)?.stop()
        }
        localEngine = nil
        stateQueue.sync {
            // 引擎没了，在途的译文不会再回调：把没收尾的句子清掉，
            // 否则「本机还忙」的判断会一直挂着。
            pendingLocalSettles.removeAll()
            localSentenceActive = false
        }
    }

    /// 新会话代号：随机十六进制，不含制表符（它会被写进存档文件的行尾）。
    private func newSessionCode() -> String {
        let hex = UUID().uuidString.replacingOccurrences(of: "-", with: "")
        return String(hex.prefix(12))
    }

    /// 本机是不是还有没收尾的句子（有句子在识别，或者译文还在路上）。
    /// 必须在 stateQueue 上读。
    private var localBusy: Bool {
        localSentenceActive || !pendingLocalSettles.isEmpty
    }

    @available(iOS 26.0, *)
    private func startLocalEngine() {
        // 自动回退时 converter 已经在跑节点链路：复用同一个实例，
        // 既避免和音频线程抢写，也保住重采样器的滤波状态。
        if converter == nil {
            converter = PCMConverter()
        }
        stateQueue.sync {
            // 只有显式「本机」模式才把节点就绪视为真；自动模式正在等节点，
            // 这里写 true 会让切回节点之前就把音频发给一个尚未 ready 的会话。
            readyReceived = engineMode == "local"
            framesSent = 0
            finalsCount = 0
            translationsCount = 0
            preReadyDropped = 0
            lastStatusCode = ""
            fatalErrorOccurred = false
            fatalErrorReason = ""
            recentLines = []
            broadcastActive = true
            routingLocal = true
            localSentenceActive = false
            pendingLocalSettles.removeAll()
            // 换引擎就是换会话：新的本机引擎的 id 又从 1 开始，
            // 不换代号的话会和上一个本机会话的同 id 句对上。
            localSessionCode = newSessionCode()
        }

        let engine = LocalEngine(
            onFinal: { [weak self] id, text in
                self?.handleLocalFinal(id: id, text: text)
            },
            onTranslation: { [weak self] id, text in
                self?.handleLocalTranslation(id: id, text: text)
            },
            onSentenceSettled: { [weak self] id in
                self?.handleLocalSettled(id: id)
            },
            onStatus: { [weak self] status in
                self?.handleLocalStatus(status)
            }
        )
        localEngine = engine
        Task {
            await engine.start()
        }
    }

    private func handleLocalFinal(id: Int, text: String) {
        stateQueue.sync {
            finalsCount += 1
            localSentenceActive = false
            pendingLocalSettles.insert(id)
            recentLines.append(RecentLine(id: id, src: text, dst: "", session: localSessionCode))
            if recentLines.count > maxRecentLines {
                recentLines.removeFirst()
            }
        }
        writeLatestLines()
        // 这里不发 .localSentenceFinalized：译文还在路上。
        // 早一步通知决策器，节点会立刻接手、本机引擎被停掉，
        // 在途的翻译随之取消，用户看到的就是「最后一句只有原文」。
        // 等译文落地（或翻译失败）后由 handleLocalSettled 通知。
    }

    /// 本机这一句收尾了（译文落地或翻译失败）：这时才可以交给节点。
    private func handleLocalSettled(id: Int) {
        let shouldReport = stateQueue.sync { () -> Bool in
            guard broadcastActive else { return false }
            guard pendingLocalSettles.remove(id) != nil else { return false }
            // 还有别的句子没收尾就继续等：一次只切一句，切早了会截断后面的半句。
            return pendingLocalSettles.isEmpty
        }
        guard shouldReport else { return }
        applyAutoDecision(handleAutoEvent(.localSentenceFinalized))
    }

    private func handleLocalTranslation(id: Int, text: String) {
        stateQueue.sync {
            translationsCount += 1
            if let idx = recentLines.firstIndex(where: { $0.id == id }) {
                recentLines[idx].dst = text
            }
        }
        writeLatestLines()
    }

    private func handleLocalStatus(_ status: String) {
        stateQueue.sync { lastStatusCode = status }
        logger.info("Local engine status: \(status)")
        if ["asr_assets_missing", "asr_unsupported", "analyzer_format_failed", "asr_failed", "asr_result_failed"].contains(where: { status.hasPrefix($0) }) {
            if engineMode == "auto" {
                // 本机引擎自己坏了：停掉它、把音频交还给节点重连，别让音频继续喂给死引擎。
                applyAutoDecision(handleAutoEvent(.localAvailabilityChanged(false)))
            } else {
                stateQueue.sync {
                    fatalErrorOccurred = true
                    fatalErrorReason = "fatal:\(status)"
                }
            }
        }
        writeStatus("last=\(status)")
    }

    override func processSampleBuffer(_ sampleBuffer: CMSampleBuffer, with sampleBufferType: RPSampleBufferType) {
        guard sampleBufferType == .audioApp else { return }

        blockCount += 1

        guard let formatDesc = CMSampleBufferGetFormatDescription(sampleBuffer),
              let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(formatDesc) else {
            return
        }

        if sampleRate == 0 {
            sampleRate = asbd.pointee.mSampleRate
            channelCount = asbd.pointee.mChannelsPerFrame
            formatFlags = asbd.pointee.mFormatFlags
            isNonInterleaved = (formatFlags & kAudioFormatFlagIsNonInterleaved) != 0
            let formatID = asbd.pointee.mFormatID
            let bitsPerChannel = asbd.pointee.mBitsPerChannel
            if formatID == kAudioFormatLinearPCM {
                if formatFlags & kAudioFormatFlagIsFloat != 0 {
                    formatString = "float32"
                } else if formatFlags & kAudioFormatFlagIsBigEndian != 0 {
                    formatString = "int16be"
                } else {
                    formatString = "int16le"
                }
            } else {
                formatString = "other"
            }
        }

        let rms = computeRMS(sampleBuffer: sampleBuffer, asbd: asbd.pointee)
        accumulatedRMS += rms
        rmsSampleCount += 1

        let now = CFAbsoluteTimeGetCurrent()
        if now - lastWriteTime >= 1.0 {
            accumulatedRMS = 0
            rmsSampleCount = 0
            lastWriteTime = now
        }
        if now - lastStatusWriteTime >= 5.0 {
            let avgRMS = rmsSampleCount > 0 ? accumulatedRMS / Float(rmsSampleCount) : 0
            let memMB = residentMemoryMB()
            let interleavedStr = isNonInterleaved ? "0" : "1"
            let state = stateQueue.sync {
                (readyReceived, framesSent, finalsCount, translationsCount, lastStatusCode, preReadyDropped, fatalErrorOccurred, fatalErrorReason, reconnectCount)
            }
            let readyStr = state.0 ? "1" : "0"
            let localActive = stateQueue.sync { routingLocal }
            let engineStr = (engineMode == "local" || localActive) ? "local" : "node"
            var status = "blocks=\(blockCount) rate=\(Int(sampleRate)) ch=\(channelCount) fmt=\(formatString) flags=\(formatFlags) ileave=\(interleavedStr) rms=\(String(format: "%.3f", avgRMS)) mem=\(memMB) engine=\(engineStr)"
            if engineMode != "local" && !localActive {
                status += " node=\(nodeName) ready=\(readyStr) sent=\(state.1) finals=\(state.2) tx=\(state.3) last=\(state.4.isEmpty ? "-" : state.4) drop=\(state.5) reconn=\(state.8)"
            } else {
                status += " finals=\(state.2) tx=\(state.3) last=\(state.4.isEmpty ? "-" : state.4)"
            }
            if state.6, !state.7.isEmpty {
                status += " \(state.7)"
            }
            writeStatus(status)
            lastStatusWriteTime = now
        }

        let routeLocal = stateQueue.sync { routingLocal }
        if engineMode == "local" || routeLocal {
            if #available(iOS 26.0, *) {
                guard let converter = converter, let engine = localEngine as? LocalEngine else { return }
                let fatal = stateQueue.sync { fatalErrorOccurred }
                guard !fatal else { return }

                let frames = converter.append(sampleBuffer)
                if !frames.isEmpty {
                    // 节点 ready 时靠这个标记判断“本机还有一句没定稿”，
                    // 避免切回节点把半句截断。
                    stateQueue.sync { localSentenceActive = true }
                    for frame in frames {
                        engine.append(pcm16k: frame)
                    }
                }
            }
            return
        }

        let currentSession = stateQueue.sync { session }
        guard let converter = converter, let session = currentSession else { return }
        let fatal = stateQueue.sync { fatalErrorOccurred }
        guard !fatal else { return }

        let frames = converter.append(sampleBuffer)
        for frame in frames {
            let ready = stateQueue.sync { readyReceived }
            if ready {
                session.send(pcm16k: frame)
                stateQueue.sync { framesSent += 1 }
            } else {
                stateQueue.sync { preReadyDropped += 1 }
            }
        }
    }

    private func startPinnedSession(_ config: NodeConfig) {
        connectionQueue.async { [weak self] in
            guard let self else { return }
            let pinnedURL: URL
            do {
                pinnedURL = try TailnetPin.pinned(config.sessionURL)
            } catch {
                let status: String
                if case TailnetPin.PinError.notTailnet = error {
                    status = "not_tailnet"
                } else {
                    status = "resolve_failed"
                }
                let shouldReport = self.stateQueue.sync { () -> Bool in
                    guard self.broadcastActive else { return false }
                    self.lastStatusCode = status
                    return true
                }
                guard shouldReport else { return }
                self.writeStatus("last=\(status)")
                // 解析失败多半是临时性的（还没连上 Tailscale、DNS 抖动），
                // 所以照旧按退避策略重连；不然节点这一路就永久断了
                // （本机还能顶上，但节点再也回不来，用户以为节点坏了）。
                self.scheduleReconnect(closeCode: 1006)
                if self.engineMode == "auto" {
                    // 节点这一轮没连上：交给决策器决定要不要落到本机。
                    self.applyAutoDecision(self.handleAutoEvent(.nodeFailed))
                }
                return
            }

            let newSession = NodeSession(
                url: pinnedURL,
                token: config.token,
                src: config.src,
                dst: config.dst,
                onEvent: { [weak self] event in
                    self?.handleNodeEvent(event)
                },
                onClose: { [weak self] code in
                    self?.handleNodeClose(code)
                }
            )
            let shouldStart = self.stateQueue.sync { () -> Bool in
                guard self.broadcastActive else { return false }
                self.session = newSession
                // 新的节点会话：节点的句子 id 重新从 1 计数，
                // 换一个新代号，主 App 才不会把它当成旧会话的同一句。
                self.nodeSessionCode = self.newSessionCode()
                return true
            }
            if shouldStart {
                newSession.start()
            }
        }
    }

    /// 按退避策略安排一次重连。节点断开、Tailnet 解析失败都走这里。
    /// - Returns: 是否真的排上了重连（false = 放弃重连或广播已结束）。
    @discardableResult
    private func scheduleReconnect(closeCode: Int) -> Bool {
        let plan = stateQueue.sync { () -> (TimeInterval?, NodeConfig?) in
            guard broadcastActive else { return (nil, nil) }
            guard !fatalErrorOccurred else { return (nil, nil) }
            guard let delay = reconnectPolicy.next(closeCode: closeCode) else { return (nil, nil) }
            reconnectCount += 1
            return (delay, nodeConfig)
        }

        guard let delay = plan.0, let config = plan.1 else { return false }

        // 重连前把 readyReceived 置 false（ready 之前的帧照旧丢弃计入 drop）
        stateQueue.sync { readyReceived = false }

        // 延迟后在 connectionQueue 上重新走 startPinnedSession（含 TailnetPin 校验）
        connectionQueue.asyncAfter(deadline: .now() + delay) { [weak self] in
            guard let self else { return }
            self.stateQueue.sync {
                guard self.broadcastActive else { return }
                self.startPinnedSession(config)
            }
        }
        return true
    }

    private func handleNodeEvent(_ event: NodeEvent) {
        switch event {
        case .ready(let cold):
            // 自动模式：把「本机是否还有句子没收尾」同步给决策器，再问要不要切回。
            // 没收尾包括「正在识别」和「译文还在路上」两种：
            // 只看正在识别的话，节点恰好在译文飞回来之前就绪，最后一句就只剩原文。
            if stateQueue.sync(execute: { localBusy }) {
                _ = handleAutoEvent(.localSentenceStarted)
            }
            let decision = handleAutoEvent(.nodeReady)
            stateQueue.sync {
                readyReceived = true
                reconnectPolicy.reset()
            }
            logger.info("Node ready cold=\(cold)")
            applyAutoDecision(decision)
        case .final(let id, _, _, let text):
            stateQueue.sync {
                finalsCount += 1
                recentLines.append(RecentLine(id: id, src: text, dst: "", session: nodeSessionCode))
                if recentLines.count > maxRecentLines {
                    recentLines.removeFirst()
                }
            }
            writeLatestLines()
        case .translation(let id, let text):
            stateQueue.sync {
                translationsCount += 1
                if let idx = recentLines.firstIndex(where: { $0.id == id }) {
                    recentLines[idx].dst = text
                }
            }
            writeLatestLines()
        case .status(let code, let text, _):
            stateQueue.sync { lastStatusCode = code }
            logger.info("Node status: \(code) \(text)")
            if NodeEvent.isFatal(code: code) {
                if engineMode == "auto" {
                    // 自动模式不把节点致命错误记成全局 fatal——那会连本机一起停掉。
                    applyAutoDecision(handleAutoEvent(.reconnectAbandoned))
                } else {
                    stateQueue.sync {
                        fatalErrorOccurred = true
                        fatalErrorReason = "fatal:\(code)"
                    }
                }
            }
        case .drained:
            break
        }
    }

    private func handleNodeClose(_ code: Int) {
        // 广播已经结束就别再管这个 close 了：不发 .nodeFailed（那会把本机引擎重新拉起来）、
        // 不重连、不改状态。少了这道闸，广播结束瞬间的 close 事件会让本机引擎复活，
        // 而节点根本不存在——用户看到的是「字幕还在跑，但没有节点」。
        guard stateQueue.sync(execute: { broadcastActive }) else {
            logger.info("Node closed after broadcast ended: \(code)")
            return
        }

        let displayCode = code == 1013 ? "busy" : "\(code)"
        stateQueue.sync { lastStatusCode = "closed=\(displayCode)" }
        logger.info("Node closed: \(code)")

        // 显式 node 和自动模式都要重连；显式 local 不碰节点。
        guard engineMode == "node" || engineMode == "auto" else { return }

        if engineMode == "auto" {
            stateQueue.sync { readyReceived = false }
            applyAutoDecision(handleAutoEvent(.nodeFailed))
        }

        // 由 NodeSession 通过 closeReason 还原过 1005→真实码
        let scheduled = scheduleReconnect(closeCode: code)
        if !scheduled, engineMode == "auto" {
            applyAutoDecision(handleAutoEvent(.reconnectAbandoned))
        }
    }

    private func computeRMS(sampleBuffer: CMSampleBuffer, asbd: AudioStreamBasicDescription) -> Float {
        guard let dataBuffer = CMSampleBufferGetDataBuffer(sampleBuffer) else { return 0 }

        var lengthAtOffset: Int = 0
        var totalLength: Int = 0
        var dataPointer: UnsafeMutablePointer<Int8>?
        CMBlockBufferGetDataPointer(dataBuffer, atOffset: 0, lengthAtOffsetOut: &lengthAtOffset, totalLengthOut: &totalLength, dataPointerOut: &dataPointer)
        guard lengthAtOffset > 0, let ptr = dataPointer else { return 0 }

        let bitsPerChannel = Int(asbd.mBitsPerChannel)
        let bytesPerSample = bitsPerChannel / 8
        let sampleCount = lengthAtOffset / bytesPerSample
        guard sampleCount > 0 else { return 0 }

        var sumSquares: Float = 0
        let formatID = asbd.mFormatID
        let flags = asbd.mFormatFlags
        let isBigEndian = (flags & kAudioFormatFlagIsBigEndian) != 0

        if formatID == kAudioFormatLinearPCM {
            if flags & kAudioFormatFlagIsFloat != 0 {
                let floatPtr = ptr.withMemoryRebound(to: Float.self, capacity: sampleCount) { $0 }
                for i in 0..<sampleCount {
                    sumSquares += floatPtr[i] * floatPtr[i]
                }
            } else {
                let int16Ptr = ptr.withMemoryRebound(to: Int16.self, capacity: sampleCount) { $0 }
                for i in 0..<sampleCount {
                    let sample: Int16
                    if isBigEndian {
                        sample = Int16(bigEndian: int16Ptr[i])
                    } else {
                        sample = int16Ptr[i]
                    }
                    let f = Float(sample) / 32768.0
                    sumSquares += f * f
                }
            }
        }

        let meanSquare = sumSquares / Float(sampleCount)
        return sqrt(meanSquare)
    }

    private func residentMemoryMB() -> Int {
        var info = task_vm_info_data_t()
        var count = mach_msg_type_number_t(MemoryLayout<task_vm_info_data_t>.size / MemoryLayout<integer_t>.size)
        let result = withUnsafeMutablePointer(to: &info) { ptr in
            ptr.withMemoryRebound(to: integer_t.self, capacity: Int(count)) { ptr in
                task_info(mach_task_self_, task_flavor_t(TASK_VM_INFO), ptr, &count)
            }
        }
        guard result == KERN_SUCCESS else { return 0 }
        return Int(info.phys_footprint) / (1024 * 1024)
    }

    private func writeStatus(_ text: String) {
        let success = SharedKeychain.writeExtStatus(text)
        if !success {
            logger.error("Failed to write status to keychain")
        }
        postDarwinNotification()
    }

    private func writeLatestLines() {
        let encoder = JSONEncoder()
        encoder.outputFormatting = []
        if let data = try? encoder.encode(recentLines),
           let json = String(data: data, encoding: .utf8) {
            if json != lastLinesJSON {
                lastLinesJSON = json
                let success = SharedKeychain.writeLatestLines(json)
                if !success {
                    logger.error("Failed to write latest_lines to keychain")
                }
                postLinesNotification()
            }
        }
    }

    private func postDarwinNotification() {
        let center = CFNotificationCenterGetDarwinNotifyCenter()
        let name = "com.wyl2607.rsios.spike.tick" as CFString
        CFNotificationCenterPostNotification(center, CFNotificationName(name), nil, nil, true)
    }

    private func postLinesNotification() {
        let center = CFNotificationCenterGetDarwinNotifyCenter()
        let name = "com.wyl2607.rsios.spike.lines" as CFString
        CFNotificationCenterPostNotification(center, CFNotificationName(name), nil, nil, true)
    }
}
