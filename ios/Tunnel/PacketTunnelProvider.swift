import NetworkExtension
import WireGuardKit
import os

/// The packet tunnel that ships inside the app (built only with project-vpn.yml).
/// The app stores the settings the hub issued under providerConfiguration["wg"].
final class PacketTunnelProvider: NEPacketTunnelProvider {
    private let log = Logger(subsystem: "me.dlewis.thomascreek", category: "tunnel")

    private lazy var adapter = WireGuardAdapter(with: self) { [log] _, message in
        log.debug("\(message, privacy: .public)")
    }

    enum TunnelError: LocalizedError {
        case badConfig(String)
        var errorDescription: String? { if case .badConfig(let m) = self { "Tunnel settings: \(m)" } else { nil } }
    }

    override func startTunnel(options: [String: NSObject]?, completionHandler: @escaping (Error?) -> Void) {
        do {
            let config = try makeConfiguration()
            adapter.start(tunnelConfiguration: config) { [log] error in
                if let error { log.error("start failed: \(String(describing: error), privacy: .public)") }
                completionHandler(error)
            }
        } catch {
            completionHandler(error)
        }
    }

    override func stopTunnel(with reason: NEProviderStopReason, completionHandler: @escaping () -> Void) {
        adapter.stop { _ in completionHandler() }
    }

    private func makeConfiguration() throws -> TunnelConfiguration {
        guard let proto = protocolConfiguration as? NETunnelProviderProtocol,
              let wg = proto.providerConfiguration?["wg"] as? [String: Any] else { throw TunnelError.badConfig("missing") }
        guard let pk = (wg["private_key"] as? String).flatMap(PrivateKey.init(base64Key:)) else { throw TunnelError.badConfig("private key") }
        var iface = InterfaceConfiguration(privateKey: pk)
        iface.addresses = (wg["addresses"] as? [String] ?? []).compactMap(IPAddressRange.init(from:))
        iface.dns = (wg["dns"] as? [String] ?? []).compactMap(DNSServer.init(from:))

        guard let peerKey = (wg["peer_public_key"] as? String).flatMap(PublicKey.init(base64Key:)) else { throw TunnelError.badConfig("peer key") }
        var peer = PeerConfiguration(publicKey: peerKey)
        peer.endpoint = (wg["endpoint"] as? String).flatMap(Endpoint.init(from:))
        peer.allowedIPs = (wg["allowed_ips"] as? [String] ?? []).compactMap(IPAddressRange.init(from:))
        if let k = wg["keepalive"] as? Int, k > 0 { peer.persistentKeepAlive = UInt16(k) }

        return TunnelConfiguration(name: "Thomas Creek", interface: iface, peers: [peer])
    }
}
