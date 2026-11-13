import Foundation
import Security
import CryptoKit

/// The app's only connection to the house. Every request goes to the Thomas Creek Hub, which holds all
/// service secrets and checks this phone's permissions. The phone holds a Secure Enclave key and a
/// 15-minute access token, nothing else.
actor HubClient {
    static let shared = HubClient()

    static let decoder: JSONDecoder = { let d = JSONDecoder(); d.keyDecodingStrategy = .convertFromSnakeCase; return d }()
    static let encoder: JSONEncoder = { let e = JSONEncoder(); e.keyEncodingStrategy = .convertToSnakeCase; return e }()

    enum HubError: LocalizedError, Equatable {
        case notEnrolled, unreachable, cancelled
        case forbidden(askParent: Bool, message: String?)
        case confirmRequired
        case server(Int, String)
        var errorDescription: String? {
            switch self {
            case .notEnrolled: "This phone isn't set up with the house yet. Open the app on home Wi-Fi."
            case .unreachable: "Can't reach home right now."
            case .cancelled: "Cancelled."
            case .forbidden(let ask, let m): m ?? (ask ? "Ask a parent for that." : "That isn't allowed from this phone.")
            case .confirmRequired: "That needs Face ID."
            case .server(_, let m): m
            }
        }
    }

    /// Set while Dylan previews the app as someone else ("View as"): every request carries it and the hub
    /// answers as that person, refusing anything that would change the house. Memory only, so it never
    /// survives the app closing.
    nonisolated(unsafe) static var viewAs: String?

    private var lastGood: URL?
    private var token: String? = SharedKeychain.get("access")
    private var tokenExpiry: Date = {
        let t = AppSettings.defaults.double(forKey: "accessExpiry"); return t > 0 ? Date(timeIntervalSince1970: t) : .distantPast
    }()

    private var session: URLSession { Self.session }

    /// Every connection to the hub checks its certificate against the pin learned on home Wi-Fi.
    nonisolated static let session: URLSession = {
        let c = URLSessionConfiguration.ephemeral
        c.timeoutIntervalForRequest = 10
        c.waitsForConnectivity = false
        return URLSession(configuration: c, delegate: HubPinning.shared, delegateQueue: nil)
    }()

    /// For long-lived streams (SSE) and big downloads.
    nonisolated static let streamSession: URLSession = {
        let c = URLSessionConfiguration.default
        c.timeoutIntervalForRequest = 3600
        c.timeoutIntervalForResource = 7 * 24 * 3600
        return URLSession(configuration: c, delegate: HubPinning.shared, delegateQueue: nil)
    }()

    /// Hub base URLs to try: the one that answered last, then home, then the published address.
    func bases() -> [URL] {
        var out = [AppSettings.hubLAN, AppSettings.hubRemote].compactMap { s -> URL? in
            guard !s.isEmpty else { return nil }
            return URL(string: s.hasPrefix("http") ? s : "http://\(s)")
        }
        if let g = lastGood, let i = out.firstIndex(of: g) { out.remove(at: i); out.insert(g, at: 0) }
        return out
    }

    func baseURL() -> URL? { lastGood ?? bases().first }

    // MARK: auth

    private struct Nonce: Decodable { var nonce: String }
    private struct TokenReply: Decodable { var accessToken: String; var expiresAt: String? }

    /// Current access token, logging in again with the device key when it has expired.
    func accessToken() async throws -> String {
        if let token, tokenExpiry.timeIntervalSinceNow > 30 { return token }
        guard let id = AppSettings.deviceID else { throw HubError.notEnrolled }
        let n: Nonce = try await call("POST", "/v1/auth/challenge", body: ["device_id": id], auth: false)
        let sig = try DeviceKeys.signChallenge(n.nonce)
        let t: TokenReply = try await call("POST", "/v1/auth/token",
                                           body: ["device_id": id, "nonce": n.nonce, "signature": sig], auth: false)
        token = t.accessToken
        tokenExpiry = t.expiresAt.flatMap(ISO.parse) ?? Date().addingTimeInterval(14 * 60)
        SharedKeychain.set(t.accessToken, for: "access")
        AppSettings.defaults.set(tokenExpiry.timeIntervalSince1970, forKey: "accessExpiry")
        return t.accessToken
    }

    func signOut() {
        token = nil; tokenExpiry = .distantPast
        SharedKeychain.set(nil, for: "access")
    }

    /// A fresh nonce for a Face ID confirmation.
    func confirmNonce() async throws -> String {
        guard let id = AppSettings.deviceID else { throw HubError.notEnrolled }
        let n: Nonce = try await call("POST", "/v1/auth/challenge", body: ["device_id": id, "purpose": "confirm"], auth: true)
        return n.nonce
    }

    // MARK: requests

    /// JSON call. `confirm` is the Face ID prompt text for Secure-tier actions; nil means never prompt
    /// (widgets and Siri pass nil, so a secure action there fails with `.confirmRequired`).
    func call<T: Decodable>(_ method: String, _ path: String, query: [String: String] = [:], body: [String: Any]? = nil,
                            auth: Bool = true, confirm: String? = nil, as: T.Type = T.self) async throws -> T {
        let data = try await raw(method, path, query: query, body: body.map { try JSONSerialization.data(withJSONObject: $0) },
                                 auth: auth, confirm: confirm)
        if T.self == Empty.self { return Empty() as! T }
        do { return try Self.decoder.decode(T.self, from: data) }
        catch { throw HubError.server(0, "The hub sent something this version of the app doesn't understand (\(path)).") }
    }

    /// GET a reply, saving a copy for offline use. When home can't be reached (or the service behind
    /// the hub is down), the saved copy is returned instead. `keep` files it with the copies that
    /// aren't trimmed (trips that are coming up).
    func get<T: Decodable>(_ path: String, _ query: [String: String] = [:], keep: Bool = false, as: T.Type = T.self) async throws -> T {
        try await fetch(path, query, keep: keep, as: T.self).value
    }

    /// Like `get`, and says whether the answer is a saved copy (and from when).
    func fetch<T: Decodable>(_ path: String, _ query: [String: String] = [:], keep: Bool = false,
                             as: T.Type = T.self) async throws -> (value: T, savedAt: Date?) {
        // A preview's answers belong to someone else: never save them over this phone's own copies.
        let cacheable = OfflineCache.cacheable(path) && Self.viewAs == nil
        let data: Data
        var savedAt: Date?
        do {
            data = try await raw("GET", path, query: query)
            if cacheable { OfflineCache.store(data, path: path, query: query, keep: keep) }
        } catch let e as HubError {
            switch e {
            case .unreachable, .server(500..., _):
                guard cacheable, let saved = OfflineCache.load(path: path, query: query) else { throw e }
                data = saved.data; savedAt = saved.savedAt
                if e == .unreachable { NotificationCenter.default.post(name: .hubUnreachable, object: nil) }
            default: throw e
            }
        }
        do { return (try Self.decoder.decode(T.self, from: data), savedAt) }
        catch { throw HubError.server(0, "The hub sent something this version of the app doesn't understand (\(path)).") }
    }

    /// The saved copy only, for showing something before the hub answers.
    static func saved<T: Decodable>(_ path: String, _ query: [String: String] = [:], as: T.Type = T.self) -> T? {
        OfflineCache.load(path: path, query: query).flatMap { try? decoder.decode(T.self, from: $0.data) }
    }

    func post<T: Decodable>(_ path: String, _ body: [String: Any] = [:], confirm: String? = nil, as: T.Type = T.self) async throws -> T {
        try await call("POST", path, body: body, confirm: confirm)
    }

    /// POST that only has to succeed.
    func send(_ path: String, _ body: [String: Any] = [:], confirm: String? = nil) async throws {
        let _: Empty = try await call("POST", path, body: body, confirm: confirm)
    }

    /// Run an action through the hub's policy check.
    @discardableResult
    func action(_ name: String, target: String? = nil, params: [String: Any] = [:], confirm: String? = nil) async throws -> ActionReply {
        var body: [String: Any] = ["action": name, "params": params]
        if let target { body["target"] = target }
        return try await call("POST", "/v1/actions", body: body, confirm: confirm)
    }

    func raw(_ method: String, _ path: String, query: [String: String] = [:], body: Data? = nil,
             auth: Bool = true, confirm: String? = nil, retried: Bool = false, timeout: TimeInterval? = nil) async throws -> Data {
        let list = bases()
        guard !list.isEmpty else { throw HubError.unreachable }
        var bearer: String?
        if auth { bearer = try await accessToken() }
        var lastError: Error = HubError.unreachable
        for base in list {
            var req = request(base, method, path, query: query, body: body, bearer: bearer, confirmHeader: nil)
            if let timeout { req.timeoutInterval = max(req.timeoutInterval, timeout) }
            let data: Data, resp: URLResponse
            // Slow calls (the assistant thinks for a while) use the long-lived session: the regular one
            // gives up after 10 s of silence whatever the request's own timeout says.
            do { (data, resp) = try await (timeout == nil ? session : Self.streamSession).data(for: req) }
            catch { lastError = HubError.unreachable; continue }
            lastGood = base
            let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            switch code {
            case 200..<300:
                return data
            case 401 where ["revoked", "unknown_device"].contains(Self.errorBody(data)?["error"] as? String ?? ""):
                await revoked()
                throw HubError.notEnrolled
            case 401 where auth && !retried:
                signOut()
                return try await raw(method, path, query: query, body: body, auth: auth, confirm: confirm, retried: true, timeout: timeout)
            case 428:
                guard let confirm else { throw HubError.confirmRequired }
                let nonce = (Self.errorBody(data)?["nonce"] as? String)
                return try await confirmed(base, method, path, query: query, body: body, bearer: bearer, nonce: nonce, reason: confirm)
            case 403:
                let j = Self.errorBody(data)
                throw HubError.forbidden(askParent: j?["ask_parent"] as? Bool ?? false, message: j?["message"] as? String)
            default:
                let j = Self.errorBody(data)
                throw HubError.server(code, (j?["message"] as? String) ?? (j?["error"] as? String) ?? "The hub answered \(code).")
            }
        }
        throw lastError
    }

    /// Sign the action with the Face ID key and send it again.
    private func confirmed(_ base: URL, _ method: String, _ path: String, query: [String: String], body: Data?,
                           bearer: String?, nonce: String?, reason: String) async throws -> Data {
        let n: String
        if let nonce { n = nonce } else { n = try await confirmNonce() }
        let sig: String
        do { sig = try DeviceKeys.confirm(nonce: n, body: body, reason: reason) }
        catch DeviceKeys.KeyError.cancelled { throw HubError.cancelled }
        let req = request(base, method, path, query: query, body: body, bearer: bearer, confirmHeader: "\(n).\(sig)")
        let (data, resp) = try await session.data(for: req)
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(code) else {
            let j = Self.errorBody(data)
            if code == 403 { throw HubError.forbidden(askParent: false, message: j?["message"] as? String) }
            throw HubError.server(code, (j?["message"] as? String) ?? "The hub answered \(code).")
        }
        return data
    }

    private func request(_ base: URL, _ method: String, _ path: String, query: [String: String], body: Data?,
                         bearer: String?, confirmHeader: String?) -> URLRequest {
        // A path may carry its own query ("snapshot.jpg?m=…"); appendingPathComponent would encode the "?".
        let parts = path.split(separator: "?", maxSplits: 1).map(String.init)
        var c = URLComponents(url: base.appendingPathComponent(parts[0]), resolvingAgainstBaseURL: false)!
        var items = parts.count > 1 ? (URLComponents(string: "?" + parts[1])?.queryItems ?? []) : []
        items += query.sorted { $0.key < $1.key }.map { URLQueryItem(name: $0.key, value: $0.value) }
        if !items.isEmpty { c.queryItems = items }
        var r = URLRequest(url: c.url!)
        r.httpMethod = method
        r.httpBody = body
        // A quick try at home first, then a longer wait for the published address.
        r.timeoutInterval = base.absoluteString == AppSettings.hubLAN && bases().count > 1 ? 3 : 10
        if body != nil { r.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        if let bearer { r.setValue("Bearer \(bearer)", forHTTPHeaderField: "Authorization") }
        if let confirmHeader { r.setValue(confirmHeader, forHTTPHeaderField: "X-TC-Confirm") }
        if let v = Self.viewAs { r.setValue(v, forHTTPHeaderField: "X-TC-View-As") }
        r.setValue(Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String, forHTTPHeaderField: "X-TC-App")
        return r
    }

    /// An authorized request for streaming endpoints (SSE) and images.
    func authorizedRequest(_ path: String, query: [String: String] = [:]) async throws -> URLRequest {
        guard let base = baseURL() else { throw HubError.unreachable }
        let t = try await accessToken()
        var r = request(base, "GET", path, query: query, body: nil, bearer: t, confirmHeader: nil)
        r.timeoutInterval = .infinity
        return r
    }

    /// Absolute URL for a hub-relative asset path ("/v1/assets/...") or a full URL as given.
    func resolve(_ s: String) -> URL? {
        if s.hasPrefix("http") { return URL(string: s) }
        guard let base = baseURL() else { return nil }
        return URL(string: s, relativeTo: base)?.absoluteURL
    }

    private func revoked() async {
        signOut()
        OfflineCache.wipeAll()
        AppSettings.deviceID = nil
        DeviceKeys.reset()
    }

    static func errorBody(_ data: Data) -> [String: Any]? {
        try? JSONSerialization.jsonObject(with: data) as? [String: Any]
    }
}

extension Notification.Name {
    /// A screen fell back to a saved copy because home didn't answer.
    static let hubUnreachable = Notification.Name("hubUnreachable")
}

/// For calls whose reply body doesn't matter.
struct Empty: Codable {}

/// Certificate pinning for the hub's self-signed certificate. The pin (hex SHA-256 of the certificate's
/// SubjectPublicKeyInfo) comes from the hub's Bonjour record, read on home Wi-Fi at enrollment.
final class HubPinning: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    static let shared = HubPinning()

    func urlSession(_ session: URLSession, didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        let (d, c) = Self.evaluate(challenge)
        completionHandler(d, c)
    }

    /// Streaming tasks (`bytes(for:)`, downloads) get the server-trust challenge at task level.
    func urlSession(_ session: URLSession, task: URLSessionTask, didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        let (d, c) = Self.evaluate(challenge)
        completionHandler(d, c)
    }

    /// Shared with the web views (camera player, lights pages).
    static func evaluate(_ challenge: URLAuthenticationChallenge) -> (URLSession.AuthChallengeDisposition, URLCredential?) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let trust = challenge.protectionSpace.serverTrust else { return (.performDefaultHandling, nil) }
        var pin = AppSettings.hubPin
        let host = challenge.protectionSpace.host
        if pin.isEmpty, !AppSettings.trustFirstCertFrom.isEmpty, host == AppSettings.trustFirstCertFrom,
           let chain = SecTrustCopyCertificateChain(trust) as? [SecCertificate], let leaf = chain.first, let fp = spkiSHA256(leaf) {
            // Typed-in address on home Wi-Fi: pin the first certificate it shows.
            AppSettings.hubPin = fp; AppSettings.trustFirstCertFrom = ""; pin = fp
        }
        if isPublished(host) { return evaluatePublished(trust) }
        guard !pin.isEmpty, isHub(host) else { return (.performDefaultHandling, nil) }
        guard let chain = SecTrustCopyCertificateChain(trust) as? [SecCertificate], let leaf = chain.first,
              let fp = spkiSHA256(leaf), fp == pin.lowercased() else {
            return (.cancelAuthenticationChallenge, nil)
        }
        return (.useCredential, URLCredential(trust: trust))
    }

    /// The published address (through the reverse proxy) shows a public Let's Encrypt certificate, not the
    /// hub's own key. Accept it only if iOS trusts it for this host AND the chain runs through one of
    /// Let's Encrypt's roots, so a certificate from any other authority is refused. The host itself came
    /// from the hub over the pinned home connection (`/v1/me` remote_url).
    static let publishedRootPins: Set<String> = [
        "0b9fa5a59eed715c26c1020c711b4f6ec42d58b0015e14337a39dad301c5afc3", // ISRG Root X1 (RSA 4096)
        "762195c225586ee6c0237456e2107dc54f1efc21f61a792ebd515913cce68332", // ISRG Root X2 (P-384)
        "b0292ae545978e0fbb98abbd94c861605e5b18bb32ed523f50d5b7b5c71d47bc", // ISRG Root YE (P-384)
    ]

    private static func isPublished(_ host: String) -> Bool {
        guard let remote = URL(string: AppSettings.hubRemote)?.host, remote == host else { return false }
        // If the same name were also the home address, keep the hub's own pin.
        return URL(string: AppSettings.hubLAN)?.host != host
    }

    private static func evaluatePublished(_ trust: SecTrust) -> (URLSession.AuthChallengeDisposition, URLCredential?) {
        guard SecTrustEvaluateWithError(trust, nil),
              let chain = SecTrustCopyCertificateChain(trust) as? [SecCertificate],
              chain.contains(where: { spkiSHA256($0).map(publishedRootPins.contains) ?? false }) else {
            return (.cancelAuthenticationChallenge, nil)
        }
        return (.useCredential, URLCredential(trust: trust))
    }

    private static func isHub(_ host: String) -> Bool {
        [AppSettings.hubLAN, AppSettings.hubRemote].compactMap { URL(string: $0)?.host }.contains(host)
    }

    /// SHA-256 of the DER SubjectPublicKeyInfo (EC P-256/P-384 and RSA 2048/4096 headers).
    static func spkiSHA256(_ cert: SecCertificate) -> String? {
        guard let key = SecCertificateCopyKey(cert),
              let raw = SecKeyCopyExternalRepresentation(key, nil) as Data?,
              let attrs = SecKeyCopyAttributes(key) as? [String: Any] else { return nil }
        let type = attrs[kSecAttrKeyType as String] as? String
        let bits = attrs[kSecAttrKeySizeInBits as String] as? Int ?? 0
        let header: [UInt8]
        if type == (kSecAttrKeyTypeECSECPrimeRandom as String) {
            switch bits {
            case 256: header = [0x30, 0x59, 0x30, 0x13, 0x06, 0x07, 0x2a, 0x86, 0x48, 0xce, 0x3d, 0x02, 0x01, 0x06, 0x08, 0x2a,
                                0x86, 0x48, 0xce, 0x3d, 0x03, 0x01, 0x07, 0x03, 0x42, 0x00]
            case 384: header = [0x30, 0x76, 0x30, 0x10, 0x06, 0x07, 0x2a, 0x86, 0x48, 0xce, 0x3d, 0x02, 0x01, 0x06, 0x05, 0x2b,
                                0x81, 0x04, 0x00, 0x22, 0x03, 0x62, 0x00]
            default: return nil
            }
        } else if type == (kSecAttrKeyTypeRSA as String) {
            switch bits {
            case 2048: header = [0x30, 0x82, 0x01, 0x22, 0x30, 0x0d, 0x06, 0x09, 0x2a, 0x86, 0x48, 0x86, 0xf7, 0x0d, 0x01, 0x01,
                                 0x01, 0x05, 0x00, 0x03, 0x82, 0x01, 0x0f, 0x00]
            case 4096: header = [0x30, 0x82, 0x02, 0x22, 0x30, 0x0d, 0x06, 0x09, 0x2a, 0x86, 0x48, 0x86, 0xf7, 0x0d, 0x01, 0x01,
                                 0x01, 0x05, 0x00, 0x03, 0x82, 0x02, 0x0f, 0x00]
            default: return nil
            }
        } else { return nil }
        let digest = SHA256.hash(data: Data(header) + raw)
        return digest.map { String(format: "%02x", $0) }.joined()
    }
}
