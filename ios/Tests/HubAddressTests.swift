import Foundation
#if canImport(Darwin)
import Darwin
#elseif canImport(Glibc)
import Glibc
#elseif canImport(Musl)
import Musl
#endif

/// Standalone checks for `HubAddress`. The Xcode project has no iOS test target, so this
/// file is not part of a target. From `ios/`:
///   swiftc -parse-as-library -o /tmp/hub-address-tests Shared/HubAddress.swift Tests/HubAddressTests.swift
///   /tmp/hub-address-tests
@main
struct HubAddressTests {
    static var failed = 0

    static func main() {
        expectURL("10.0.0.5:8443", scheme: "https", host: "10.0.0.5", port: 8443)
        expectURL("  HTTPS://Hub.Example.com/admin?x=1#y  ", scheme: "https", host: "hub.example.com", port: nil)
        expectURL("https://user:secret@10.0.0.5:8443/v1", scheme: "https", host: "10.0.0.5", port: 8443)
        expectURL("http://192.168.1.20:8443/", scheme: "http", host: "192.168.1.20", port: 8443)
        expectURL("HTTP://hub.local:8443", scheme: "http", host: "hub.local", port: 8443)
        expectURL("http://printer:8443", scheme: "http", host: "printer", port: 8443)
        expectURL("http://localhost", scheme: "http", host: "localhost", port: nil)
        expectURL("http://[::1]:8443", scheme: "http", host: "::1", port: 8443)
        expectURL("http://[fe80::1%en0]:8443", scheme: "http", host: "fe80::1%en0", port: 8443)
        expectURL("http://8.8.8.8:8443", scheme: "http", host: "8.8.8.8", port: 8443)
        expectURL("https://hub.example.com", scheme: "https", host: "hub.example.com", port: nil)

        expectRejected("http://hub.example.com", HubAddress.insecureMessage)
        expectRejected("http://hub.example.com:8443", HubAddress.insecureMessage)
        expectRejected("http://printer.home:8443", HubAddress.insecureMessage)
        expectRejected("ftp://10.0.0.5:8443", HubAddress.malformedMessage)
        expectRejected("https://", HubAddress.malformedMessage)
        expectRejected("not a url", HubAddress.malformedMessage)

        check("blank", isEmpty("  "))
        check("slashes", isEmpty("///"))
        check("no secret stored", secretStripped)
        check("canonical strips slash", slashStripped)
        check("path and query stripped", pathStripped)

        check("public ip is cleartext-ok", HubAddress.allowsCleartextHTTP("8.8.8.8"))
        check("dotted name is not", !HubAddress.allowsCleartextHTTP("hub.example.com"))
        check("ipv6", HubAddress.allowsCleartextHTTP("2001:db8::1"))
        check("ipv4-mapped", HubAddress.allowsCleartextHTTP("::ffff:192.0.2.1"))
        check("not an ip", !HubAddress.allowsCleartextHTTP("1.2.3"))
        check("bad ipv6", !HubAddress.allowsCleartextHTTP("1::2::3"))
        check("eight groups", HubAddress.allowsCleartextHTTP("1:2:3:4:5:6:7:8"))
        check("too many groups", !HubAddress.allowsCleartextHTTP("1:2:3:4:5:6:7:8:9"))

        if failed != 0 {
            fputs("\(failed) failed\n", stderr)
            exit(1)
        }
        print("ok")
    }

    static func expectURL(_ raw: String, scheme: String, host: String, port: Int?) {
        guard case .url(let url) = HubAddress.parse(raw) else {
            fail("\(raw) rejected: \(String(describing: HubAddress.parse(raw)))")
            return
        }
        let gotHost = (url.host ?? "").lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "[]"))
        check("\(raw) scheme", url.scheme == scheme)
        check("\(raw) host \(gotHost)", gotHost == host)
        check("\(raw) port \(String(describing: url.port))", url.port == port)
    }

    static func expectRejected(_ raw: String, _ message: String) {
        guard case .rejected(let got) = HubAddress.parse(raw) else {
            fail("\(raw) was \(String(describing: HubAddress.parse(raw)))")
            return
        }
        check("\(raw) message", got == message)
    }

    static func check(_ name: String, _ ok: Bool) {
        if !ok { fail(name) }
    }

    static func fail(_ name: String) {
        failed += 1
        fputs("FAIL \(name)\n", stderr)
    }

    static func isEmpty(_ raw: String) -> Bool {
        if case .empty = HubAddress.parse(raw) { return true }
        return false
    }

    static var secretStripped: Bool {
        guard case .url(let url) = HubAddress.parse("https://user:secret@10.0.0.5:8443") else { return false }
        return !HubAddress.canonical(url).contains("secret")
    }

    static var slashStripped: Bool {
        guard case .url(let url) = HubAddress.parse("https://10.0.0.5:8443/") else { return false }
        return HubAddress.canonical(url) == "https://10.0.0.5:8443"
    }

    static var pathStripped: Bool {
        guard case .url(let url) = HubAddress.parse("https://Hub.Example.com/admin?x=1#y") else { return false }
        let stored = HubAddress.canonical(url)
        return stored == "https://Hub.Example.com"
    }
}
