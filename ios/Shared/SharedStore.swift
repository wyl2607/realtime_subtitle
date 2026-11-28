import Foundation
import os

/// App Group 容器内的轻量键值文件存储（每个键一个文件）。
///
/// 为什么从 Keychain 迁到这里：状态行（5 秒一次）、最近句对与草稿（每句都写）属于高频
/// 非机密数据；付费团队已启用 App Group，Keychain 只留机密凭据（`groq_key`、`node_config`）。
///
/// 锁屏语义：写入必须显式带 `.completeFileProtectionUntilFirstUserAuthentication`，
/// 否则默认文件保护在锁屏后读写失败，主 App 会误判为采集被抢占或字幕停更（2026-11-21 真机 bug）。
public final class SharedStore: @unchecked Sendable {
    public static let appGroupIdentifier = "group.com.yilinwang.realtimesubtitle"

    public enum Key {
        public static let extStatus = "ext_status"
        public static let latestLines = "latest_lines"
        public static let latestDraft = "latest_draft"
        public static let engineMode = "engine_mode"
        public static let cloudEnabled = "cloud_enabled"
    }

    public enum LegacyAction: Equatable {
        case migrateValue
        case deleteOnly
        case none
    }

    private static let migrationValueKeys: [String] = [Key.engineMode, Key.cloudEnabled]

    public static let shared = SharedStore()

    private let rootURL: URL?
    private let legacyRead: @Sendable (String) -> String?
    private let legacyDelete: @Sendable (String) -> Bool
    private let lock = NSLock()
    // Protected by lock: each key gets at most one legacy Keychain check per process.
    private var legacyKeysChecked: Set<String> = []
    private let logger = Logger(subsystem: "com.wyl2607.rsios.spike", category: "shared-store")

    public init(
        rootURL: URL? = FileManager.default.containerURL(
            forSecurityApplicationGroupIdentifier: SharedStore.appGroupIdentifier
        ),
        legacyRead: @escaping @Sendable (String) -> String? = { SharedKeychain.read(account: $0) },
        legacyDelete: @escaping @Sendable (String) -> Bool = { SharedKeychain.delete(account: $0) }
    ) {
        self.rootURL = rootURL
        self.legacyRead = legacyRead
        self.legacyDelete = legacyDelete
    }

    /// 纯函数：决定缺失文件对应的旧 Keychain 项应迁移、只删除，还是忽略。
    public static func shouldMigrateFromKeychain(
        key: String,
        fileExists: Bool,
        legacyValue: String?
    ) -> LegacyAction {
        guard !fileExists else { return .none }
        if migrationValueKeys.contains(key) {
            return legacyValue == nil ? .none : .migrateValue
        }
        if [Key.extStatus, Key.latestLines, Key.latestDraft].contains(key) {
            return .deleteOnly
        }
        return .none
    }

    public func read(key: String) -> String? {
        guard let rootURL else {
            logger.error("App Group container unavailable; read failed key=\(key, privacy: .public)")
            return nil
        }

        return lock.withLock {
            let url = fileURL(rootURL: rootURL, key: key)
            let fileExists = FileManager.default.fileExists(atPath: url.path)
            if fileExists {
                do {
                    let data = try Data(contentsOf: url)
                    guard let value = String(data: data, encoding: .utf8) else {
                        logger.error("SharedStore decode UTF-8 failed key=\(key, privacy: .public)")
                        return nil
                    }
                    return value
                } catch {
                    logger.error("SharedStore read failed key=\(key, privacy: .public) error=\(error.localizedDescription, privacy: .public)")
                    return nil
                }
            }

            guard !legacyKeysChecked.contains(key) else {
                return nil
            }
            legacyKeysChecked.insert(key)

            let legacyValue = legacyRead(key)
            switch Self.shouldMigrateFromKeychain(
                key: key,
                fileExists: fileExists,
                legacyValue: legacyValue
            ) {
            case .deleteOnly:
                if !legacyDelete(key) {
                    logger.error("SharedStore stale legacy keychain delete failed key=\(key, privacy: .public)")
                }
                return nil
            case .migrateValue:
                guard let legacyValue else { return nil }
                if writeUnlocked(legacyValue, to: url, rootURL: rootURL, key: key) {
                    if !legacyDelete(key) {
                        logger.error("SharedStore legacy keychain delete failed key=\(key, privacy: .public)")
                    }
                }
                return legacyValue
            case .none:
                return nil
            }
        }
    }

    @discardableResult
    public func write(_ value: String, key: String) -> Bool {
        guard let rootURL else {
            logger.error("App Group container unavailable; write failed key=\(key, privacy: .public)")
            return false
        }

        return lock.withLock {
            let url = fileURL(rootURL: rootURL, key: key)
            return writeUnlocked(value, to: url, rootURL: rootURL, key: key)
        }
    }

    private func fileURL(rootURL: URL, key: String) -> URL {
        rootURL.appendingPathComponent(key, isDirectory: false)
    }

    private func writeUnlocked(_ value: String, to url: URL, rootURL: URL, key: String) -> Bool {
        do {
            try FileManager.default.createDirectory(at: rootURL, withIntermediateDirectories: true)
            try Data(value.utf8).write(
                to: url,
                options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication]
            )
            return true
        } catch {
            logger.error("SharedStore write failed key=\(key, privacy: .public) error=\(error.localizedDescription, privacy: .public)")
            return false
        }
    }

    // MARK: - Instance convenience

    @discardableResult
    public func writeExtStatus(_ string: String) -> Bool {
        write(string, key: Key.extStatus)
    }

    public func readExtStatus() -> String? {
        read(key: Key.extStatus)
    }

    @discardableResult
    public func writeLatestLines(_ string: String) -> Bool {
        write(string, key: Key.latestLines)
    }

    public func readLatestLines() -> String? {
        read(key: Key.latestLines)
    }

    @discardableResult
    public func writeLatestDraft(_ string: String) -> Bool {
        write(string, key: Key.latestDraft)
    }

    public func readLatestDraft() -> String? {
        read(key: Key.latestDraft)
    }

    @discardableResult
    public func writeEngineMode(_ string: String) -> Bool {
        write(string, key: Key.engineMode)
    }

    /// 没写过或读到不认识的旧值都按 `auto` 处理：升级后的老用户默认获得自动回退，
    /// 已有的 `node` / `local` 值原样返回，显式选择不会被改掉。
    public func readEngineMode() -> String {
        EngineMode.normalized(read(key: Key.engineMode)).rawValue
    }

    /// 云端开关。没写过时默认关：隐私默认是「不发到云端」（spec 002 C7）。
    @discardableResult
    public func writeCloudEnabled(_ enabled: Bool) -> Bool {
        write(enabled ? "1" : "0", key: Key.cloudEnabled)
    }

    public func readCloudEnabled() -> Bool {
        read(key: Key.cloudEnabled) == "1"
    }

    // MARK: - Shared singleton API

    @discardableResult
    public static func writeExtStatus(_ string: String) -> Bool {
        shared.writeExtStatus(string)
    }

    public static func readExtStatus() -> String? {
        shared.readExtStatus()
    }

    @discardableResult
    public static func writeLatestLines(_ string: String) -> Bool {
        shared.writeLatestLines(string)
    }

    public static func readLatestLines() -> String? {
        shared.readLatestLines()
    }

    @discardableResult
    public static func writeLatestDraft(_ string: String) -> Bool {
        shared.writeLatestDraft(string)
    }

    public static func readLatestDraft() -> String? {
        shared.readLatestDraft()
    }

    @discardableResult
    public static func writeEngineMode(_ string: String) -> Bool {
        shared.writeEngineMode(string)
    }

    public static func readEngineMode() -> String {
        shared.readEngineMode()
    }

    @discardableResult
    public static func writeCloudEnabled(_ enabled: Bool) -> Bool {
        shared.writeCloudEnabled(enabled)
    }

    public static func readCloudEnabled() -> Bool {
        shared.readCloudEnabled()
    }
}
