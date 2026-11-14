import SwiftUI

/// The eight idle scenes. The date already picks one. Pinning one, or naming a channel, is optional.
struct AmbientLibrary: Decodable {
    var pin: String?
    var current: String
    var items: [AmbientItem]
}
struct AmbientItem: Decodable, Identifiable {
    var id: String
    var title: String
    var channelKey: String?
    var matched: Bool?
}

struct AmbientSettings: View {
    @State private var items: [AmbientItem] = []
    @State private var pin: String?
    @State private var current = ""
    @State private var keys: [String: String] = [:]
    @State private var note: String?
    @State private var loaded = false

    var body: some View {
        Form {
            Section {
                if items.isEmpty && loaded {
                    Text("The usual scenes will keep playing.").foregroundStyle(Theme.dim)
                }
                ForEach(items) { item in
                    VStack(alignment: .leading, spacing: 6) {
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(item.title).font(.body.weight(.semibold))
                                Text(item.id == current ? "Playing today" : (pin == item.id ? "Pinned" : " "))
                                    .font(.caption).foregroundStyle(Theme.dim)
                            }
                            Spacer()
                            Button(pin == item.id ? "Unpin" : "Pin") {
                                UIImpactFeedbackGenerator(style: .soft).impactOccurred()
                                pin = pin == item.id ? nil : item.id
                            }
                            .font(.subheadline.weight(.semibold))
                        }
                        TextField("Channel", text: Binding(get: { keys[item.id] ?? "" }, set: { keys[item.id] = $0 }))
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                    }
                    .padding(.vertical, 4)
                }
            } footer: {
                Text("Leave the channel blank and a lineup channel with the same name is used. The date picks the scene until you pin one.")
            }
            if let note { Section { Text(note).foregroundStyle(Theme.dim) } }
        }
        .navigationTitle("Ambient")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Save") { Task { await save() } }.disabled(!loaded) } }
        .task { await load() }
    }

    private func load() async {
        guard let page: AmbientLibrary = try? await HubClient.shared.get("/v1/ambient/library") else {
            note = "The TV will keep today's scene."
            loaded = true
            return
        }
        items = page.items
        pin = page.pin
        current = page.current
        keys = Dictionary(uniqueKeysWithValues: page.items.map { ($0.id, $0.matched == true ? "" : ($0.channelKey ?? "")) })
        loaded = true
    }

    private func save() async {
        var body: [String: Any] = ["pin": pin as Any? ?? NSNull()]
        body["items"] = items.map { item in
            ["id": item.id, "channel_key": keys[item.id, default: ""]]
        }
        do {
            let _: AmbientLibrary = try await HubClient.shared.call("PUT", "/v1/ambient", body: body)
            UINotificationFeedbackGenerator().notificationOccurred(.success)
            note = "Saved."
        } catch {
            note = "The TV will keep today's scene."
        }
    }
}
