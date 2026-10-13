import Foundation
import Security
import os

public enum SharedKeychain {
    public static let service = "rsios.spike"
    public static let account = "ext_status"
    public static let accessGroupPrefix = "com.wyl2607.rsios.shared"

    private static let logger = Logger(subsystem: "com.wyl2607.rsios.spike", category: "shared-keychain")

    public static func makeAccessGroup() -> String {
        let prefix = Bundle.main.object(forInfoDictionaryKey: "AppIdentifierPrefix") as? String ?? ""
        return "\(prefix)\(accessGroupPrefix)"
    }

    public static func write(_ string: String) -> Bool {
        let accessGroup = makeAccessGroup()
        let data = string.data(using: .utf8)!

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
            logger.debug("Keychain updated: \(string, privacy: .public)")
            return true
        }

        if status == errSecItemNotFound {
            status = SecItemAdd(query as CFDictionary, nil)
            if status == errSecSuccess {
                logger.debug("Keychain added: \(string, privacy: .public)")
                return true
            }
        }

        logger.error("Keychain write failed: \(status)")
        return false
    }

    public static func read() -> String? {
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
                logger.error("Keychain read failed: \(status)")
            }
            return nil
        }

        return string
    }

    public static func delete() -> Bool {
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