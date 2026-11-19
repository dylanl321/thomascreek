import SwiftUI

/// Parents: how grown-up a title each child may watch. The hub keeps the level and filters every row,
/// search and play on every TV, phone and iPad by it (`GET /v1/kids/ratings`, `PUT /v1/people/{id}/ratings`).
struct KidRatingsView: View {
    @Environment(HomeStore.self) private var store
    @State private var answer: KidRatings?
    @State private var problem: String?
    @State private var saving: String?

    var body: some View {
        List {
            if let answer {
                Section {
                    ForEach(answer.kids) { kid in
                        VStack(alignment: .leading, spacing: 10) {
                            HStack {
                                Avatar(name: kid.name ?? "", size: 36)
                                VStack(alignment: .leading, spacing: 1) {
                                    Text(kid.name ?? "").font(.headline)
                                    Text(saving == kid.person ? "Saving…" : label(kid.level, in: answer))
                                        .font(.subheadline).foregroundStyle(Theme.dim)
                                }
                            }
                            if answer.canChange == true {
                                Picker("Level", selection: Binding(get: { kid.level }, set: { new in Task { await set(kid, new) } })) {
                                    ForEach(answer.levels ?? []) { Text($0.id.capitalized).tag($0.id) }
                                }
                                .pickerStyle(.segmented)
                                .disabled(saving != nil)
                            }
                        }
                        .padding(.vertical, 4)
                    }
                } footer: {
                    Text("Every TV, phone and iPad follows this at once. Titles without a rating still follow the kids' categories.")
                }
            } else if let problem {
                Text(problem).foregroundStyle(Theme.dim)
            } else {
                ProgressView("Loading the kids' ratings…")
            }
        }
        .navigationTitle("Kids' ratings")
        .task { await load() }
        .refreshable { await load() }
    }

    private func label(_ level: String, in answer: KidRatings) -> String {
        answer.levels?.first { $0.id == level }?.label ?? level
    }

    private func load() async {
        do {
            answer = try await HubClient.shared.get("/v1/kids/ratings", as: KidRatings.self)
            problem = nil
        } catch {
            problem = "Couldn't load the kids' ratings: \(error.localizedDescription)"
        }
    }

    private func set(_ kid: KidRatings.Kid, _ level: String) async {
        guard level != kid.level, saving == nil else { return }
        saving = kid.person
        defer { saving = nil }
        do {
            let now: KidRatings.Kid = try await HubClient.shared.call("PUT", "/v1/people/\(kid.person)/ratings", body: ["level": level])
            answer?.kids = (answer?.kids ?? []).map { $0.person == now.person ? now : $0 }
            store.show("\(kid.name ?? "Your child") now watches at \(level)")
        } catch {
            store.show("Couldn't change the rating: \(error.localizedDescription)", isError: true)
        }
    }
}
