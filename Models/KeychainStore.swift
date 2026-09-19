import Foundation
import Security

/// Minimal Keychain wrapper for exactly one thing: the hosted-API key.
/// Not UserDefaults -- UserDefaults is a plist on disk, readable by
/// anything with access to the user's Library folder, and an API key
/// doesn't belong there. Scoped under Jugnu's own service name via
/// kSecClassGenericPassword, so nothing else on the system can read it
/// back out.
enum KeychainStore {
    private static let service = "com.jugnu.app.apikey"

    static func get(_ account: String) -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var result: AnyObject?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        guard status == errSecSuccess, let data = result as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    /// Empty string deletes the stored value rather than writing an empty
    /// one -- there's no meaningful difference between "no key set" and
    /// "key set to nothing," and treating them the same keeps get()
    /// simple (nil means "not configured," full stop).
    static func set(_ value: String, for account: String) {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]

        if value.isEmpty {
            SecItemDelete(query as CFDictionary)
            return
        }

        let data = Data(value.utf8)
        let updateStatus = SecItemUpdate(query as CFDictionary, [kSecValueData as String: data] as CFDictionary)
        if updateStatus == errSecItemNotFound {
            var addQuery = query
            addQuery[kSecValueData as String] = data
            addQuery[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
            SecItemAdd(addQuery as CFDictionary, nil)
        }
    }
}
