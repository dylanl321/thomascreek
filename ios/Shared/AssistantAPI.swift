import Foundation

/// Ask Thomas Creek: the hub runs the AI and every step as this person, with this person's limits.
/// Steps that need Face ID (or a delete) come back as confirm cards that the app sends itself.
struct AskReply {
    struct Step: Identifiable, Hashable { let id = UUID(); var label: String; var status: String }
    struct Confirm: Identifiable, Hashable {
        var id: String
        var kind: String          // "faceid" or "tap"
        var summary: String
        var method: String
        var path: String
        var query: [String: String]
        var body: Data?
        static func == (a: Confirm, b: Confirm) -> Bool { a.id == b.id }
        func hash(into h: inout Hasher) { h.combine(id) }
    }
    var conversationID: String
    var reply: String
    var steps: [Step]
    var confirms: [Confirm]
    var changed: Bool
}

extension HubClient {
    /// Whether the hub has the assistant set up (a Claude key on its admin page).
    func assistantReady() async -> Bool {
        guard let d = try? await raw("GET", "/v1/assistant"),
              let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else { return false }
        return j["ready"] as? Bool ?? false
    }

    /// One turn. Parsed by hand: confirm bodies are sent back to the hub byte-for-byte as JSON,
    /// so their keys must not go through the snake_case decoder.
    func ask(_ text: String, conversation: String?, screen: String?,
             confirmed: [(id: String, ok: Bool, message: String?)] = []) async throws -> AskReply {
        var body: [String: Any] = ["text": text]
        if let conversation { body["conversation_id"] = conversation }
        if let screen { body["screen"] = screen }
        if !confirmed.isEmpty {
            body["confirmed"] = confirmed.map { c -> [String: Any] in
                var d: [String: Any] = ["id": c.id, "ok": c.ok]
                if let m = c.message { d["message"] = String(m.prefix(280)) }
                return d
            }
        }
        let data = try await raw("POST", "/v1/assistant", body: JSONSerialization.data(withJSONObject: body), timeout: 60)
        guard let j = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw HubError.server(0, "The hub's answer couldn't be read.")
        }
        let steps = (j["steps"] as? [[String: Any]] ?? []).map {
            AskReply.Step(label: $0["label"] as? String ?? "", status: $0["status"] as? String ?? "")
        }
        let confirms = (j["confirms"] as? [[String: Any]] ?? []).compactMap { c -> AskReply.Confirm? in
            guard let id = c["id"] as? String, let path = c["path"] as? String else { return nil }
            let q = (c["query"] as? [String: Any] ?? [:]).mapValues { "\($0)" }
            let b = (c["body"]).flatMap { $0 is NSNull ? nil : try? JSONSerialization.data(withJSONObject: $0) }
            return AskReply.Confirm(id: id, kind: c["kind"] as? String ?? "tap", summary: c["summary"] as? String ?? "Confirm",
                                    method: c["method"] as? String ?? "POST", path: path, query: q, body: b)
        }
        return AskReply(conversationID: j["conversation_id"] as? String ?? "", reply: j["reply"] as? String ?? "",
                        steps: steps, confirms: confirms, changed: j["changed"] as? Bool ?? false)
    }

    /// Carry out a confirm card. Face ID is asked for when the hub wants it.
    func perform(_ c: AskReply.Confirm) async throws {
        _ = try await raw(c.method, c.path, query: c.query, body: c.body, confirm: c.summary)
    }
}
