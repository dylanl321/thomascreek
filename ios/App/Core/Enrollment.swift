import Foundation
import Network
import UIKit

/// First launch: find the hub on home Wi-Fi with Bonjour, register this phone with its Secure Enclave keys,
/// and wait for Dylan to say whose phone it is. Nothing to scan or type.
@MainActor
final class Enrollment {
    static let shared = Enrollment()
    private var running = false

    private struct EnrollReply: Decodable { var deviceId: String; var status: String }

    func run() async {
        guard !running else { return }
        running = true; defer { running = false }
        let store = HomeStore.shared

        // 1. Find the hub (or use the address saved from a previous attempt).
        if AppSettings.hubLAN.isEmpty {
            store.phase = .findingHome
            guard let found = await Discovery.find(timeout: 12) else { return }   // stays on the "join home Wi-Fi" screen
            AppSettings.hubPin = found.pin ?? ""
            AppSettings.hubLAN = found.url
        }

        // 2. Register.
        store.phase = .enrolling
        do {
            let publicKey = try DeviceKeys.devicePublicKey()
            var body: [String: Any] = [
                "device_name": UIDevice.current.name,
                "model": Self.modelName,
                "platform": UIDevice.current.userInterfaceIdiom == .pad ? "ipados" : "ios",
                "os_version": UIDevice.current.systemVersion,
                "app_version": Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "",
                "public_key": publicKey,
            ]
            if let secure = try? DeviceKeys.securePublicKey() { body["secure_public_key"] = secure }
            if let t = PushRegistration.shared.token { body["apns_token"] = t; body["apns_env"] = PushRegistration.apsEnvironment }
            let r: EnrollReply = try await HubClient.shared.call("POST", "/v1/enroll", body: body, auth: false)
            AppSettings.deviceID = r.deviceId
            await store.enrolled(status: r.status)
        } catch HubClient.HubError.unreachable {
            AppSettings.hubLAN = ""          // look again next time
            store.phase = .findingHome
        } catch {
            store.phase = .failed(error.localizedDescription)
        }
    }

    /// "iPhone16,2" style identifier; the hub turns it into a friendly name for Dylan's notification.
    static var modelName: String {
        var info = utsname(); uname(&info)
        return withUnsafePointer(to: &info.machine) {
            $0.withMemoryRebound(to: CChar.self, capacity: 1) { String(cString: $0) }
        }
    }
}

/// Bonjour browse for _thomascreek._tcp and resolve it to an IPv4 http URL.
enum Discovery {
    struct Found: Sendable { var url: String; var pin: String? }

    static func find(timeout: TimeInterval) async -> Found? {
        await withCheckedContinuation { (cont: CheckedContinuation<Found?, Never>) in
            let finder = Finder(cont)
            finder.start(timeout: timeout)
        }
    }

    private final class Finder: @unchecked Sendable {
        private var cont: CheckedContinuation<Found?, Never>?
        private var pin: String?
        private var browser: NWBrowser?
        private var connection: NWConnection?
        private let queue = DispatchQueue(label: "thomascreek.discovery")
        private var keepAlive: Finder?

        init(_ c: CheckedContinuation<Found?, Never>) { cont = c }

        func start(timeout: TimeInterval) {
            keepAlive = self
            let b = NWBrowser(for: .bonjourWithTXTRecord(type: "_thomascreek._tcp", domain: nil), using: .tcp)
            browser = b
            b.browseResultsChangedHandler = { [weak self] results, _ in
                guard let self, self.connection == nil, let first = results.first else { return }
                // The hub speaks HTTPS with its own certificate; the TXT record carries the key pin.
                var scheme = "https"
                if case .bonjour(let txt) = first.metadata {
                    self.pin = txt["pin"]
                    if txt["tls"] == "0" { scheme = "http" }
                }
                self.resolve(first.endpoint, scheme: scheme)
            }
            b.stateUpdateHandler = { [weak self] state in
                if case .failed = state { self?.finish(nil) }
            }
            b.start(queue: queue)
            queue.asyncAfter(deadline: .now() + timeout) { [weak self] in self?.finish(nil) }
        }

        private func resolve(_ endpoint: NWEndpoint, scheme: String) {
            let params = NWParameters.tcp
            if let ip = params.defaultProtocolStack.internetProtocol as? NWProtocolIP.Options { ip.version = .v4 }
            let c = NWConnection(to: endpoint, using: params)
            connection = c
            c.stateUpdateHandler = { [weak self] state in
                guard let self else { return }
                switch state {
                case .ready:
                    if case .hostPort(let host, let port) = c.currentPath?.remoteEndpoint {
                        var h = "\(host)"
                        if let pct = h.firstIndex(of: "%") { h = String(h[..<pct]) }
                        self.finish("\(scheme)://\(h):\(port.rawValue)")
                    } else { self.finish(nil) }
                case .failed, .cancelled: self.finish(nil)
                default: break
                }
            }
            c.start(queue: queue)
        }

        private func finish(_ url: String?) {
            queue.async { [self] in
                guard let c = cont else { return }
                cont = nil
                browser?.cancel(); connection?.cancel()
                c.resume(returning: url.map { Found(url: $0, pin: pin) })
                keepAlive = nil
            }
        }
    }
}
