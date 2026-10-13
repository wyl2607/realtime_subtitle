import ReplayKit
import CoreMedia
import AudioToolbox
import os
import Darwin.Mach
import Foundation

class SampleHandler: RPBroadcastSampleHandler {
    private let logger = Logger(subsystem: "com.wyl2607.rsios.spike", category: "ext")

    private var blockCount: UInt64 = 0
    private var lastWriteTime: CFAbsoluteTime = 0
    private var accumulatedRMS: Float = 0
    private var rmsSampleCount: Int = 0
    private var sampleRate: Double = 0
    private var channelCount: UInt32 = 0
    private var formatString: String = "unknown"
    private var formatFlags: UInt32 = 0
    private var isNonInterleaved: Bool = false

    private var converter: PCMConverter?
    private var session: NodeSession?
    private var nodeName: String = ""
    private var readyReceived = false
    private var framesSent: UInt64 = 0
    private var finalsCount: Int = 0
    private var translationsCount: Int = 0
    private var preReadyDropped: UInt64 = 0
    private var lastStatusCode: String = ""
    private var fatalErrorOccurred = false

    private var recentLines: [RecentLine] = []
    private let maxRecentLines = 3

    struct RecentLine: Codable {
        let id: Int
        let src: String
        var dst: String
    }

    override func broadcastStarted(withSetupInfo setupInfo: [String: NSObject]?) {
        logger.info("Broadcast started")
        writeStatus("started")

        guard let json = SharedKeychain.readNodeConfig(),
              let data = json.data(using: .utf8),
              let config = try? JSONDecoder().decode(NodeConfig.self, from: data) else {
            writeStatus("no_config")
            return
        }

        nodeName = config.name
        converter = PCMConverter()
        readyReceived = false
        framesSent = 0
        finalsCount = 0
        translationsCount = 0
        preReadyDropped = 0
        lastStatusCode = ""
        fatalErrorOccurred = false
        recentLines = []

        session = NodeSession(
            url: config.sessionURL,
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
        session?.start()
    }

    override func broadcastFinished() {
        logger.info("Broadcast finished")
        session?.flush()
        session?.close()
        session = nil
        converter = nil
        writeStatus("finished")
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
            let avgRMS = rmsSampleCount > 0 ? accumulatedRMS / Float(rmsSampleCount) : 0
            let memMB = residentMemoryMB()
            let interleavedStr = isNonInterleaved ? "0" : "1"
            let readyStr = readyReceived ? "1" : "0"
            let status = "blocks=\(blockCount) rate=\(Int(sampleRate)) ch=\(channelCount) fmt=\(formatString) flags=\(formatFlags) ileave=\(interleavedStr) rms=\(String(format: "%.3f", avgRMS)) mem=\(memMB) node=\(nodeName) ready=\(readyStr) sent=\(framesSent) finals=\(finalsCount) tx=\(translationsCount) last=\(lastStatusCode.isEmpty ? "-" : lastStatusCode)"
            writeStatus(status)
            lastWriteTime = now
            accumulatedRMS = 0
            rmsSampleCount = 0
        }

        guard let converter = converter, let session = session, !fatalErrorOccurred else { return }

        let frames = converter.append(sampleBuffer)
        for frame in frames {
            if readyReceived {
                session.send(pcm16k: frame)
                framesSent += 1
            } else {
                preReadyDropped += 1
            }
        }
    }

    private func handleNodeEvent(_ event: NodeEvent) {
        switch event {
        case .ready(let cold):
            readyReceived = true
            logger.info("Node ready cold=\(cold)")
        case .final(let id, _, _, let text):
            finalsCount += 1
            recentLines.append(RecentLine(id: id, src: text, dst: ""))
            if recentLines.count > maxRecentLines {
                recentLines.removeFirst()
            }
            writeLatestLines()
        case .translation(let id, let text):
            translationsCount += 1
            if let idx = recentLines.firstIndex(where: { $0.id == id }) {
                recentLines[idx].dst = text
                writeLatestLines()
            }
        case .status(let code, let text, _):
            lastStatusCode = code
            logger.info("Node status: \(code) \(text)")
            if NodeEvent.isFatal(code: code) {
                fatalErrorOccurred = true
            }
        case .drained:
            break
        }
    }

    private func handleNodeClose(_ code: Int) {
        let displayCode = code == 1013 ? "busy" : "\(code)"
        lastStatusCode = "closed=\(displayCode)"
        fatalErrorOccurred = true
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
            let success = SharedKeychain.writeLatestLines(json)
            if !success {
                logger.error("Failed to write latest_lines to keychain")
            }
            postLinesNotification()
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