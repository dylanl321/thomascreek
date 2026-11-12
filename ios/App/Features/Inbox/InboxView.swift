import SwiftUI

/// One list for everything that needs someone: Act now, Needs a decision, Maintenance, FYI.
/// Adults approve children's requests here; children see the answers to theirs.
struct InboxView: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List {
                if store.inbox.isEmpty {
                    ContentUnavailableView(store.role == .child ? "Nothing new" : "All clear", systemImage: "bell.slash",
                                           description: Text(store.role == .child ? "Answers to what you ask for show up here." : "Problems, approvals and house news show up here."))
                }
                ForEach(InboxLevel.allCases, id: \.self) { level in
                    let items = store.inbox.filter { $0.bucket == level }
                    if !items.isEmpty {
                        Section(level.title) {
                            ForEach(items) { InboxRow(item: $0, close: { dismiss() }) }
                        }
                    }
                }
            }
            .navigationTitle(store.role == .child ? "My messages" : "Inbox")
            .toolbar { Button("Done") { dismiss() } }
            .refreshable { await store.loadInbox() }
            .task { await store.loadInbox() }
        }
    }
}

struct InboxRow: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    let item: InboxItem
    let close: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: symbol).foregroundStyle(Theme.level(item.level)).frame(width: 24)
                VStack(alignment: .leading, spacing: 3) {
                    HStack {
                        Text(item.title).font(.subheadline.weight(.semibold))
                        Spacer()
                        Text(item.date, format: .relative(presentation: .numeric)).font(.caption).foregroundStyle(Theme.dim)
                    }
                    Text(item.body).font(.subheadline).foregroundStyle(Theme.dim)
                }
            }
            if let actions = item.actions, !actions.isEmpty, item.acked != true {
                HStack(spacing: 8) {
                    Spacer().frame(width: 24)
                    ForEach(actions) { a in
                        Button(a.title) { act(a) }
                            .buttonStyle(.borderedProminent)
                            .tint(a.destructive == true ? Theme.dim : Theme.accent)
                            .controlSize(.small)
                    }
                }
            }
        }
        .padding(.vertical, 2)
        .contentShape(Rectangle())
        .onTapGesture {
            if let ref = item.ref, let url = URL(string: ref) { close(); router.open(url) }
        }
        .swipeActions {
            Button("Done") { Task { try? await HubClient.shared.send("/v1/inbox/\(item.id)/ack"); await store.loadInbox() } }.tint(Theme.accent)
        }
    }

    private var symbol: String {
        switch item.cat {
        case "show": "sparkles"; case "health": "exclamationmark.triangle.fill"; case "control": "hand.tap.fill"
        case "scene": "film"; case "renders": "wand.and.stars"; case "manager": "cpu"; case "weather": "cloud.rain.fill"
        case "summary": "chart.bar.fill"; case "approval", "request": "hand.raised.fill"; case "garage", "doors": "door.garage.closed"
        case "camera": "video.fill"; case "device": "iphone"; case "storage", "apps": "server.rack"; case "maintenance": "wrench.and.screwdriver.fill"
        default: "bell.fill"
        }
    }

    private func act(_ a: InboxItem.InboxAction) {
        // Choosing whose new device it is needs a person picker.
        if a.id == "assign" { close(); router.show(.systems, .people); return }
        Task {
            do {
                try await HubClient.shared.send("/v1/inbox/\(item.id)/act", ["action": a.id],
                                                confirm: "\(a.title): \(item.title)")
                store.show(a.title)
                await store.loadInbox()
            } catch { store.show(error.localizedDescription, isError: true) }
        }
    }
}
