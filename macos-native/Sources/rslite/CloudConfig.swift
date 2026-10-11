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
        if isatty(STDIN_FILENO) != 0 {
            let line = try readHiddenLineFromTTY()
            try writeKey(line, directory: directory)
            return
        }
        guard let line = readLine(strippingNewline: true) else {
            throw CloudConfigError.stdinClosed
        }
        try writeKey(line, directory: directory)
    }

    /// stdin 是 TTY 时关掉 ECHO 读一行。任何退出（含出错）都恢复进入时的 termios，并向 stderr 补一个换行。
    private static func readHiddenLineFromTTY() throws -> String {
        var original = termios()
        guard tcgetattr(STDIN_FILENO, &original) == 0 else {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
        var hidden = original
        hidden.c_lflag &= ~tcflag_t(ECHO)
        guard tcsetattr(STDIN_FILENO, TCSANOW, &hidden) == 0 else {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
        defer {
            var restore = original
            _ = tcsetattr(STDIN_FILENO, TCSANOW, &restore)
            var newline: UInt8 = 0x0A
            _ = Darwin.write(STDERR_FILENO, &newline, 1)
        }
        var bytes: [UInt8] = []
        var byte: UInt8 = 0
        while true {
            let count = Darwin.read(STDIN_FILENO, &byte, 1)
            if count == 0 {
                if bytes.isEmpty {
                    throw CloudConfigError.stdinClosed
                }
                break
            }
            if count < 0 {
                if errno == EINTR { continue }
                throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
            }
            if byte == UInt8(ascii: "\n") {
                break
            }
            bytes.append(byte)
        }
        return String(decoding: bytes, as: UTF8.self)
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

        failures.append(contentsOf: ttyEchoSelfTest())
        return failures
    }

    private static func ttyEchoSelfTest() -> [String] {
        var failures: [String] = []
        func check(_ ok: Bool, _ name: String) {
            if !ok { failures.append(name) }
        }

        var master: Int32 = -1
        var slave: Int32 = -1
        var savedIn: Int32 = -1
        var savedErr: Int32 = -1
        var errRead: Int32 = -1
        defer {
            if savedIn >= 0 {
                _ = dup2(savedIn, STDIN_FILENO)
                close(savedIn)
            }
            if savedErr >= 0 {
                _ = dup2(savedErr, STDERR_FILENO)
                close(savedErr)
            }
            if slave >= 0 { close(slave) }
            if master >= 0 { close(master) }
            if errRead >= 0 { close(errRead) }
        }

        guard openpty(&master, &slave, nil, nil, nil) == 0 else {
            failures.append("openpty 失败，无法验证 TTY 关回显")
            return failures
        }
        savedIn = dup(STDIN_FILENO)
        guard savedIn >= 0, dup2(slave, STDIN_FILENO) != -1 else {
            failures.append("dup2 stdin 失败，无法验证 TTY 关回显")
            return failures
        }
        close(slave)
        slave = -1

        var pipeEnds = [Int32](repeating: 0, count: 2)
        guard pipe(&pipeEnds) == 0 else {
            failures.append("pipe 失败，无法验证 TTY stderr 换行")
            return failures
        }
        errRead = pipeEnds[0]
        let errWrite = pipeEnds[1]
        savedErr = dup(STDERR_FILENO)
        guard savedErr >= 0, dup2(errWrite, STDERR_FILENO) != -1 else {
            close(errWrite)
            failures.append("dup2 stderr 失败，无法验证 TTY stderr 换行")
            return failures
        }
        close(errWrite)
        let errFlags = fcntl(errRead, F_GETFL)
        _ = fcntl(errRead, F_SETFL, errFlags | O_NONBLOCK)
        let masterFlags = fcntl(master, F_GETFL)
        _ = fcntl(master, F_SETFL, masterFlags | O_NONBLOCK)

        var cooked = termios()
        guard tcgetattr(STDIN_FILENO, &cooked) == 0 else {
            failures.append("无法读取 pty termios")
            return failures
        }
        cooked.c_lflag |= tcflag_t(ECHO | ICANON)
        cooked.c_iflag |= tcflag_t(ICRNL)
        guard tcsetattr(STDIN_FILENO, TCSANOW, &cooked) == 0 else {
            failures.append("无法设置 pty termios")
            return failures
        }
        var savedTerm = termios()
        guard tcgetattr(STDIN_FILENO, &savedTerm) == 0 else {
            failures.append("无法回读 pty termios")
            return failures
        }
        check((savedTerm.c_lflag & tcflag_t(ECHO)) != 0, "测试前 TTY 应开启 ECHO")

        let dir = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("rslite-tty-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: dir) }
        let key = "gsk_abcdefghijklmnopqrstuvwxyz012345"

        func drain(_ fd: Int32) -> Data {
            var buffer = [UInt8](repeating: 0, count: 256)
            var data = Data()
            while true {
                let count = Darwin.read(fd, &buffer, buffer.count)
                if count > 0 {
                    data.append(buffer, count: count)
                    continue
                }
                break
            }
            return data
        }

        func flagsMatch(_ term: termios) -> Bool {
            term.c_iflag == savedTerm.c_iflag
                && term.c_oflag == savedTerm.c_oflag
                && term.c_cflag == savedTerm.c_cflag
                && term.c_lflag == savedTerm.c_lflag
        }

        func exercise(line: String) -> Error? {
            final class Outcome: @unchecked Sendable {
                private let lock = NSLock()
                private var error: Error?
                func set(_ error: Error?) { lock.withLockVoid { self.error = error } }
                func get() -> Error? { lock.withLockValue { error } }
            }
            let outcome = Outcome()
            let done = DispatchSemaphore(value: 0)
            DispatchQueue.global(qos: .userInitiated).async {
                do {
                    try readLineFromStdinAndWriteKey(directory: dir)
                    outcome.set(nil)
                } catch {
                    outcome.set(error)
                }
                done.signal()
            }

            let deadline = Date().addingTimeInterval(1)
            var sawEchoOff = false
            while Date() < deadline {
                var nowTerm = termios()
                if tcgetattr(STDIN_FILENO, &nowTerm) == 0, (nowTerm.c_lflag & tcflag_t(ECHO)) == 0 {
                    sawEchoOff = true
                    break
                }
                Thread.sleep(forTimeInterval: 0.002)
            }
            check(sawEchoOff, "TTY 读取期间应关闭 ECHO")
            _ = drain(master)

            let payload = Array((line + "\n").utf8)
            let wrote = payload.withUnsafeBytes { raw -> Int in
                guard let base = raw.baseAddress else { return -1 }
                return Darwin.write(master, base, raw.count)
            }
            check(wrote == payload.count, "向 pty 写入测试行失败")
            if done.wait(timeout: .now() + 2) == .timedOut {
                failures.append("TTY 读 key 超时")
                if master >= 0 {
                    close(master)
                    master = -1
                }
                _ = done.wait(timeout: .now() + 2)
            }
            return outcome.get()
        }

        let successError = exercise(line: key)
        check(successError == nil, "TTY 关闭回显后应写入合法 key")
        check(readKey(from: defaultKeyURL(directory: dir)) == key, "TTY 路径写入的 key 应能读回")
        let perm = (try? FileManager.default.attributesOfItem(atPath: defaultKeyURL(directory: dir).path)[.posixPermissions] as? NSNumber)?.intValue ?? 0
        check(perm & 0o777 == 0o600, "TTY 写入的 key 文件权限应为 0600")
        var after = termios()
        _ = tcgetattr(STDIN_FILENO, &after)
        check(flagsMatch(after), "TTY 读完应恢复原终端设置")
        let echoed = drain(master)
        check(!String(decoding: echoed, as: UTF8.self).contains("gsk_"), "TTY 读 key 期间不应回显")
        check(drain(errRead) == Data([0x0A]), "TTY 读完应向 stderr 补一个换行")

        let invalidError = exercise(line: "bad")
        check(invalidError != nil, "TTY 下无效 key 仍应拒绝")
        check(readKey(from: defaultKeyURL(directory: dir)) == key, "TTY 下无效 key 不得覆盖已有 key")
        _ = tcgetattr(STDIN_FILENO, &after)
        check(flagsMatch(after), "TTY 出错后应恢复原终端设置")
        check(drain(errRead) == Data([0x0A]), "TTY 出错后仍应向 stderr 补一个换行")
        _ = drain(master)

        return failures
    }
}
