import Foundation
import CryptoKit

struct CapabilitySnapshot: Sendable {
    var chipName: String
    var memoryBytes: UInt64
    var onAC: Bool
    var hasBattery: Bool
}

enum Capability {
    static let accurateMemoryThresholdBytes: UInt64 = 24 * 1024 * 1024 * 1024

    static func hwHash() -> String {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/sbin/ioreg")
        process.arguments = ["-rd1", "-c", "IOPlatformExpertDevice"]
        let pipe = Pipe()
        process.standardOutput = pipe
        do {
            try process.run()
            process.waitUntilExit()
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            if let text = String(data: data, encoding: .utf8),
               let regex = try? NSRegularExpression(pattern: "\"IOPlatformUUID\"\\s*=\\s*\"([^\"]+)\"") {
                if let match = regex.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)),
                   let range = Range(match.range(at: 1), in: text) {
                    let uuid = String(text[range])
                    let hash = SHA256.hash(data: uuid.data(using: .utf8)!)
                    return String(hash.compactMap { String(format: "%02x", $0) }.joined().prefix(16))
                }
            }
        } catch {}
        return ""
    }

    static func current() -> CapabilitySnapshot {
        CapabilitySnapshot(
            chipName: sysctlString("machdep.cpu.brand_string") ?? "",
            memoryBytes: sysctlUInt64("hw.memsize") ?? 0,
            onAC: currentPower().onAC,
            hasBattery: currentPower().hasBattery
        )
    }

    /// P8：强芯片/足够内存且接电，或没有电池的台式机。探测失败时保守判为不满足。
    static func supportsAccurateLocal(_ snapshot: CapabilitySnapshot = current()) -> Bool {
        if !snapshot.hasBattery {
            return true
        }
        let chip = snapshot.chipName.lowercased()
        let strongChip = chip.contains("pro") || chip.contains("max") || chip.contains("ultra")
        let enoughMemory = snapshot.memoryBytes >= accurateMemoryThresholdBytes
        return (strongChip || enoughMemory) && snapshot.onAC
    }

    static func selfTest() -> [String] {
        var failures: [String] = []
        func check(_ ok: Bool, _ name: String) {
            if !ok { failures.append(name) }
        }
        check(!supportsAccurateLocal(CapabilitySnapshot(
            chipName: "Apple M2", memoryBytes: 16 * 1024 * 1024 * 1024, onAC: true, hasBattery: true
        )), "MacBook Air M2 16GB 不应满足 P8")
        check(supportsAccurateLocal(CapabilitySnapshot(
            chipName: "Apple M2 Pro", memoryBytes: 16 * 1024 * 1024 * 1024, onAC: true, hasBattery: true
        )), "Pro 芯片接电应满足 P8")
        check(!supportsAccurateLocal(CapabilitySnapshot(
            chipName: "Apple M2 Pro", memoryBytes: 16 * 1024 * 1024 * 1024, onAC: false, hasBattery: true
        )), "笔记本电池供电不应满足 P8")
        check(supportsAccurateLocal(CapabilitySnapshot(
            chipName: "Apple M4", memoryBytes: 16 * 1024 * 1024 * 1024, onAC: true, hasBattery: false
        )), "无电池台式机应满足 P8")
        return failures
    }

    private static func sysctlString(_ name: String) -> String? {
        var size = 0
        guard sysctlbyname(name, nil, &size, nil, 0) == 0, size > 0 else {
            return nil
        }
        var buffer = [CChar](repeating: 0, count: size)
        guard sysctlbyname(name, &buffer, &size, nil, 0) == 0 else {
            return nil
        }
        return String(cString: buffer).trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private static func sysctlUInt64(_ name: String) -> UInt64? {
        var value: UInt64 = 0
        var size = MemoryLayout<UInt64>.size
        guard sysctlbyname(name, &value, &size, nil, 0) == 0 else {
            return nil
        }
        return value
    }

    private static func currentPower() -> (onAC: Bool, hasBattery: Bool) {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/pmset")
        process.arguments = ["-g", "batt"]
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = Pipe()
        do {
            try process.run()
            process.waitUntilExit()
        } catch {
            return (false, true)
        }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        let text = String(data: data, encoding: .utf8) ?? ""
        if text.contains("No batteries") {
            return (true, false)
        }
        if text.contains("AC Power") {
            return (true, true)
        }
        if text.contains("Battery Power") || text.contains("UPS Power") {
            return (false, true)
        }
        return (false, true)
    }
}
