import Foundation
import WatchConnectivity

/// The Watch app has no keys of its own: it asks this iPhone app, which talks to the hub.
/// iOS wakes the iPhone app in the background for each Watch message.
/// Messages: {cmd: "state"} → doors, modes, who's home and intercom phrases;
/// {cmd: "action", action, target, params} → runs it ("quick", so the garage opens without Face ID).
@MainActor
final class WatchBridge: NSObject, WCSessionDelegate {
    static let shared = WatchBridge()

    func activate() {
        guard WCSession.isSupported() else { return }
        WCSession.default.delegate = self
        WCSession.default.activate()
    }

    nonisolated func session(_ session: WCSession, activationDidCompleteWith activationState: WCSessionActivationState, error: Error?) {}
    nonisolated func sessionDidBecomeInactive(_ session: WCSession) {}
    nonisolated func sessionDidDeactivate(_ session: WCSession) { session.activate() }

    nonisolated func session(_ session: WCSession, didReceiveMessage message: [String: Any],
                             replyHandler: @escaping ([String: Any]) -> Void) {
        let cmd = message["cmd"] as? String ?? ""
        let action = message["action"] as? String
        let target = message["target"] as? String
        let params = message["params"] as? [String: Any] ?? [:]
        Task { @MainActor in
            switch cmd {
            case "action" where action != nil:
                do {
                    let r = try await HubClient.shared.action(action!, target: target, params: params, quick: true)
                    await HomeStore.shared.loadHome()
                    replyHandler(["ok": true, "message": r.message ?? "Done", "state": Self.state()])
                } catch {
                    replyHandler(["ok": false, "message": error.localizedDescription])
                }
            default:
                await HomeStore.shared.loadHome()
                await FamilyStore.shared.loadPresence()
                replyHandler(["ok": true, "state": Self.state()])
            }
        }
    }

    /// A small, plain snapshot (WatchConnectivity only carries property-list values).
    static func state() -> [String: Any] {
        let home = HomeStore.shared.home
        let me = HomeStore.shared.me
        var out: [String: Any] = ["status": home?.status.line ?? "", "level": home?.status.level ?? "ok",
                                  "name": me?.firstName ?? "", "role": me?.role.rawValue ?? "pending"]
        if me?.can(.doors) == true {
            out["garages"] = (home?.devices(kind: .garage) ?? []).map {
                ["id": $0.id, "name": $0.name, "open": $0["open"]?.bool ?? false] as [String: Any]
            }
            out["locks"] = (home?.devices(kind: .lock) ?? []).map {
                ["id": $0.id, "name": $0.name, "locked": $0["locked"]?.bool ?? true] as [String: Any]
            }
        }
        out["modes"] = (home?.modes ?? []).map { ["id": $0.id, "name": $0.name, "symbol": $0.symbol ?? "sparkles"] }
        out["people"] = FamilyStore.shared.family.map { p -> [String: Any] in
            var d: [String: Any] = ["name": p.first]
            if let h = p.home { d["home"] = h }
            return d
        }
        if me?.can(.familyEdit) == true {
            out["phrases"] = ["Dinner's ready!", "Can everyone come downstairs, please?", "Shoes on, we're leaving in five minutes.",
                              "Time to get ready for bed."]
        }
        return out
    }
}
