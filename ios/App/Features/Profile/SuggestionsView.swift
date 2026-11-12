import SwiftUI

/// Which kinds of "Right now" cards this person sees. Three "Not now" in a row on one kind turns it off;
/// this is where it comes back.
struct SuggestionsView: View {
    @Environment(HomeStore.self) private var store
    @State private var settings: SmartSettings?
    @State private var failed = false

    struct SmartSettings: Codable {
        var muted: [Rule]
        var rules: [Rule]
        struct Rule: Codable, Hashable { var rule: String; var title: String }
    }

    var body: some View {
        List {
            if let s = settings {
                Section {
                    ForEach(s.rules, id: \.self) { r in
                        Toggle(r.title, isOn: Binding(
                            get: { !s.muted.contains { $0.rule == r.rule } },
                            set: { on in Task { await set(r.rule, muted: !on) } }))
                    }
                } footer: {
                    Text("Cards only show when they fit: dinner near dinner time, lock-up at night, bedtime for the kids. Tap × on a card to hide it until next time.")
                }
            } else if failed {
                ContentUnavailableView("Can't reach home", systemImage: "icloud.slash", description: Text("Try again when you're connected."))
            } else {
                ProgressView().frame(maxWidth: .infinity)
            }
        }
        .navigationTitle("Suggestions")
        .task { await load() }
    }

    private func load() async {
        do { settings = try await HubClient.shared.get("/v1/smart/settings") } catch { failed = true }
    }

    private func set(_ rule: String, muted: Bool) async {
        do {
            settings = try await HubClient.shared.post("/v1/smart/settings", ["rule": rule, "muted": muted])
            store.refreshSmart()
        } catch { store.show(error.localizedDescription, isError: true) }
    }
}
