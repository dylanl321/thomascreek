import Foundation
import NetworkExtension
import CryptoKit

/// The built-in way home. The app makes its own WireGuard key (the private key never leaves this phone),
/// the hub hands back the tunnel settings. The person picks Off (default), When I'm away, or Always on in Profile.
/// Only home addresses go through the tunnel. The one unavoidable step is iOS's "Allow VPN" prompt.
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
    /// The person's choice; nothing is installed until they pick something other than Off.
    var mode: Mode { Mode(rawValue: AppSettings.vpnMode) ?? .off }

    private init() {
        NotificationCenter.default.addObserver(forName: .NEVPNStatusDidChange, object: nil, queue: .main) { [weak self] n in
            let s = (n.object as? NEVPNConnection)?.status
            Task { @MainActor in if let s { self?.status = s } }
        }
    }

    private struct Config: Decodable {
        var interface: Interface
        var peer: Peer
        var homeSsids: [String]?
        struct Interface: Decodable { var addresses: [String]; var dns: [String]?; var privateKey: String?; var mtu: Int? }
        struct Peer: Decodable { var publicKey: String; var endpoint: String; var allowedIps: [String]; var persistentKeepalive: Int?; var presharedKey: String? }
    }

    /// Present only in builds made with project-vpn.yml.
    var isAvailable: Bool {
        guard let dir = Bundle.main.builtInPlugInsURL else { return false }
        return FileManager.default.fileExists(atPath: dir.appendingPathComponent("ThomasCreekTunnel.appex").path)
    }

    private(set) var status: NEVPNStatus = .invalid
    /// Why the tunnel isn't set up, in words for the person (nil when fine).
    private(set) var problem: String?
    private var checked = false

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
            if let proto = mgr.protocolConfiguration as? NETunnelProviderProtocol, proto.providerConfiguration?["wg"] != nil,
               mgr.isEnabled {
                try await applyRules(mgr, homeSsids: AppSettings.defaults.stringArray(forKey: "vpnHomeSsids"))
                status = mgr.connection.status; return
            }
            try await install(into: mgr)
        } catch HubClient.HubError.server(let code, _) where code == 400 || code == 503 {
            problem = "Ask Dylan to add a VPN client"
            checked = false
        } catch {
            checked = false
        }
    }

    /// Ask the hub for a fresh configuration (also used after Dylan revokes and re-approves a phone).
    func reinstall() async throws {
        let managers = try await NETunnelProviderManager.loadAllFromPreferences()
        do { try await install(into: managers.first ?? NETunnelProviderManager()) }
        catch HubClient.HubError.server(let code, _) where code == 400 || code == 503 {
            problem = "Ask Dylan to add a VPN client"
            throw HubClient.HubError.server(code, "No VPN client is free. Ask Dylan to add one on the Firewalla.")
        }
    }

    private func install(into mgr: NETunnelProviderManager) async throws {
        let key = Curve25519.KeyAgreement.PrivateKey()
        let cfg: Config = try await HubClient.shared.post("/v1/vpn/config",
                                                          ["wg_public_key": key.publicKey.rawRepresentation.base64EncodedString()])
        let proto = NETunnelProviderProtocol()
        proto.providerBundleIdentifier = (Bundle.main.bundleIdentifier ?? "") + ".tunnel"
        proto.serverAddress = "Thomas Creek"
        // The hub may hand out one of the Firewalla's ready-made clients, private key included.
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
        proto.providerConfiguration = ["wg": wg]
        problem = nil
        mgr.protocolConfiguration = proto
        mgr.localizedDescription = "Thomas Creek"
        mgr.isEnabled = true

        AppSettings.defaults.set(cfg.homeSsids, forKey: "vpnHomeSsids")
        try await applyRules(mgr, homeSsids: cfg.homeSsids)    // first save shows iOS's "Allow VPN Configuration" prompt
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

    /// Change the setting from the Profile screen.
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
        if let mgr = managers.first, (mgr.protocolConfiguration as? NETunnelProviderProtocol)?.providerConfiguration?["wg"] != nil {
            try await applyRules(mgr, homeSsids: AppSettings.defaults.stringArray(forKey: "vpnHomeSsids"))
        } else {
            try await reinstall()
        }
        checked = true
    }

    /// Whether a tunnel setup exists on this phone (the hub has to be reachable once to make one).
    private(set) var isSetUp = false

    /// The Connect button: set up if needed (needs the hub once), then connect now.
    func connectNow() async throws {
        var mgr = try await NETunnelProviderManager.loadAllFromPreferences().first
        if mgr == nil || (mgr?.protocolConfiguration as? NETunnelProviderProtocol)?.providerConfiguration?["wg"] == nil {
            try await reinstall()
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
        isSetUp = (mgr?.protocolConfiguration as? NETunnelProviderProtocol)?.providerConfiguration?["wg"] != nil
        status = mgr?.connection.status ?? .invalid
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
