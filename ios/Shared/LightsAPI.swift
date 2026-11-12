import Foundation

/// Holiday lights. The show player still does the work; the hub passes these calls through at /v1/lights/*,
/// adds the player's token itself and checks the person may change the lights.
enum LightsAPI {
    private static let hub = HubClient.shared

    @discardableResult static func status() async throws -> ShowStatus {
        let s: ShowStatus = try await hub.get("/v1/lights/status")
        AppSettings.cachedStatus = s
        return s
    }

    @discardableResult static func command(_ path: String, _ query: [String: String] = [:]) async throws -> String {
        // The show player takes its commands as GET; the hub treats them as writes by path.
        let r: ResultReply = try await hub.call("GET", "/v1/lights/\(path)", query: query)
        _ = try? await status()
        AppSettings.refreshSurfaces()
        if r.dryRun == true { return "Practice mode: " + (r.message ?? "nothing changed") }
        return r.result ?? "ok"
    }

    static func look(_ l: Look) async throws -> String { try await command("look", ["name": l.rawValue]) }
    static func start() async throws -> String { try await command("start") }
    static func stop() async throws -> String { try await command("stop") }
    static func next() async throws -> String { try await command("next") }
    static func play(_ scene: String) async throws -> String { try await command("play", ["scene": scene]) }
    static func queue(_ scene: String) async throws -> String { try await command("queue", ["scene": scene]) }
    static func unqueue(_ i: Int) async throws -> String { try await command("unqueue", ["i": String(i)]) }
    static func brightness(percent: Int) async throws -> String {
        try await command("brightness", ["bri": String(max(1, min(255, Int((Double(percent) * 2.55).rounded()))))])
    }
    static func skipTonight(_ skip: Bool) async throws { _ = try await command("schedule", ["skip": skip ? "1" : "0"]) }
    static func schedule(enabled: Bool) async throws { _ = try await command("schedule", ["enabled": enabled ? "1" : "0"]) }

    static func live() async throws -> ShowAttributes.ContentState { try await hub.get("/v1/lights/api/live") }
}
