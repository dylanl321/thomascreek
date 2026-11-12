import SwiftUI

struct FamilyView: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    @State private var chores: [ChoreBoard]?
    private struct Boards: Decodable { var boards: [ChoreBoard] }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                if let today = store.home?.today {
                    SectionHeader(title: "Today") { MoreButton("Calendar") { router.go(.calendar) } }
                    TodayCard(today: today)
                }
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), spacing: 10)], spacing: 10) {
                    SectionTile(symbol: "calendar", title: "Calendar", detail: "This week") { router.go(.calendar) }
                    SectionTile(symbol: "fork.knife", title: "Dinners", detail: store.home?.today?.dinner?.title ?? "Plan the week") { router.go(.meals) }
                    SectionTile(symbol: "book.fill", title: "Recipes", detail: "Add, edit, find") { router.go(.recipes) }
                    SectionTile(symbol: "cart.fill", title: "Shopping", detail: "Lists") { router.go(.shopping) }
                    SectionTile(symbol: "star.fill", title: "Chores", detail: chores.map { b in "\(b.reduce(0) { $0 + $1.doneCount })/\(b.reduce(0) { $0 + $1.items.count }) done today" } ?? "Points and rewards") { router.go(.chores) }
                    SectionTile(symbol: "shippingbox.fill", title: "Inventory", detail: "Where is it?") { router.go(.inventory) }
                    if store.me?.can(.tripsView) == true {
                        SectionTile(symbol: "suitcase.rolling.fill", title: "Trips", detail: tripDetail) { router.go(.trips) }
                    }
                }
                .padding(.top, 8)
            }
            .screen()
        }
        .background(Backdrop())
        .navigationTitle("Family")
        .topBar()
        .task { chores = (try? await HubClient.shared.get("/v1/chores/all", as: Boards.self))?.boards }
        .task { if store.me?.can(.tripsView) == true { await TripStore.shared.load() } }
    }

    private var tripDetail: String {
        guard let t = TripStore.shared.list?.next else { return "Plan in TREK" }
        return t.isOn ? "On the trip now" : "\(t.title) · \(t.countdown)"
    }
}

struct CalendarView: View {
    @State private var events: [CalendarEvent]?
    private struct Reply: Decodable { var events: [CalendarEvent] }

    var body: some View {
        List {
            if let events {
                let days = Dictionary(grouping: events) { e in ISO.parse(e.start).map { Calendar.current.startOfDay(for: $0) } ?? .distantPast }
                ForEach(days.keys.sorted(), id: \.self) { day in
                    Section(day.formatted(.dateTime.weekday(.wide).month().day())) {
                        ForEach(days[day] ?? [], id: \.stableID) { e in
                            HStack(alignment: .top, spacing: 12) {
                                Text(e.isAllDay ? "All day" : clock(e.start)).font(.subheadline.monospacedDigit()).foregroundStyle(Theme.dim).frame(width: 72, alignment: .leading)
                                VStack(alignment: .leading) {
                                    Text(e.title).font(.subheadline.weight(.medium))
                                    if let w = e.who { Text(w).font(.caption).foregroundStyle(Theme.dim) }
                                }
                            }
                        }
                    }
                }
                if events.isEmpty { ContentUnavailableView("A quiet week", systemImage: "calendar") }
            } else { ProgressView() }
        }
        .scrollContentBackground(.hidden).background(Backdrop())
        .navigationTitle("Calendar")
        .task {
            let r: Reply? = try? await HubClient.shared.get("/v1/calendar", ["days": "7"])
            events = r?.events ?? []
        }
    }
}

struct ShoppingView: View {
    @Environment(HomeStore.self) private var store
    @State private var data: ShoppingLists?
    @State private var newItem = ""
    @State private var listID: String?

    var body: some View {
        List {
            if let data {
                if data.lists.count > 1 {
                    Picker("List", selection: Binding(get: { listID ?? data.lists.first?.id ?? "" }, set: { listID = $0 })) {
                        ForEach(data.lists) { Text($0.name).tag($0.id) }
                    }
                    .pickerStyle(.segmented)
                }
                let list = data.lists.first { $0.id == (listID ?? data.lists.first?.id) }
                Section {
                    HStack {
                        TextField("Add an item", text: $newItem).onSubmit(add)
                        Button("Add", action: add).disabled(newItem.trimmingCharacters(in: .whitespaces).isEmpty)
                    }
                    ForEach(list?.items ?? []) { item in
                        Button { toggle(item, in: list) } label: {
                            HStack {
                                Image(systemName: item.done ? "checkmark.circle.fill" : "circle").foregroundStyle(item.done ? Theme.accent : Theme.dim)
                                Text(item.text).strikethrough(item.done).foregroundStyle(item.done ? Theme.dim : Theme.fg)
                            }
                        }
                        .disabled(store.me?.can(.familyEdit) != true)
                    }
                }
            } else { ProgressView() }
        }
        .scrollContentBackground(.hidden).background(Backdrop())
        .navigationTitle("Shopping")
        .task { await load() }
        .refreshable { await load() }
    }

    private func load() async { data = try? await HubClient.shared.get("/v1/shopping") }

    private func add() {
        let t = newItem.trimmingCharacters(in: .whitespaces)
        guard !t.isEmpty, let id = listID ?? data?.lists.first?.id else { return }
        newItem = ""
        Task {
            do {
                if try await Outbox.shared.send("/v1/shopping/\(id)/items", ["text": t], label: "\(t) (shopping)") { await load() }
                else {
                    // Offline: show it on the list straight away; it's sent when home is back.
                    if let i = data?.lists.firstIndex(where: { $0.id == id }) {
                        data?.lists[i].items.append(.init(id: "pending-\(UUID().uuidString)", text: t, done: false))
                    }
                    store.show("Added", "It's sent when home is back")
                }
            } catch { store.show(error.localizedDescription, isError: true) }
        }
    }

    private func toggle(_ item: ShoppingLists.Item, in list: ShoppingLists.ShoppingList?) {
        guard let list else { return }
        Task {
            _ = try? await HubClient.shared.call("PATCH", "/v1/shopping/\(list.id)/items/\(item.id)", body: ["done": !item.done], as: Empty.self)
            await load()
        }
    }
}

/// Adults: everyone's chores and pending reward approvals.
struct ChoresManageView: View {
    @Environment(HomeStore.self) private var store
    @State private var boards: [ChoreBoard]?
    private struct Reply: Decodable { var boards: [ChoreBoard] }

    var body: some View {
        List {
            if let boards {
                ForEach(boards, id: \.person) { b in
                    Section {
                        ForEach(b.items) { c in
                            HStack {
                                Image(systemName: c.waiting ? "hourglass.circle.fill" : c.done ? "checkmark.circle.fill" : "circle")
                                    .foregroundStyle(c.waiting ? Theme.warn : c.done ? Theme.accent : Theme.dim)
                                Text("\(c.icon ?? "") \(c.title)")
                                Spacer()
                                if c.waiting { Text("Check").font(.caption.weight(.semibold)).foregroundStyle(Theme.warn) }
                                if let p = c.points { Text("+\(p)").font(.caption.weight(.semibold)).foregroundStyle(Theme.star) }
                            }
                        }
                    } header: {
                        HStack { Text(b.person ?? ""); Spacer(); Label("\(b.points)", systemImage: "star.fill").foregroundStyle(Theme.star) }
                    }
                }
                Section {
                    Button { router.showInbox = true } label: { Label("Reward requests are in your inbox", systemImage: "bell") }
                }
            } else { ProgressView() }
        }
        .scrollContentBackground(.hidden).background(Backdrop())
        .navigationTitle("Chores")
        .task { boards = (try? await HubClient.shared.get("/v1/chores/all", as: Reply.self))?.boards ?? [] }
    }

    @Environment(Router.self) private var router
}

struct InventoryView: View {
    @State private var query = ""
    @State private var items: [InventoryItem] = []
    var body: some View {
        List {
            ForEach(items) { i in
                VStack(alignment: .leading, spacing: 3) {
                    Text(i.name).font(.body.weight(.medium))
                    if let l = i.location { Label(l, systemImage: "mappin.and.ellipse").font(.caption).foregroundStyle(Theme.accent) }
                    if let d = i.detail { Text(d).font(.caption).foregroundStyle(Theme.dim) }
                    if let s = i.lastService { Text("Last serviced \(s)").font(.caption2).foregroundStyle(Theme.dim) }
                }
            }
            if items.isEmpty && !query.isEmpty { ContentUnavailableView.search(text: query) }
        }
        .scrollContentBackground(.hidden).background(Backdrop())
        .navigationTitle("Inventory")
        .searchable(text: $query, prompt: "Where is the…")
        .task(id: query) {
            guard !query.isEmpty else { items = []; return }
            try? await Task.sleep(for: .milliseconds(300))
            if let l: InventoryList = try? await HubClient.shared.get("/v1/inventory/search", ["q": query]) { items = l.items }
        }
    }
}
