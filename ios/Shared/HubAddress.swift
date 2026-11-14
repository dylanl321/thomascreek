import Foundation

/// Turns a typed or saved hub address into a URL the app can open.
///
/// A schemeless address is https. The hub listens with TLS on port 8443 and the app pins that
/// certificate at enrollment, so prefixing `http://` made a schemeless home address fail.
/// An explicit `http://` URL is kept only when App Transport Security's local-networking
/// exception would allow the load: a numeric IP, a single-label name, or a `.local` name.
/// Any other cleartext address is rejected, so Profile can say why instead of saving a URL
/// the system will block.
enum HubAddress {
    enum Outcome: Equatable {
        case empty
        case url(URL)
        case rejected(String)
    }

    static let malformedMessage = "That isn't a hub address. Try https://10.0.0.5:8443."
    static let insecureMessage = "Use https for that address. Plain http only works for an IP, a .local name, or a single name on the home network."

    /// Blank means "no address" (Profile clears the saved one and Bonjour runs again).
    static func parse(_ raw: String) -> Outcome {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty || trimmed.allSatisfy({ $0 == "/" }) { return .empty }
        let candidate = trimmed.contains("://") ? trimmed : "https://" + trimmed
        guard var parts = URLComponents(string: candidate),
              let scheme = parts.scheme?.lowercased(),
              let host = parts.host, !host.isEmpty else {
            return .rejected(malformedMessage)
        }
        parts.scheme = scheme
        parts.path = ""
        parts.query = nil
        parts.fragment = nil
        parts.user = nil
        parts.password = nil
        guard let url = parts.url else { return .rejected(malformedMessage) }
        switch scheme {
        case "https":
            return .url(url)
        case "http":
            return allowsCleartextHTTP(host) ? .url(url) : .rejected(insecureMessage)
        default:
            return .rejected(malformedMessage)
        }
    }

    /// The URL to connect to, or nil when the string is blank or not allowed.
    static func url(from raw: String) -> URL? {
        if case .url(let url) = parse(raw) { return url }
        return nil
    }

    /// Address as stored and shown: scheme, host, and port, with no trailing slash.
    static func canonical(_ url: URL) -> String {
        var s = url.absoluteString
        while s.hasSuffix("/") { s.removeLast() }
        return s
    }

    /// True when `NSAllowsLocalNetworking` permits cleartext to this host.
    /// Apple treats a numeric IP (any address, v4 or v6), a name with no dot, and a `.local` name as local.
    static func allowsCleartextHTTP(_ host: String) -> Bool {
        var name = host.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if name.hasPrefix("["), let end = name.firstIndex(of: "]") {
            name = String(name[name.index(after: name.startIndex)..<end])
        }
        if let zone = name.firstIndex(of: "%") { name = String(name[..<zone]) }
        if name.hasSuffix(".") { name.removeLast() }
        if name.isEmpty { return false }
        if name.hasSuffix(".local") || isIPv4(name) || isIPv6(name) { return true }
        return !name.contains(".") && !name.contains(":")
    }

    private static func isIPv4(_ host: String) -> Bool {
        let parts = host.split(separator: ".", omittingEmptySubsequences: false)
        guard parts.count == 4 else { return false }
        return parts.allSatisfy { part in
            let text = String(part)
            guard isASCIIDigits(text), let n = Int(text), n <= 255 else { return false }
            return true
        }
    }

    /// Enough of RFC 4291 to recognise an address ATS will treat as an IP literal, including a dotted tail.
    private static func isIPv6(_ host: String) -> Bool {
        let text = host.lowercased()
        guard !text.isEmpty, !text.contains("%") else { return false }
        let pieces = text.components(separatedBy: "::")
        guard pieces.count <= 2 else { return false }
        let compressed = pieces.count == 2
        guard let left = ipv6Groups(pieces[0]) else { return false }
        if !compressed { return left == 8 }
        guard let right = ipv6Groups(pieces[1]) else { return false }
        // "::" has to stand in for at least one group.
        return left + right < 8
    }

    private static func ipv6Groups(_ side: String) -> Int? {
        if side.isEmpty { return 0 }
        var parts = side.split(separator: ":", omittingEmptySubsequences: false).map(String.init)
        if parts.contains(where: \.isEmpty) { return nil }
        var extra = 0
        if let last = parts.last, last.contains(".") {
            guard isIPv4(last) else { return nil }
            parts.removeLast()
            extra = 2
        }
        guard parts.allSatisfy({ isHexGroup($0) }) else { return nil }
        return parts.count + extra
    }

    private static func isHexGroup(_ text: String) -> Bool {
        (1...4).contains(text.count) && text.utf8.allSatisfy { byte in
            (byte >= 48 && byte <= 57) || (byte >= 97 && byte <= 102)
        }
    }

    private static func isASCIIDigits(_ text: String) -> Bool {
        !text.isEmpty && text.utf8.allSatisfy { $0 >= 48 && $0 <= 57 }
    }
}
