import SwiftUI

/// One list for everything that needs someone: Act now, Needs a decision, Maintenance, FYI.
/// Adults approve children's requests here; children see the answers to theirs.
struct InboxView: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    @Environment(\.dismiss) private var dismiss

    @State private var tab = 0

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                Picker("Show", selection: $tab) {
                    Text(store.role == .child ? "Messages" : "Inbox").tag(0)
                    Text("History").tag(1)
                }
                .pickerStyle(.segmented).padding(.horizontal).padding(.vertical, 8)
                if tab == 0 { inbox } else { AlertHistoryList(close: { dismiss() }) }
            }
            .background(Color(uiColor: .systemGroupedBackground))
            .navigationTitle(tab == 1 ? "History" : (store.role == .child ? "My messages" : "Inbox"))
            .toolbar { Button("Done") { dismiss() } }
        }
    }

    private var inbox: some View {
            List {
                if store.inbox.isEmpty {
                    if CatDirector.shared.isOn {
                        // Kiki asleep on the empty list.
                        ContentUnavailableView {
                            VStack(spacing: 6) {
                                LivingCat(kind: .kiki, pose: .sleep, scale: 1.3)
                                Text(store.role == .child ? "Nothing new" : "Nothing to report")
                            }
                        } description: {
                            Text(store.role == .child ? "Answers to what you ask for show up here." : "Problems, approvals and house news show up here.")
                        }
                    } else {
                        ContentUnavailableView(store.role == .child ? "Nothing new" : "All clear", systemImage: "bell.slash",
                                               description: Text(store.role == .child ? "Answers to what you ask for show up here." : "Problems, approvals and house news show up here."))
                    }
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
            .refreshable { await store.loadInbox() }
            .task { await store.loadInbox() }
    }
}

// MARK: - History: every alert, pushed or not

struct AlertHistoryItem: Decodable, Identifiable {
    var id: String
    var ts: Double
    var kind: String
    var title: String
    var body: String?
    var ref: String?
    var image: String?
    var date: Date { Date(timeIntervalSince1970: ts) }
}

/// Everything that could have alerted this person in the last 30 days, sorted by kind,
/// whether or not they turned on push for it.
struct AlertHistoryList: View {
    @Environment(Router.self) private var router
    let close: () -> Void
    @State private var items: [AlertHistoryItem] = []
    @State private var kinds: [Kind] = []
    @State private var kind: String?
    @State private var done = false
    @State private var loading = false
    @State private var failed: String?

    struct Kind: Decodable, Identifiable { var id: String; var title: String }
    private struct Reply: Decodable { var items: [AlertHistoryItem]; var kinds: [Kind] }

    var body: some View {
        VStack(spacing: 0) {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 6) {
                    chip(nil, "All")
                    ForEach(kinds) { chip($0.id, $0.title) }
                }
                .padding(.horizontal).padding(.bottom, 8)
            }
            List {
                if items.isEmpty && !loading {
                    ContentUnavailableView(failed == nil ? "Nothing yet" : "Can't load history", systemImage: "clock",
                                           description: Text(failed ?? "Camera, doorbell, house, network and media alerts land here, even ones you don't get pushed."))
                }
                ForEach(sections, id: \.0) { day, rows in
                    Section(day) {
                        ForEach(rows) { item in
                            row(item).onAppear { if item.id == items.last?.id { Task { await more() } } }
                        }
                    }
                }
            }
            .refreshable { await reload() }
        }
        .task(id: kind) { await reload() }
    }

    private var sections: [(String, [AlertHistoryItem])] {
        var out: [(String, [AlertHistoryItem])] = []
        for i in items {
            let d = Calendar.current.isDateInToday(i.date) ? "Today" : Calendar.current.isDateInYesterday(i.date) ? "Yesterday"
                : i.date.formatted(.dateTime.weekday(.wide).month().day())
            if out.last?.0 == d { out[out.count - 1].1.append(i) } else { out.append((d, [i])) }
        }
        return out
    }

    private func chip(_ id: String?, _ title: String) -> some View {
        let on = kind == id
        return Button { kind = id } label: {
            Text(title).font(.footnote.weight(.semibold))
                .padding(.horizontal, 12).padding(.vertical, 6)
                .background(on ? Theme.accent : Theme.card2, in: Capsule())
                .foregroundStyle(on ? Theme.onAccent : Theme.dim)
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(on ? .isSelected : [])
    }

    private func row(_ item: AlertHistoryItem) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: Self.symbol(item.kind)).foregroundStyle(Theme.accent).frame(width: 24)
            VStack(alignment: .leading, spacing: 3) {
                Text(item.title).font(.subheadline.weight(.semibold))
                if let b = item.body, !b.isEmpty { Text(b).font(.subheadline).foregroundStyle(Theme.dim).lineLimit(2) }
                Text(item.date, format: .dateTime.hour().minute()).font(.caption).foregroundStyle(Theme.dim)
            }
            Spacer(minLength: 0)
            if let img = item.image {
                HubImage(path: img) { Theme.card2 }
                    .frame(width: 72, height: 48).clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            }
        }
        .contentShape(Rectangle())
        .onTapGesture { if let ref = item.ref, let url = URL(string: ref) { close(); router.open(url) } }
    }

    static func symbol(_ kind: String) -> String {
        switch kind {
        case "camera": "video.fill"; case "package": "shippingbox.fill"; case "doorbell": "bell.and.waves.left.and.right.fill"
        case "doors": "door.garage.open"; case "house": "house.fill"; case "kids": "figure.and.child.holdinghands"
        case "network": "network"; case "media": "play.tv.fill"
        default: "bell.fill"
        }
    }

    private func fetch(before: Double?) async throws -> Reply {
        var q: [String: String] = ["limit": "50"]
        if let kind { q["kind"] = kind }
        if let before { q["before"] = String(before) }
        let data = try await HubClient.shared.raw("GET", "/v1/alerts/history", query: q)
        return try JSONDecoder().decode(Reply.self, from: data)
    }

    private func reload() async {
        loading = true; defer { loading = false }
        do {
            let r = try await fetch(before: nil)
            items = r.items; kinds = r.kinds; done = r.items.count < 50; failed = nil
        } catch { failed = error.localizedDescription }
    }

    private func more() async {
        guard !done, !loading, let last = items.last else { return }
        loading = true; defer { loading = false }
        if let r = try? await fetch(before: last.ts) {
            items += r.items; done = r.items.count < 50
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
