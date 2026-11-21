import Foundation
import Security
import os

public enum SharedKeychain {
    public static let service = "rsios.spike"
    public static let accessGroupPrefix = "com.yilinwang.realtimesubtitle.shared"

    public enum Account {
        public static let extStatus = "ext_status"
        public static let nodeConfig = "node_config"
        public static let latestLines = "latest_lines"
        /// 未定稿的实时草稿原文：只给主界面/画中画显示，不进字幕存档。
        public static let latestDraft = "latest_draft"
        public static let engineMode = "engine_mode"
        /// Groq API key。只进 Keychain：UserDefaults 会随备份/共享出去，日志和 URL 更不能带。
        public static let groqKey = "groq_key"
        /// 云端开关（用户是否允许把音频片段发给 Groq）。
        public static let cloudEnabled = "cloud_enabled"
    }

    private static let logger = Logger(subsystem: "com.wyl2607.rsios.spike", category: "shared-keychain")

    public static func makeAccessGroup() -> String {
        let prefix = Bundle.main.object(forInfoDictionaryKey: "AppIdentifierPrefix") as? String ?? ""
        return "\(prefix)\(accessGroupPrefix)"
    }

    /// 锁屏后也要能读写：广播扩展和麦克风引擎熄屏后照常出字幕，状态/句对全靠这里传。
    /// 默认的 WhenUnlocked 在锁屏后一律返回 -25308，主 App 读到空状态会误判成别的采集
    /// 抢占而把麦克风停掉（2026-11-21 真机：锁屏 3 秒后录音停止）。ThisDeviceOnly：
    /// 这些值（含 Groq key）不该跟着备份去别的设备。
    static var accessibility: CFString { kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly }

    private static func itemQuery(account: String) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecAttrAccessGroup as String: makeAccessGroup(),
            kSecAttrSynchronizable as String: kCFBooleanFalse!,
        ]
    }

    public static func write(_ value: String, account: String) -> Bool {
        let query = itemQuery(account: account)
        // 每次更新都带上可访问性：老版本写下的 WhenUnlocked 条目第一次解锁状态下更新时就被改过来。
        let attributes: [String: Any] = [
            kSecValueData as String: Data(value.utf8),
            kSecAttrAccessible as String: accessibility,
        ]

        var status = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        if status == errSecSuccess {
            logger.debug("Keychain updated account=\(account, privacy: .public)")
            return true
        }

        if status == errSecItemNotFound {
            status = SecItemAdd(query.merging(attributes) { $1 } as CFDictionary, nil)
            if status == errSecSuccess {
                logger.debug("Keychain added account=\(account, privacy: .public)")
                return true
            }
        }

        logger.error("Keychain write failed account=\(account, privacy: .public) status=\(status)")
        return false
    }

    /// App 启动（前台、已解锁）时把所有已有条目改成锁屏可读。节点配置、档位、key 这些
    /// 很少重写，不迁移的话熄屏后引擎重连时仍然读不到。没有的条目跳过。
    public static func migrateAccessibility() {
        let accounts = [
            Account.extStatus, Account.nodeConfig, Account.latestLines, Account.latestDraft,
            Account.engineMode, Account.groqKey, Account.cloudEnabled,
        ]
        for account in accounts {
            let status = SecItemUpdate(
                itemQuery(account: account) as CFDictionary,
                [kSecAttrAccessible as String: accessibility] as CFDictionary
            )
            if status != errSecSuccess && status != errSecItemNotFound {
                logger.error("Keychain migrate failed account=\(account, privacy: .public) status=\(status)")
            }
        }
    }

    public static func read(account: String) -> String? {
        let accessGroup = makeAccessGroup()

        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecAttrAccessGroup as String: accessGroup,
            kSecReturnData as String: kCFBooleanTrue!,
            kSecMatchLimit as String: kSecMatchLimitOne,
            kSecAttrSynchronizable as String: kCFBooleanFalse!,
        ]

        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)

        guard status == errSecSuccess,
              let data = item as? Data,
              let string = String(data: data, encoding: .utf8) else {
            if status != errSecItemNotFound {
                logger.error("Keychain read failed account=\(account, privacy: .public) status=\(status)")
            }
            return nil
        }

        return string
    }

    @discardableResult
    public static func writeExtStatus(_ string: String) -> Bool {
        write(string, account: Account.extStatus)
    }

    public static func readExtStatus() -> String? {
        read(account: Account.extStatus)
    }

    @discardableResult
    public static func writeNodeConfig(_ string: String) -> Bool {
        write(string, account: Account.nodeConfig)
    }

    public static func readNodeConfig() -> String? {
        read(account: Account.nodeConfig)
    }

    @discardableResult
    public static func writeLatestLines(_ string: String) -> Bool {
        write(string, account: Account.latestLines)
    }

    public static func readLatestLines() -> String? {
        read(account: Account.latestLines)
    }

    @discardableResult
    public static func writeLatestDraft(_ string: String) -> Bool {
        write(string, account: Account.latestDraft)
    }

    public static func readLatestDraft() -> String? {
        read(account: Account.latestDraft)
    }

    @discardableResult
    public static func writeEngineMode(_ string: String) -> Bool {
        write(string, account: Account.engineMode)
    }

    /// 没写过或读到不认识的旧值都按 `auto` 处理：升级后的老用户默认获得自动回退，
    /// 已有的 `node` / `local` 值原样返回，显式选择不会被改掉。
    public static func readEngineMode() -> String {
        EngineMode.normalized(read(account: Account.engineMode)).rawValue
    }

    // MARK: - 云端档（Groq）

    /// 写 Groq key。key 只进 Keychain：广播扩展和主 App 靠共享 access group 读同一份。
    /// 返回 false 表示 Keychain 写失败，调用方不要当成「已保存」。
    @discardableResult
    public static func writeGroqKey(_ key: String) -> Bool {
        let trimmed = key.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty {
            return delete(account: Account.groqKey)
        }
        return write(trimmed, account: Account.groqKey)
    }

    /// 读 Groq key。空串与 nil 一律当「没有 key」。
    public static func readGroqKey() -> String? {
        guard let key = read(account: Account.groqKey) else { return nil }
        let trimmed = key.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    public static func hasGroqKey() -> Bool {
        readGroqKey() != nil
    }

    /// 清除 Groq key。写开关之前先清：key 没了，开关开着也没有云端可去。
    @discardableResult
    public static func clearGroqKey() -> Bool {
        delete(account: Account.groqKey)
    }

    /// 展示用的掩码：只留后 4 位。界面上永远不显示完整 key。
    public static func maskedGroqKey() -> String? {
        guard let key = readGroqKey() else { return nil }
        guard key.count > 4 else { return String(repeating: "*", count: key.count) }
        return "••••" + key.suffix(4)
    }

    /// 云端开关。没写过时默认关：隐私默认是「不发到云端」（spec 002 C7）。
    @discardableResult
    public static func writeCloudEnabled(_ enabled: Bool) -> Bool {
        write(enabled ? "1" : "0", account: Account.cloudEnabled)
    }

    public static func readCloudEnabled() -> Bool {
        read(account: Account.cloudEnabled) == "1"
    }

    public static func delete(account: String) -> Bool {
        let accessGroup = makeAccessGroup()

        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecAttrAccessGroup as String: accessGroup,
            kSecAttrSynchronizable as String: kCFBooleanFalse!,
        ]

        let status = SecItemDelete(query as CFDictionary)
        return status == errSecSuccess || status == errSecItemNotFound
    }
}