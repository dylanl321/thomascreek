import Foundation
import Network
import NetworkExtension
import CryptoKit

/// The built-in VPN, with two places to go:
/// - Home: the Firewalla's WireGuard (only home addresses go through it).
/// - Private: Private Internet Access, nearest region, all traffic. The PIA login stays on the hub; the hub
///   registers this phone's own public key with PIA and hands back the server settings.
/// One "Thomas Creek" entry in iOS Settings → VPN; switching swaps its settings, so iOS asks "Allow VPN" only once.
/// The person picks how it connects by itself (Never by default) in the VPN screen.
@MainActor
@Observable
final class VPN {
    static let shared = VPN()

    enum Mode: String, CaseIterable, Identifiable {
        case off, away, always
        var id: String { rawValue }
        var title: String {
            switch self {
            case .off: "Off"
            case .away: "When I'm away from home"
            case .always: "Always on"
            }
        }
    }

    enum Exit: String, CaseIterable, Identifiable {
        case home
        case `private`
        var id: String { rawValue }
        var title: String { self == .home ? "Home" : "Private" }
        var symbol: String { self == .home ? "house.fill" : "globe" }
        var blurb: String { self == .home ? "Reach the cameras, NAS and other home devices" : "PIA, nearest region. For hotel and public Wi-Fi" }
    }

    struct Region: Decodable, Hashable {
        var id: String
        var name: String
        var country: String?
        var auto: Bool?
        var probe: String?
    }

    struct Stats: Equatable {
        var received: Int64
        var sent: Int64
        var lastHandshake: Date?
    }

    /// How it connects by itself; nothing connects by itself while it's Off.
    var mode: Mode { Mode(rawValue: AppSettings.vpnMode) ?? .off }
    /// Where it goes: Home (default) or Private.
    var exit: Exit { Exit(rawValue: AppSettings.vpnExit) ?? .home }

    private init() {
        NotificationCenter.default.addObserver(forName: .NEVPNStatusDidChange, object: nil, queue: .main) { [weak self] n in
            let c = n.object as? NEVPNConnection
            Task { @MainActor in
                guard let self, let c else { return }
                self.status = c.status
                self.connectedSince = c.connectedDate
            }
        }
    }

    private struct Config: Decodable {
        var interface: Interface
        var peer: Peer
        var homeSsids: [String]?
        var exit: String?
        var region: Region?
        struct Interface: Decodable { var addresses: [String]; var dns: [String]?; var privateKey: String?; var mtu: Int? }
        struct Peer: Decodable { var publicKey: String; var endpoint: String; var allowedIps: [String]; var persistentKeepalive: Int?; var presharedKey: String? }
    }

    private struct Exits: Decodable {
        struct Item: Decodable { var id: String; var available: Bool; var problem: String? }
        var exits: [Item]
        var regions: [Region]?
    }

    /// Present only in builds made with project-vpn.yml.
    var isAvailable: Bool {
        guard let dir = Bundle.main.builtInPlugInsURL else { return false }
        return FileManager.default.fileExists(atPath: dir.appendingPathComponent("ThomasCreekTunnel.appex").path)
    }

    private(set) var status: NEVPNStatus = .invalid
    private(set) var connectedSince: Date?
    /// Why the tunnel isn't set up, in words for the person (nil when fine).
    private(set) var problem: String?
    /// Whether this person gets the Private button (from the hub; remembered for when home can't be reached).
    private(set) var privateAvailable = AppSettings.defaults.bool(forKey: "vpnPrivateAvailable")
    /// The region the current Private settings point at.
    private(set) var regionName: String? = AppSettings.defaults.string(forKey: "vpnRegionName")
    private var regions: [Region] = []
    private var checked = false

    /// Whether a tunnel setup exists on this phone (the hub has to be reachable once to make one).
    private(set) var isSetUp = false

    // MARK: - What the hub offers

    /// Ask the hub which buttons this person gets (and the Private regions to time).
    func loadExits() async {
        guard let e: Exits = try? await HubClient.shared.get("/v1/vpn/exits") else { return }
        privateAvailable = e.exits.first { $0.id == "private" }?.available ?? false
        AppSettings.defaults.set(privateAvailable, forKey: "vpnPrivateAvailable")
        regions = e.regions ?? []
        if !privateAvailable && exit == .private && status != .connected { AppSettings.vpnExit = Exit.home.rawValue }
    }

    // MARK: - Install and keep fresh

    /// Install (or refresh) the tunnel once per launch. Safe to call often.
    func ensureInstalled() async {
        guard isAvailable, !checked else { return }
        guard mode != .off else {
            // Off: keep the setup (so Connect works away from home) but never connect by itself.
            if let mgr = try? await NETunnelProviderManager.loadAllFromPreferences().first {
                if mgr.isOnDemandEnabled { mgr.isOnDemandEnabled = false; try? await mgr.saveToPreferences() }
                status = mgr.connection.status
            } else { status = .invalid }
            return
        }
        checked = true
        do {
            let managers = try await NETunnelProviderManager.loadAllFromPreferences()
            let mgr = managers.first ?? NETunnelProviderManager()
            if mgr.isEnabled, isCurrent(mgr, maxAge: exit == .private ? 6 * 3600 : nil) {
                try await applyRules(mgr, homeSsids: AppSettings.defaults.stringArray(forKey: "vpnHomeSsids"))
                status = mgr.connection.status; return
            }
            try await install(into: mgr)
        } catch HubClient.HubError.server(let code, let msg) where [400, 403, 502, 503].contains(code) {
            problem = friendly(code, msg)
            checked = false
        } catch {
            checked = false
        }
    }

    /// Settings exist for the chosen place (and, for Private, were fetched recently enough: PIA forgets keys).
    private func isCurrent(_ mgr: NETunnelProviderManager, maxAge: TimeInterval?) -> Bool {
        guard let pc = (mgr.protocolConfiguration as? NETunnelProviderProtocol)?.providerConfiguration,
              pc["wg"] != nil else { return false }
        let configured = Exit(rawValue: pc["exit"] as? String ?? "home") ?? .home
        guard configured == exit else { return false }
        if let maxAge {
            let fetched = pc["fetched"] as? Double ?? 0
            return Date().timeIntervalSince1970 - fetched < maxAge
        }
        return true
    }

    /// Ask the hub for fresh settings for the chosen place.
    func reinstall() async throws {
        let managers = try await NETunnelProviderManager.loadAllFromPreferences()
        do { try await install(into: managers.first ?? NETunnelProviderManager()) }
        catch HubClient.HubError.server(let code, let msg) where [400, 403, 502, 503].contains(code) {
            problem = friendly(code, msg)
            throw HubClient.HubError.server(code, problem ?? msg)
        }
    }

    private func friendly(_ code: Int, _ msg: String) -> String {
        if exit == .home { return code == 503 ? "Home VPN isn't set up yet" : "Ask Dylan to add a VPN client" }
        return switch code {
        case 403: "Private VPN isn't turned on for you"
        case 503: "Private VPN isn't set up yet"
        default: "PIA didn't answer. Try again in a minute."
        }
    }

    private func install(into mgr: NETunnelProviderManager) async throws {
        let place = exit
        let key: Curve25519.KeyAgreement.PrivateKey
        var body: [String: Any]
        if place == .private {
            // Kept in the keychain: PIA re-registers the same key on every Connect.
            key = Self.privateKey()
            if regions.isEmpty { await loadExits() }
            let region = await nearestRegion()
            body = ["exit": "private", "wg_public_key": key.publicKey.rawRepresentation.base64EncodedString()]
            if let region { body["region"] = region.id }
        } else {
            key = Curve25519.KeyAgreement.PrivateKey()
            body = ["wg_public_key": key.publicKey.rawRepresentation.base64EncodedString()]
        }
        let cfg: Config = try await HubClient.shared.post("/v1/vpn/config", body)
        let proto = NETunnelProviderProtocol()
        proto.providerBundleIdentifier = (Bundle.main.bundleIdentifier ?? "") + ".tunnel"
        proto.serverAddress = place == .home ? "Thomas Creek Home" : "Private (PIA)"
        // For Home the hub may hand out one of the Firewalla's ready-made clients, private key included.
        var wg: [String: Any] = [
            "private_key": cfg.interface.privateKey ?? key.rawRepresentation.base64EncodedString(),
            "addresses": cfg.interface.addresses,
            "dns": cfg.interface.dns ?? [],
            "peer_public_key": cfg.peer.publicKey,
            "endpoint": cfg.peer.endpoint,
            "allowed_ips": cfg.peer.allowedIps,
            "keepalive": cfg.peer.persistentKeepalive ?? 25,
        ]
        if let m = cfg.interface.mtu { wg["mtu"] = m }
        if let p = cfg.peer.presharedKey { wg["preshared_key"] = p }
        proto.providerConfiguration = ["wg": wg, "exit": place.rawValue,
                                       "region": cfg.region?.name ?? "", "fetched": Date().timeIntervalSince1970]
        problem = nil
        regionName = cfg.region?.name
        AppSettings.defaults.set(regionName, forKey: "vpnRegionName")
        mgr.protocolConfiguration = proto
        mgr.localizedDescription = "Thomas Creek"
        mgr.isEnabled = true

        if let ssids = cfg.homeSsids, !ssids.isEmpty { AppSettings.defaults.set(ssids, forKey: "vpnHomeSsids") }
        try await applyRules(mgr, homeSsids: AppSettings.defaults.stringArray(forKey: "vpnHomeSsids"))  // first save shows iOS's "Allow VPN Configuration" prompt
        isSetUp = true
    }

    private static func privateKey() -> Curve25519.KeyAgreement.PrivateKey {
        if let s = SharedKeychain.get("vpn.private.key"), let d = Data(base64Encoded: s),
           let k = try? Curve25519.KeyAgreement.PrivateKey(rawRepresentation: d) { return k }
        let k = Curve25519.KeyAgreement.PrivateKey()
        SharedKeychain.set(k.rawRepresentation.base64EncodedString(), for: "vpn.private.key")
        return k
    }

    /// Away: off on home Wi-Fi, on anywhere else. Always: on everywhere.
    private func applyRules(_ mgr: NETunnelProviderManager, homeSsids: [String]?) async throws {
        var rules: [NEOnDemandRule] = []
        if mode == .off {
            mgr.isOnDemandEnabled = false
            mgr.isEnabled = true
            try await mgr.saveToPreferences()
            try await mgr.loadFromPreferences()
            status = mgr.connection.status
            return
        }
        if mode == .away, let ssids = homeSsids, !ssids.isEmpty {
            let home = NEOnDemandRuleDisconnect(); home.interfaceTypeMatch = .wiFi; home.ssidMatch = ssids
            rules.append(home)
        }
        rules.append(NEOnDemandRuleConnect())
        mgr.onDemandRules = rules
        mgr.isOnDemandEnabled = true
        mgr.isEnabled = true
        try await mgr.saveToPreferences()
        try await mgr.loadFromPreferences()
        status = mgr.connection.status
    }

    // MARK: - Nearest Private region

    /// The PIA region that answers fastest from here (like PIA's own app). Remembered for 30 minutes,
    /// and never timed through the tunnel itself.
    private func nearestRegion() async -> Region? {
        let d = AppSettings.defaults
        let cachedID = d.string(forKey: "vpnRegionID")
        let cachedAt = d.double(forKey: "vpnRegionAt")
        let fresh = Date().timeIntervalSince1970 - cachedAt < 30 * 60
        if let cachedID, fresh || status == .connected || status == .connecting {
            return regions.first { $0.id == cachedID } ?? Region(id: cachedID, name: cachedID)
        }
        let candidates = regions.filter { $0.auto ?? true }.compactMap { r in r.probe.map { (r, $0) } }
        guard !candidates.isEmpty else { return cachedID.map { Region(id: $0, name: $0) } }
        let best = await withTaskGroup(of: (Region, Double?).self) { group in
            for (r, target) in candidates { group.addTask { (r, await Self.connectTime(target)) } }
            var best: (Region, Double)?
            for await (r, t) in group { if let t, t < (best?.1 ?? .infinity) { best = (r, t) } }
            return best?.0
        }
        if let best {
            d.set(best.id, forKey: "vpnRegionID")
            d.set(Date().timeIntervalSince1970, forKey: "vpnRegionAt")
        }
        return best ?? cachedID.map { Region(id: $0, name: $0) }
    }

    /// Seconds to open a TCP connection to host:port, or nil after 1.5 s.
    nonisolated private static func connectTime(_ target: String) async -> Double? {
        let parts = target.split(separator: ":")
        guard parts.count == 2, let port = NWEndpoint.Port(String(parts[1])) else { return nil }
        let conn = NWConnection(host: NWEndpoint.Host(String(parts[0])), port: port, using: .tcp)
        let queue = DispatchQueue(label: "vpn.probe")
        let start = DispatchTime.now()
        return await withCheckedContinuation { (cont: CheckedContinuation<Double?, Never>) in
            final class Once: @unchecked Sendable { var done = false }
            let once = Once()
            let finish: @Sendable (Double?) -> Void = { v in
                guard !once.done else { return }
                once.done = true
                conn.cancel()
                cont.resume(returning: v)
            }
            conn.stateUpdateHandler = { state in
                switch state {
                case .ready: finish(Double(DispatchTime.now().uptimeNanoseconds - start.uptimeNanoseconds) / 1e9)
                case .failed, .cancelled: finish(nil)
                case .waiting: finish(nil)
                default: break
                }
            }
            conn.start(queue: queue)
            queue.asyncAfter(deadline: .now() + 1.5) { finish(nil) }
        }
    }

    // MARK: - Buttons

    /// Change how it connects by itself.
    func setMode(_ m: Mode) async throws {
        AppSettings.vpnMode = m.rawValue
        checked = false
        if m == .off {
            if let mgr = try? await NETunnelProviderManager.loadAllFromPreferences().first {
                mgr.isOnDemandEnabled = false
                try? await mgr.saveToPreferences()
                mgr.connection.stopVPNTunnel()
                status = mgr.connection.status
            } else { status = .invalid }
            return
        }
        let managers = try await NETunnelProviderManager.loadAllFromPreferences()
        if let mgr = managers.first, isCurrent(mgr, maxAge: exit == .private ? 6 * 3600 : nil) {
            try await applyRules(mgr, homeSsids: AppSettings.defaults.stringArray(forKey: "vpnHomeSsids"))
        } else {
            try await reinstall()
        }
        checked = true
    }

    /// Switch between Home and Private. If it was connected, it reconnects to the new place.
    func setExit(_ e: Exit) async throws {
        guard e != exit else { return }
        let mgr = try? await NETunnelProviderManager.loadAllFromPreferences().first
        let wasOn = [.connected, .connecting, .reasserting].contains(mgr?.connection.status ?? .invalid)
        AppSettings.vpnExit = e.rawValue
        problem = nil
        if wasOn, let mgr {
            mgr.connection.stopVPNTunnel()
            for _ in 0..<30 where mgr.connection.status != .disconnected && mgr.connection.status != .invalid {
                try? await Task.sleep(for: .milliseconds(100))
            }
        }
        if wasOn {
            try await connectNow()
        } else if mode != .off {
            try await reinstall()
        }
    }

    /// The Connect button: get settings for the chosen place (Private always gets fresh ones), then connect now.
    func connectNow() async throws {
        var mgr = try await NETunnelProviderManager.loadAllFromPreferences().first
        let needs = mgr.map { !isCurrent($0, maxAge: exit == .private ? 60 : nil) } ?? true
        if needs {
            do { try await reinstall() }
            catch {
                // Home can't be reached: use the last settings for this place if there are any.
                guard let m = mgr, isCurrent(m, maxAge: nil) else { throw error }
            }
            mgr = try await NETunnelProviderManager.loadAllFromPreferences().first
        }
        guard let mgr else { return }
        if !mgr.isEnabled { mgr.isEnabled = true; try await mgr.saveToPreferences(); try await mgr.loadFromPreferences() }
        isSetUp = true
        try mgr.connection.startVPNTunnel()
        status = mgr.connection.status
    }

    /// The Disconnect button: stop now, and stop it reconnecting by itself (setting goes to Off).
    func disconnectNow() async {
        if mode != .off { try? await setMode(.off) }
        guard let mgr = try? await NETunnelProviderManager.loadAllFromPreferences().first else { return }
        mgr.connection.stopVPNTunnel()
        status = mgr.connection.status
    }

    /// Read the current state (for the Home card on appear).
    func refreshStatus() async {
        guard isAvailable else { return }
        let mgr = try? await NETunnelProviderManager.loadAllFromPreferences().first
        let pc = (mgr?.protocolConfiguration as? NETunnelProviderProtocol)?.providerConfiguration
        isSetUp = pc?["wg"] != nil
        if let r = pc?["region"] as? String, !r.isEmpty { regionName = r }
        status = mgr?.connection.status ?? .invalid
        connectedSince = mgr?.connection.connectedDate
    }

    /// Bytes and the last handshake, from the tunnel extension (nil when not connected).
    func stats() async -> Stats? {
        guard let mgr = try? await NETunnelProviderManager.loadAllFromPreferences().first,
              mgr.connection.status == .connected,
              let session = mgr.connection as? NETunnelProviderSession else { return nil }
        let data: Data? = await withCheckedContinuation { cont in
            do { try session.sendProviderMessage(Data("stats".utf8)) { cont.resume(returning: $0) } }
            catch { cont.resume(returning: nil) }
        }
        guard let data, let text = String(data: data, encoding: .utf8) else { return nil }
        return Self.parseStats(text)
    }

    /// WireGuard's runtime settings ("key=value" lines): add up every peer's counters.
    nonisolated static func parseStats(_ text: String) -> Stats {
        var rx: Int64 = 0, tx: Int64 = 0, hs: Int64 = 0
        for line in text.split(separator: "\n") {
            let kv = line.split(separator: "=", maxSplits: 1)
            guard kv.count == 2, let v = Int64(kv[1]) else { continue }
            switch kv[0] {
            case "rx_bytes": rx += v
            case "tx_bytes": tx += v
            case "last_handshake_time_sec": hs = max(hs, v)
            default: break
            }
        }
        return Stats(received: rx, sent: tx, lastHandshake: hs > 0 ? Date(timeIntervalSince1970: TimeInterval(hs)) : nil)
    }

    func remove() async {
        guard let managers = try? await NETunnelProviderManager.loadAllFromPreferences() else { return }
        for m in managers {
            m.isOnDemandEnabled = false
            try? await m.saveToPreferences()
            m.connection.stopVPNTunnel()
            try? await m.removeFromPreferences()
        }
        checked = false
    }
}
