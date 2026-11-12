import Foundation
import Security
import LocalAuthentication
import CryptoKit

/// This phone's identity with the hub: two P-256 keys that never leave the Secure Enclave.
/// - The device key signs login challenges (no user prompt), so the app and widgets can get a short-lived token.
/// - The secure key needs Face ID for every signature and confirms Secure-tier actions (garage, locks, ovens...).
/// Both live in the App Group's keychain access group so the widgets can use the device key too.
enum DeviceKeys {
    static let deviceTag = "me.dlewis.thomascreek.device"
    static let secureTag = "me.dlewis.thomascreek.secure"

    enum KeyError: LocalizedError {
        case create(String), missing, sign(String), cancelled
        var errorDescription: String? {
            switch self {
            case .create(let m): "Couldn't create this phone's key: \(m)"
            case .missing: "This phone isn't set up with the hub yet."
            case .sign(let m): "Couldn't sign: \(m)"
            case .cancelled: "Cancelled."
            }
        }
    }

    // MARK: public keys for enrollment (X9.63 uncompressed point, base64)

    static func devicePublicKey() throws -> String { try publicKey(for: deviceKey(create: true)) }
    static func securePublicKey() throws -> String { try publicKey(for: secureKey(create: true)) }

    /// Signs the login challenge. No prompt.
    static func signChallenge(_ nonce: String) throws -> String {
        try sign(Data(nonce.utf8), with: deviceKey(create: false))
    }

    /// Signs "<nonce>.<sha256 hex of body>" with the Face ID key. Shows the Face ID sheet with `reason`.
    static func confirm(nonce: String, body: Data?, reason: String) throws -> String {
        let digest = SHA256.hash(data: body ?? Data()).map { String(format: "%02x", $0) }.joined()
        let ctx = LAContext()
        ctx.localizedReason = reason
        ctx.localizedCancelTitle = "Cancel"
        let key = try secureKey(create: false, context: ctx)
        return try sign(Data("\(nonce).\(digest)".utf8), with: key)
    }

    static var hasSecureKey: Bool { (try? secureKey(create: false)) != nil }

    /// Forget both keys (the hub revoked this phone, or Dylan re-enrolls it).
    static func reset() {
        for tag in [deviceTag, secureTag] {
            var q: [String: Any] = [kSecClass as String: kSecClassKey,
                                    kSecAttrApplicationTag as String: Data(tag.utf8)]
            if !AppSettings.groupID.isEmpty { q[kSecAttrAccessGroup as String] = AppSettings.groupID }
            SecItemDelete(q as CFDictionary)
        }
    }

    // MARK: internals

    private static var useEnclave: Bool {
        #if targetEnvironment(simulator)
        false
        #else
        true
        #endif
    }

    private static func deviceKey(create: Bool) throws -> SecKey {
        if let k = find(deviceTag, context: nil) { return k }
        guard create else { throw KeyError.missing }
        return try make(deviceTag, flags: [.privateKeyUsage])
    }

    private static func secureKey(create: Bool, context: LAContext? = nil) throws -> SecKey {
        if let k = find(secureTag, context: context) { return k }
        guard create else { throw KeyError.missing }
        return try make(secureTag, flags: [.privateKeyUsage, .biometryCurrentSet])
    }

    private static func find(_ tag: String, context: LAContext?) -> SecKey? {
        var q: [String: Any] = [kSecClass as String: kSecClassKey,
                                kSecAttrApplicationTag as String: Data(tag.utf8),
                                kSecAttrKeyType as String: kSecAttrKeyTypeECSECPrimeRandom,
                                kSecReturnRef as String: true]
        if !AppSettings.groupID.isEmpty { q[kSecAttrAccessGroup as String] = AppSettings.groupID }
        if let context { q[kSecUseAuthenticationContext as String] = context }
        var out: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess, let out else { return nil }
        return (out as! SecKey)
    }

    private static func make(_ tag: String, flags: SecAccessControlCreateFlags) throws -> SecKey {
        var err: Unmanaged<CFError>?
        var f = flags
        if !useEnclave { f.remove(.privateKeyUsage) }
        guard let access = SecAccessControlCreateWithFlags(nil, kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly, f, &err) else {
            throw KeyError.create(err?.takeRetainedValue().localizedDescription ?? "access control")
        }
        var priv: [String: Any] = [kSecAttrIsPermanent as String: true,
                                   kSecAttrApplicationTag as String: Data(tag.utf8),
                                   kSecAttrAccessControl as String: access]
        if !AppSettings.groupID.isEmpty { priv[kSecAttrAccessGroup as String] = AppSettings.groupID }
        var attrs: [String: Any] = [kSecAttrKeyType as String: kSecAttrKeyTypeECSECPrimeRandom,
                                    kSecAttrKeySizeInBits as String: 256,
                                    kSecPrivateKeyAttrs as String: priv]
        if useEnclave { attrs[kSecAttrTokenID as String] = kSecAttrTokenIDSecureEnclave }
        guard let key = SecKeyCreateRandomKey(attrs as CFDictionary, &err) else {
            throw KeyError.create(err?.takeRetainedValue().localizedDescription ?? "unknown")
        }
        return key
    }

    private static func publicKey(for key: SecKey) throws -> String {
        var err: Unmanaged<CFError>?
        guard let pub = SecKeyCopyPublicKey(key),
              let data = SecKeyCopyExternalRepresentation(pub, &err) as Data? else {
            throw KeyError.create(err?.takeRetainedValue().localizedDescription ?? "public key")
        }
        return data.base64EncodedString()
    }

    private static func sign(_ data: Data, with key: SecKey) throws -> String {
        var err: Unmanaged<CFError>?
        guard let sig = SecKeyCreateSignature(key, .ecdsaSignatureMessageX962SHA256, data as CFData, &err) as Data? else {
            let e = err?.takeRetainedValue()
            if let e, CFErrorGetCode(e) == Int(errSecUserCanceled) || (e as Error as NSError).code == LAError.userCancel.rawValue {
                throw KeyError.cancelled
            }
            throw KeyError.sign(e?.localizedDescription ?? "unknown")
        }
        return sig.base64EncodedString()
    }
}

/// Small generic-password store in the shared access group (the hub's short-lived access token).
enum SharedKeychain {
    static func set(_ value: String?, for account: String) {
        var q: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                kSecAttrService as String: "thomascreek",
                                kSecAttrAccount as String: account]
        if !AppSettings.groupID.isEmpty { q[kSecAttrAccessGroup as String] = AppSettings.groupID }
        SecItemDelete(q as CFDictionary)
        guard let value else { return }
        q[kSecValueData as String] = Data(value.utf8)
        q[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        SecItemAdd(q as CFDictionary, nil)
    }

    static func get(_ account: String) -> String? {
        var q: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                kSecAttrService as String: "thomascreek",
                                kSecAttrAccount as String: account,
                                kSecReturnData as String: true]
        if !AppSettings.groupID.isEmpty { q[kSecAttrAccessGroup as String] = AppSettings.groupID }
        var out: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess, let d = out as? Data else { return nil }
        return String(data: d, encoding: .utf8)
    }
}
