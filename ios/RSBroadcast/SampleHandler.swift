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
    private var engineMode: String = "node"
    private var nodeName: String = ""
    private var readyReceived = false
    private var framesSent: UInt64 = 0
    private var finalsCount: Int = 0
    private var translationsCount: Int = 0
    private var preReadyDropped: UInt64 = 0
    private var lastStatusCode: String = ""
    private var fatalErrorOccurred = false
    private var fatalErrorReason: String = ""

    private var recentLines: [RecentLine] = []
    private let maxRecentLines = 3
    private let stateQueue = DispatchQueue(label: "com.wyl2607.rsios.spike.state")
    private let connectionQueue = DispatchQueue(label: "com.wyl2607.rsios.spike.connection")
    private var broadcastActive = false

    struct RecentLine: Codable {
        let id: Int
        let src: String
        var dst: String
    }

    override func broadcastStarted(withSetupInfo setupInfo: [String: NSObject]?) {
        logger.info("Broadcast started")
        writeStatus("started")

        engineMode = SharedKeychain.readEngineMode() ?? "node"

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

        guard let json = SharedKeychain.readNodeConfig(),
              let data = json.data(using: .utf8),
              let config = try? JSONDecoder().decode(NodeConfig.self, from: data) else {
            writeStatus("no_config")
            return
        }

        nodeName = config.name
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
        }

        startPinnedSession(config)
    }

    override func broadcastFinished() {
        logger.info("Broadcast finished")
        
        if engineMode == "local" {
            if #available(iOS 26.0, *) {
                (localEngine as? LocalEngine)?.stop()
                localEngine = nil
            }
            converter = nil
            writeStatus("finished")
            return
        }

        let currentSession = stateQueue.sync { () -> NodeSession? in
            broadcastActive = false
            let currentSession = session
            session = nil
            return currentSession
        }
        currentSession?.flush()
        currentSession?.close()
        converter = nil
        writeStatus("finished")
    }

    @available(iOS 26.0, *)
    private func startLocalEngine() {
        converter = PCMConverter()
        stateQueue.sync {
            readyReceived = true
            framesSent = 0
            finalsCount = 0
            translationsCount = 0
            preReadyDropped = 0
            lastStatusCode = ""
            fatalErrorOccurred = false
            fatalErrorReason = ""
            recentLines = []
            broadcastActive = true
        }

        let engine = LocalEngine(
            onFinal: { [weak self] id, text in
                self?.handleLocalFinal(id: id, text: text)
            },
            onTranslation: { [weak self] id, text in
                self?.handleLocalTranslation(id: id, text: text)
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
            recentLines.append(RecentLine(id: id, src: text, dst: ""))
            if recentLines.count > maxRecentLines {
                recentLines.removeFirst()
            }
        }
        writeLatestLines()
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
        if status == "asr_assets_missing" || status == "asr_unsupported" || status == "analyzer_format_failed" || status == "asr_failed" || status == "asr_result_failed" {
            stateQueue.sync {
                fatalErrorOccurred = true
                fatalErrorReason = "fatal:\(status)"
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
                (readyReceived, framesSent, finalsCount, translationsCount, lastStatusCode, preReadyDropped, fatalErrorOccurred, fatalErrorReason)
            }
            let readyStr = state.0 ? "1" : "0"
            let engineStr = engineMode == "local" ? "local" : "node"
            var status = "blocks=\(blockCount) rate=\(Int(sampleRate)) ch=\(channelCount) fmt=\(formatString) flags=\(formatFlags) ileave=\(interleavedStr) rms=\(String(format: "%.3f", avgRMS)) mem=\(memMB) engine=\(engineStr)"
            if engineMode == "node" {
                status += " node=\(nodeName) ready=\(readyStr) sent=\(state.1) finals=\(state.2) tx=\(state.3) last=\(state.4.isEmpty ? "-" : state.4) drop=\(state.5)"
            }
            if state.6, !state.7.isEmpty {
                status += " \(state.7)"
            }
            writeStatus(status)
            lastStatusWriteTime = now
        }

        if engineMode == "local" {
            if #available(iOS 26.0, *) {
                guard let converter = converter, let engine = localEngine as? LocalEngine else { return }
                let fatal = stateQueue.sync { fatalErrorOccurred }
                guard !fatal else { return }

                let frames = converter.append(sampleBuffer)
                for frame in frames {
                    engine.append(pcm16k: frame)
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
                if shouldReport {
                    self.writeStatus("last=\(status)")
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
                return true
            }
            if shouldStart {
                newSession.start()
            }
        }
    }

    private func handleNodeEvent(_ event: NodeEvent) {
        switch event {
        case .ready(let cold):
            stateQueue.sync { readyReceived = true }
            logger.info("Node ready cold=\(cold)")
        case .final(let id, _, _, let text):
            stateQueue.sync {
                finalsCount += 1
                recentLines.append(RecentLine(id: id, src: text, dst: ""))
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
                stateQueue.sync {
                    fatalErrorOccurred = true
                    fatalErrorReason = "fatal:\(code)"
                }
            }
        case .drained:
            break
        }
    }

    private func handleNodeClose(_ code: Int) {
        let displayCode = code == 1013 ? "busy" : "\(code)"
        stateQueue.sync { lastStatusCode = "closed=\(displayCode)" }
        stateQueue.sync {
            fatalErrorOccurred = true
            fatalErrorReason = "fatal:closed=\(displayCode)"
        }
        logger.info("Node closed: \(code)")
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
