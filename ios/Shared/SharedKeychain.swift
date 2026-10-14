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
    }

    private static let logger = Logger(subsystem: "com.wyl2607.rsios.spike", category: "shared-keychain")

    public static func makeAccessGroup() -> String {
        let prefix = Bundle.main.object(forInfoDictionaryKey: "AppIdentifierPrefix") as? String ?? ""
        return "\(prefix)\(accessGroupPrefix)"
    }

    public static func write(_ value: String, account: String) -> Bool {
        let accessGroup = makeAccessGroup()
        let data = value.data(using: .utf8)!

        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecAttrAccessGroup as String: accessGroup,
            kSecValueData as String: data,
            kSecAttrSynchronizable as String: kCFBooleanFalse!,
        ]

        var status = SecItemUpdate(query as CFDictionary, [kSecValueData as String: data] as CFDictionary)
        if status == errSecSuccess {
            logger.debug("Keychain updated account=\(account, privacy: .public)")
            return true
        }

        if status == errSecItemNotFound {
            status = SecItemAdd(query as CFDictionary, nil)
            if status == errSecSuccess {
                logger.debug("Keychain added account=\(account, privacy: .public)")
                return true
            }
        }

        logger.error("Keychain write failed account=\(account, privacy: .public) status=\(status)")
        return false
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