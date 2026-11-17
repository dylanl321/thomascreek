import SwiftUI

/// A chore a kid reported done, or a reward they redeemed, waiting for a parent on the family board.
/// The hub lists and answers them (GET/POST /v1/family/approvals); adults also get a push with Approve.
struct BoardApproval: Decodable, Identifiable, Hashable {
    var kind: String
    var id: String
    var title: String
    var person: String
    var date: String?
    var note: String?
    var points: Int?

    var key: String { "\(kind):\(id)" }
    var line: String { kind == "chore" ? "\(person) says they did \(title)" : "\(person) wants \(title)" }
    var detail: String? {
        var bits: [String] = []
        if let points, points > 0 { bits.append("\(points) points") }
        if let note, !note.isEmpty { bits.append("\u{201C}\(note)\u{201D}") }
        return bits.isEmpty ? nil : bits.joined(separator: " · ")
    }
}

/// Shown to grown-ups on the Family tab, only while something is waiting.
struct ApprovalsSection: View {
    @Environment(HomeStore.self) private var store
    @State private var items: [BoardApproval] = []
    @State private var busy: Set<String> = []
    @State private var note: String?
    private struct Reply: Decodable { var items: [BoardApproval] }

    var body: some View {
        // A VStack, not a Group: with nothing waiting it must still exist so its task runs.
        VStack(alignment: .leading, spacing: 12) {
            if !items.isEmpty || note != nil {
                SectionHeader("Waiting for a parent")
                RowsCard {
                    if let note { LoadFailureNote(message: note) { await load() } }
                    ForEach(items) { a in
                        InfoRow(symbol: a.kind == "chore" ? "checkmark.seal.fill" : "gift.fill", title: a.line, subtitle: a.detail) {
                            if busy.contains(a.key) {
                                ProgressView()
                            } else {
                                HStack(spacing: 8) {
                                    Button("Not now") { Task { await answer(a, approve: false) } }
                                        .buttonStyle(.bordered)
                                    Button("Approve") { Task { await answer(a, approve: true) } }
                                        .buttonStyle(.borderedProminent).tint(Theme.accent)
                                }
                                .font(.footnote.weight(.semibold))
                            }
                        }
                    }
                }
            }
        }
        .task { await load() }
    }

    private func load() async {
        do {
            items = try await HubClient.shared.get("/v1/family/approvals", as: Reply.self).items
            note = nil
        } catch HubClient.HubError.server(409, _) {
            // The board's key isn't saved on the hub yet: nothing to answer here, so stay out of the way.
            items = []
            note = nil
        } catch HubClient.HubError.forbidden {
            items = []
            note = nil
        } catch {
            note = HubFailure.line(error)
        }
    }

    private func answer(_ a: BoardApproval, approve: Bool) async {
        busy.insert(a.key)
        defer { busy.remove(a.key) }
        do {
            try await HubClient.shared.send("/v1/family/approvals/\(a.kind)/\(a.id)", ["approve": approve])
            items.removeAll { $0.key == a.key }
            store.show(approve ? "Approved" : "Not now", a.line)
        } catch {
            store.failed("Couldn't send that answer", error)
        }
    }
}
