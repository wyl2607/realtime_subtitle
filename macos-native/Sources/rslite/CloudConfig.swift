import Darwin
import Foundation

struct CloudConfig: Codable, Equatable {
    var enabled: Bool
}

enum CloudConfigError: Error, CustomStringConvertible {
    case invalidKeyShape
    case stdinClosed

    var description: String {
        switch self {
        case .invalidKeyShape:
            return "Groq key 形状无效：必须以 gsk_ 开头且长度足够"
        case .stdinClosed:
            return "没有从 stdin 读到 Groq key"
        }
    }
}

enum CloudConfigStore {
    static let keyFileName = "groq.key"
    static let configFileName = "cloud.json"
    static let minimumKeyLength = 20

    static var defaultDirectory: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".config", isDirectory: true)
            .appendingPathComponent("rslite", isDirectory: true)
    }

    static func defaultKeyURL(directory: URL = defaultDirectory) -> URL {
        directory.appendingPathComponent(keyFileName)
    }

    static func defaultConfigURL(directory: URL = defaultDirectory) -> URL {
        directory.appendingPathComponent(configFileName)
    }

    static func readKey(from url: URL = defaultKeyURL()) -> String? {
        guard let data = try? Data(contentsOf: url),
              let raw = String(data: data, encoding: .utf8) else {
            return nil
        }
        let key = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        return isValidKeyShape(key) ? key : nil
    }

    static func readEnabled(directory: URL = defaultDirectory) -> Bool {
        let keyExists = readKey(from: defaultKeyURL(directory: directory)) != nil
        let configURL = defaultConfigURL(directory: directory)
        guard let data = try? Data(contentsOf: configURL),
              let config = try? JSONDecoder().decode(CloudConfig.self, from: data) else {
            // RFC：有 key 且未写开关文件时，云端默认开启。
            return keyExists
        }
        return config.enabled
    }

    static func readLineFromStdinAndWriteKey(directory: URL = defaultDirectory) throws {
        guard let line = readLine(strippingNewline: true) else {
            throw CloudConfigError.stdinClosed
        }
        try writeKey(line, directory: directory)
    }

    static func writeKey(_ raw: String, directory: URL = defaultDirectory) throws {
        let key = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard isValidKeyShape(key) else {
            throw CloudConfigError.invalidKeyShape
        }
        try atomicWrite(Data((key + "\n").utf8), to: defaultKeyURL(directory: directory), mode: 0o600)
        if !FileManager.default.fileExists(atPath: defaultConfigURL(directory: directory).path) {
            try writeEnabled(true, directory: directory)
        }
    }

    static func clearKey(directory: URL = defaultDirectory) throws {
        let url = defaultKeyURL(directory: directory)
        if FileManager.default.fileExists(atPath: url.path) {
            try FileManager.default.removeItem(at: url)
        }
    }

    static func writeEnabled(_ enabled: Bool, directory: URL = defaultDirectory) throws {
        let data = try JSONEncoder().encode(CloudConfig(enabled: enabled))
        try atomicWrite(data, to: defaultConfigURL(directory: directory), mode: 0o600)
    }

    static func isValidKeyShape(_ key: String) -> Bool {
        key.hasPrefix("gsk_") && key.count >= minimumKeyLength && !key.contains(where: { $0.isWhitespace })
    }

    /// key 与开关都是安全配置：同目录临时文件写满、fsync、0600、rename，避免半截 key 落盘。
    static func atomicWrite(
        _ data: Data,
        to url: URL,
        mode: mode_t,
        sync: (Int32) -> Int32 = { fsync($0) }
    ) throws {
        try FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(),
            withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        let tmp = url.deletingLastPathComponent()
            .appendingPathComponent(".\(url.lastPathComponent).\(UUID().uuidString).tmp")
        let fd = open(tmp.path, O_WRONLY | O_CREAT | O_EXCL, mode)
        guard fd >= 0 else {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
        var renamed = false
        defer {
            if !renamed {
                unlink(tmp.path)
            }
        }
        do {
            try data.withUnsafeBytes { raw in
                guard let base = raw.baseAddress else { return }
                var offset = 0
                while offset < raw.count {
                    let written = write(fd, base.advanced(by: offset), raw.count - offset)
                    if written < 0 {
                        if errno == EINTR { continue }
                        throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
                    }
                    if written == 0 {
                        throw POSIXError(.EIO)
                    }
                    offset += written
                }
            }
            if fchmod(fd, mode) != 0 {
                throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
            }
            if sync(fd) != 0 {
                throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
            }
        } catch {
            close(fd)
            throw error
        }
        close(fd)
        guard rename(tmp.path, url.path) == 0 else {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
        renamed = true
    }

    static func selfTest() -> [String] {
        var failures: [String] = []
        func check(_ ok: Bool, _ name: String) {
            if !ok { failures.append(name) }
        }

        let dir = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("rslite-cloud-config-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: dir) }

        do {
            check(readEnabled(directory: dir) == false, "无 key 时默认关闭")
            try writeKey("gsk_abcdefghijklmnopqrstuvwxyz012345", directory: dir)
            check(readKey(from: defaultKeyURL(directory: dir)) == "gsk_abcdefghijklmnopqrstuvwxyz012345", "写入后应能读回 key")
            check(readEnabled(directory: dir) == true, "有 key 且无显式开关时默认开启")
            let perm = (try FileManager.default.attributesOfItem(atPath: defaultKeyURL(directory: dir).path)[.posixPermissions] as? NSNumber)?.intValue ?? 0
            check(perm & 0o777 == 0o600, "key 文件权限应为 0600")

            try writeEnabled(false, directory: dir)
            check(readEnabled(directory: dir) == false, "cloud.json false 应关闭云端")
            try writeEnabled(true, directory: dir)
            check(readEnabled(directory: dir) == true, "cloud.json true 应开启云端")

            var invalidThrew = false
            do {
                try writeKey("bad_key", directory: dir)
            } catch {
                invalidThrew = true
            }
            check(invalidThrew, "无效 key 形状应拒绝写入")
        } catch {
            failures.append("CloudConfig selftest 异常：\(error)")
        }

        do {
            let file = dir.appendingPathComponent("atomic.json")
            try atomicWrite(Data("{\"a\":1}".utf8), to: file, mode: 0o600)
            var threw = false
            do {
                try atomicWrite(Data("{\"a\":2}".utf8), to: file, mode: 0o600, sync: { _ in errno = EIO; return -1 })
            } catch {
                threw = true
            }
            let back = String(data: try Data(contentsOf: file), encoding: .utf8)
            let leftovers = try FileManager.default.contentsOfDirectory(atPath: dir.path)
                .filter { $0.contains(".tmp") }
            check(threw, "fsync 失败应抛错")
            check(back == "{\"a\":1}", "fsync 失败不得覆盖旧文件")
            check(leftovers.isEmpty, "fsync 失败不应留下临时文件")
        } catch {
            failures.append("CloudConfig 原子写测试异常：\(error)")
        }

        return failures
    }
}
