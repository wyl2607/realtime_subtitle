import ReplayKit
import CoreMedia
import AudioToolbox
import os
import Darwin.Mach
import Shared

class SampleHandler: RPBroadcastSampleHandler {
    private let logger = Logger(subsystem: "com.wyl2607.rsios.spike", category: "ext")

    private var blockCount: UInt64 = 0
    private var lastWriteTime: CFAbsoluteTime = 0
    private var accumulatedRMS: Float = 0
    private var rmsSampleCount: Int = 0
    private var sampleRate: Double = 0
    private var channelCount: UInt32 = 0
    private var formatString: String = "unknown"

    override func broadcastStarted(withSetupInfo setupInfo: [String: NSObject]?) {
        logger.info("Broadcast started")
        writeStatus("started")
    }

    override func broadcastFinished() {
        logger.info("Broadcast finished")
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
            let formatID = asbd.pointee.mFormatID
            if formatID == kAudioFormatLinearPCM {
                let flags = asbd.pointee.mFormatFlags
                if flags & kAudioFormatFlagIsFloat != 0 {
                    formatString = "float32"
                } else {
                    formatString = "int16"
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
            let status = "blocks=\(blockCount) rate=\(Int(sampleRate)) ch=\(channelCount) fmt=\(formatString) rms=\(String(format: "%.3f", avgRMS)) mem=\(memMB)"
            writeStatus(status)
            lastWriteTime = now
            accumulatedRMS = 0
            rmsSampleCount = 0
        }
    }

    private func computeRMS(sampleBuffer: CMSampleBuffer, asbd: AudioStreamBasicDescription) -> Float {
        guard let dataBuffer = CMSampleBufferGetDataBuffer(sampleBuffer) else { return 0 }

        var length: Int = 0
        var dataPointer: UnsafeMutablePointer<Int8>?
        CMBlockBufferGetDataPointer(dataBuffer, atOffset: 0, lengthAtOffsetOut: nil, totalLengthOut: &length, dataPointerOut: &dataPointer)
        guard length > 0, let ptr = dataPointer else { return 0 }

        let bytesPerFrame = Int(asbd.mBytesPerFrame)
        let frameCount = length / bytesPerFrame
        guard frameCount > 0 else { return 0 }

        var sumSquares: Float = 0
        let formatID = asbd.mFormatID
        let flags = asbd.mFormatFlags

        if formatID == kAudioFormatLinearPCM {
            if flags & kAudioFormatFlagIsFloat != 0 {
                let floatPtr = ptr.withMemoryRebound(to: Float.self, capacity: frameCount * Int(channelCount)) { $0 }
                for i in 0..<frameCount * Int(channelCount) {
                    sumSquares += floatPtr[i] * floatPtr[i]
                }
            } else {
                let int16Ptr = ptr.withMemoryRebound(to: Int16.self, capacity: frameCount * Int(channelCount)) { $0 }
                for i in 0..<frameCount * Int(channelCount) {
                    let sample = Float(int16Ptr[i]) / 32768.0
                    sumSquares += sample * sample
                }
            }
        }

        let meanSquare = sumSquares / Float(frameCount * Int(channelCount))
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
        let success = SharedKeychain.write(text)
        if !success {
            logger.error("Failed to write status to keychain: \(text, privacy: .public)")
        }
        postDarwinNotification()
    }

    private func postDarwinNotification() {
        let center = CFNotificationCenterGetDarwinNotifyCenter()
        let name = "com.wyl2607.rsios.spike.tick" as CFString
        CFNotificationCenterPostNotification(center, CFNotificationName(name), nil, nil, true)
    }
}