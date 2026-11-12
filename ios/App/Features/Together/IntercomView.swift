import SwiftUI

/// Say something through the house speakers: big ready-made phrases, or type (or dictate) your own.
struct IntercomView: View {
    @Environment(HomeStore.self) private var store
    @State private var info: IntercomInfo?
    @State private var loadError: String?
    @State private var rooms: Set<String> = []
    @State private var text = ""
    @State private var sending: String?
    @State private var sent: String?
    @FocusState private var typing: Bool

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                HStack(spacing: 12) {
                    Image(systemName: "megaphone.fill").font(.system(size: 30)).foregroundStyle(Theme.accent)
                        .symbolEffect(.variableColor.iterative, isActive: sending != nil)
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Intercom").font(.system(.title2, design: .rounded, weight: .bold))
                        Text(roomsLine).font(.subheadline).foregroundStyle(Theme.dim)
                    }
                }
                .padding(.top, 8)

                if let rs = info?.rooms, !rs.isEmpty {
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 8) {
                            ChipButton(title: "Everywhere", symbol: "house.fill", active: rooms.isEmpty) { rooms = [] }
                            ForEach(rs) { r in
                                ChipButton(title: r.name, active: rooms.contains(r.id)) {
                                    if rooms.contains(r.id) { rooms.remove(r.id) } else { rooms.insert(r.id) }
                                }
                            }
                        }
                    }
                }

                LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible(), spacing: 10)], spacing: 10) {
                    ForEach(info?.phrases ?? []) { p in
                        Button { announce(p.text) } label: {
                            VStack(alignment: .leading, spacing: 8) {
                                Text(p.emoji).font(.system(size: 34))
                                Text(p.text).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.fg)
                                    .multilineTextAlignment(.leading).fixedSize(horizontal: false, vertical: true)
                                Spacer(minLength: 0)
                            }
                            .frame(maxWidth: .infinity, minHeight: 110, alignment: .topLeading)
                            .card(padding: 14, color: sent == p.text ? Theme.accent.opacity(0.18) : Theme.card)
                            .overlay(alignment: .topTrailing) {
                                if sending == p.text { ProgressView().padding(12) }
                                else if sent == p.text { Image(systemName: "checkmark.circle.fill").foregroundStyle(Theme.accent).padding(12) }
                            }
                        }
                        .buttonStyle(.plain)
                        .disabled(sending != nil)
                    }
                }

                VStack(alignment: .leading, spacing: 10) {
                    Text("Say something else").font(.headline)
                    TextField("Type, or tap the mic on the keyboard", text: $text, axis: .vertical)
                        .lineLimit(1...4).focused($typing)
                        .padding(12).background(Theme.card2, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                    PrimaryButton(title: "Announce", symbol: "speaker.wave.3.fill") { announce(text); typing = false }
                        .disabled(text.trimmingCharacters(in: .whitespaces).isEmpty || sending != nil)
                }
                .card()
            }
            .screen()
        }
        .background(Backdrop())
        .navigationTitle("Intercom")
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
        .refreshable { await load() }
    }

    private func load() async {
        do { info = try await HubClient.shared.get("/v1/intercom"); loadError = nil }
        catch { loadError = FamilyStore.message(error) }
    }

    private var roomsLine: String {
        guard let rs = info?.rooms else { return loadError.map { "Couldn't find the speakers. \($0)" } ?? "Finding speakers…" }
        if rs.isEmpty { return "No speakers found in Home Assistant" }
        if rooms.isEmpty { return "Plays on every speaker in the house" }
        return "Plays in " + rs.filter { rooms.contains($0.id) }.map(\.name).joined(separator: ", ")
    }

    private func announce(_ words: String) {
        let t = words.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return }
        sending = t
        Task {
            defer { sending = nil }
            do {
                let r = try await HubClient.shared.action("announce", params: ["text": t, "rooms": Array(rooms)])
                sent = t
                if t == text { text = "" }
                UINotificationFeedbackGenerator().notificationOccurred(.success)
                store.show(r.dryRun == true ? "Practice mode" : "Announced", t)
            } catch { store.show(error.localizedDescription, isError: true) }
        }
    }
}
