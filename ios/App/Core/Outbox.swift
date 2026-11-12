import Foundation
import Observation

/// Things done while home couldn't be reached (a shopping item, a chore ticked, a request, where a
/// downloaded film was stopped), sent in order once home answers again.
///
/// Only harmless, "add a note" kinds of changes go here. Lights, locks, the garage and every other
/// device action are never queued: a command that fires hours later is unsafe.
@MainActor
@Observable
final class Outbox {
    static let shared = Outbox()

    struct Item: Codable, Identifiable, Hashable {
        var id = UUID()
        var path: String
        var body: Data?
        /// Shown on the Storage screen ("Sunscreen (shopping)"); nil for quiet ones like play progress.
        var label: String?
        var createdAt = Date()
        var attempts = 0
    }

    private(set) var items: [Item] = []
    var pending: Int { items.count }
    var visible: [Item] { items.filter { $0.label != nil } }
    private var flushing = false

    private var file: URL { OfflineCache.root.appendingPathComponent("outbox.json") }

    init() { load() }

    /// Send now, or keep it for later if home can't be reached. Returns false when it was queued.
    /// Other errors (not allowed, bad request) are thrown so the screen can say so.
    @discardableResult
    func send(_ path: String, _ body: [String: Any] = [:], label: String?) async throws -> Bool {
        let data = try JSONSerialization.data(withJSONObject: body)
        do {
            _ = try await HubClient.shared.raw("POST", path, body: data)
            return true
        } catch HubClient.HubError.unreachable {
            items.append(Item(path: path, body: data, label: label))
            save()
            HomeStore.shared.markUnreachable()
            return false
        }
    }

    /// Queue without trying first (used when we already know we're offline).
    func enqueue(_ path: String, _ body: [String: Any], label: String?) {
        let data = try? JSONSerialization.data(withJSONObject: body)
        // Progress reports replace the earlier one for the same title.
        if label == nil { items.removeAll { $0.path == path && $0.label == nil } }
        items.append(Item(path: path, body: data, label: label))
        save()
    }

    func flush() async {
        guard !flushing, !items.isEmpty else { return }
        flushing = true; defer { flushing = false }
        var sent: [String] = []
        while let first = items.first {
            do {
                _ = try await HubClient.shared.raw("POST", first.path, body: first.body)
                if let l = first.label { sent.append(l) }
                items.removeFirst()
            } catch HubClient.HubError.unreachable {
                break
            } catch {
                // The hub said no (it changed while we were away). Drop it rather than retry forever.
                items.removeFirst()
            }
            save()
        }
        save()
        if !sent.isEmpty {
            HomeStore.shared.show("Home is back", sent.count == 1 ? "Sent: \(sent[0])" : "Sent \(sent.count) changes made offline")
        }
    }

    func discard(_ item: Item) { items.removeAll { $0.id == item.id }; save() }

    private func load() {
        items = (try? Data(contentsOf: file)).flatMap { try? JSONDecoder().decode([Item].self, from: $0) } ?? []
    }

    private func save() {
        if let d = try? JSONEncoder().encode(items) { OfflineCache.write(d, to: file) }
    }

    /// The owner changed: what was waiting belonged to someone else.
    func reset() { items = []; load() }
}
