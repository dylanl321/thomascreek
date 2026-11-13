import SwiftUI

// MARK: - Starring

/// A star for a screen's toolbar or a row: tap to add to (or remove from) this person's Favorites.
struct StarButton: View {
    let kind: FavoriteKind
    let id: String
    let name: String
    var label: FavoriteRef? = nil
    var size: Font = .body

    var body: some View {
        let on = SettingsStore.shared.isStarred(kind, id)
        Button {
            SettingsStore.shared.toggle(kind, id, label: label, name: name)
        } label: {
            Image(systemName: on ? "star.fill" : "star")
                .font(size)
                .foregroundStyle(on ? Theme.star : Theme.dim)
                .contentTransition(.symbolEffect(.replace))
        }
        .buttonStyle(.plain)
        .sensoryFeedback(.selection, trigger: on)
        .accessibilityLabel(on ? "Remove \(name) from Favorites" : "Add \(name) to Favorites")
    }
}

/// "Add to Favorites" for a long-press menu on any tile.
struct FavoriteMenuItem: View {
    let kind: FavoriteKind
    let id: String
    let name: String
    var label: FavoriteRef? = nil
    var body: some View {
        let on = SettingsStore.shared.isStarred(kind, id)
        Button(on ? "Remove from Favorites" : "Add to Favorites", systemImage: on ? "star.slash" : "star") {
            SettingsStore.shared.toggle(kind, id, label: label, name: name)
        }
    }
}

extension View {
    /// Long-press a tile to star it. Tiles that already have a menu add `FavoriteMenuItem` to it instead.
    func favoriteMenu(_ kind: FavoriteKind, _ id: String, name: String, label: FavoriteRef? = nil) -> some View {
        contextMenu { FavoriteMenuItem(kind: kind, id: id, name: name, label: label) }
    }
}

// MARK: - Home

/// The person's own favorites on Home: rooms, lights, locks, the garage, TVs, cameras and modes, in the
/// order they arranged them. Device tiles work in place.
struct FavoritesSection: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router

    var body: some View {
        let items = entries
        SectionHeader(title: "Favorites", live: true) {
            MoreButton(items.isEmpty ? "Add" : "Edit") { router.showEditHome = true }
        }
        if items.isEmpty {
            Button { router.show(.house) } label: {
                HStack(spacing: 12) {
                    IconBadge(symbol: "star", color: Theme.star, size: 38)
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Star what you use most").font(.subheadline.weight(.semibold)).foregroundStyle(Theme.fg)
                        Text("Long-press a room, light, camera or mode, or tap its star. It shows up here on every device you use.")
                            .font(.caption).foregroundStyle(Theme.dim).multilineTextAlignment(.leading)
                    }
                    Spacer(minLength: 0)
                }
                .card()
            }
            .buttonStyle(.plain)
        } else {
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), spacing: 10)], spacing: 10) {
                ForEach(items) { e in tile(e) }
            }
        }
    }

    enum Entry: Identifiable {
        case room(Room), device(DeviceItem), camera(Camera), mode(Mode)
        var id: String {
            switch self {
            case .room(let r): "room:\(r.id)"
            case .device(let d): "device:\(d.id)"
            case .camera(let c): "camera:\(c.id)"
            case .mode(let m): "mode:\(m.id)"
            }
        }
    }

    /// Rooms first, then devices, cameras and modes, each in the person's own order; anything the house
    /// no longer has (or they can no longer use) is left out.
    private var entries: [Entry] {
        let s = SettingsStore.shared
        let h = store.home
        var out: [Entry] = store.favoriteRooms.map(Entry.room)
        out += s.favorites(.device).compactMap { f in h?.devices?.first { $0.id == f.id }.map(Entry.device) }
        if store.me?.can(.cameras) == true {
            out += s.favorites(.camera).compactMap { f in h?.cameras?.first { $0.id == f.id }.map(Entry.camera) }
        }
        out += s.favorites(.mode).compactMap { f in h?.modes?.first { $0.id == f.id }.map(Entry.mode) }
        return out
    }

    @ViewBuilder private func tile(_ e: Entry) -> some View {
        switch e {
        case .room(let r): RoomTile(room: r)
        case .device(let d): DeviceTile(device: d)
        case .camera(let c):
            Button { router.show(.house, .camera(c.id)) } label: { CameraThumb(camera: c).frame(height: 104) }
                .buttonStyle(.plain)
                .favoriteMenu(.camera, c.id, name: c.name)
        case .mode(let m):
            StateTile(symbol: m.symbol ?? "sparkles", title: m.name, detail: m.active == true ? "On" : "Tap to run",
                      on: m.active == true) { store.perform("mode.run", target: m.id, done: "\(m.name) is on") }
                .favoriteMenu(.mode, m.id, name: m.name)
        }
    }
}

// MARK: - Edit Home

/// Arrange Home: drag sections into order, switch off the ones you don't use, and arrange or remove
/// favorites. Saved to the person's profile, so every device they use matches.
struct EditHomeSheet: View {
    @Environment(HomeStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    @State private var order: [HomeSection] = []
    @State private var hidden: Set<HomeSection> = []
    @State private var favs: [FavoriteKind: [FavoriteRef]] = [:]

    private let kinds: [FavoriteKind] = [.room, .device, .camera, .mode]

    var body: some View {
        NavigationStack {
            List {
                Section {
                    ForEach(order) { s in
                        let shown = !hidden.contains(s)
                        HStack {
                            Label(s.title, systemImage: s.symbol).foregroundStyle(shown ? Theme.fg : Theme.dim)
                            Spacer()
                            Button {
                                if shown { hidden.insert(s) } else { hidden.remove(s) }
                            } label: {
                                Image(systemName: shown ? "eye.fill" : "eye.slash")
                                    .foregroundStyle(shown ? Theme.accent : Theme.dim)
                                    .frame(width: 44, height: 36)
                                    .contentShape(Rectangle())
                            }
                            .buttonStyle(.borderless)
                            .accessibilityLabel(shown ? "Hide \(s.title)" : "Show \(s.title)")
                        }
                    }
                    .onMove { order.move(fromOffsets: $0, toOffset: $1) }
                } header: { Text("Sections") } footer: {
                    Text("Drag to reorder, tap the eye to show or hide. Your other iPhone and iPad match within a second.")
                }
                ForEach(kinds, id: \.self) { kind in
                    let list = favs[kind] ?? []
                    if !list.isEmpty {
                        Section(header: Text(header(kind))) {
                            ForEach(list) { f in
                                Label(name(kind, f), systemImage: symbol(kind, f))
                            }
                            .onMove { favs[kind]?.move(fromOffsets: $0, toOffset: $1) }
                            .onDelete { idx in
                                for i in idx { SettingsStore.shared.star(kind, list[i].id, false) }
                                favs[kind]?.remove(atOffsets: idx)
                            }
                        }
                    }
                }
                if kinds.allSatisfy({ (favs[$0] ?? []).isEmpty }) {
                    Section {
                        Text("Nothing starred yet. Long-press a room, light, camera or mode anywhere in the app and choose Add to Favorites.")
                            .font(.subheadline).foregroundStyle(Theme.dim)
                    } header: { Text("Favorites") }
                }
            }
            .environment(\.editMode, .constant(.active))
            .navigationTitle("Edit Home")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) { Button("Done") { save(); dismiss() }.bold() }
            }
            .onAppear(perform: loadState)
        }
    }

    private var available: [HomeSection] { HomeView.sections(for: store.me) }

    private func loadState() {
        let s = SettingsStore.shared
        let shown = s.homeSections(default: available)
        order = shown + available.filter { !shown.contains($0) }
        hidden = Set(available.filter { s.isHidden($0) })
        for k in kinds { favs[k] = s.favorites(k) }
    }

    private func save() {
        SettingsStore.shared.setHomeSections(order: order, hidden: hidden)
        for k in kinds {
            let now = (favs[k] ?? []).map(\.id)
            if now != SettingsStore.shared.favorites(k).map(\.id) { SettingsStore.shared.reorder(k, now) }
        }
    }

    private func header(_ k: FavoriteKind) -> String {
        switch k {
        case .room: "Rooms"
        case .device: "Lights, doors and TVs"
        case .camera: "Cameras"
        case .mode: "Modes"
        default: "Favorites"
        }
    }

    private func name(_ k: FavoriteKind, _ f: FavoriteRef) -> String {
        let h = store.home
        switch k {
        case .room: return h?.room(f.id)?.name ?? f.title ?? f.id
        case .device: return h?.devices?.first { $0.id == f.id }?.name ?? f.title ?? f.id
        case .camera: return h?.cameras?.first { $0.id == f.id }?.name ?? f.title ?? f.id
        case .mode: return h?.modes?.first { $0.id == f.id }?.name ?? f.title ?? f.id
        default: return f.title ?? f.id
        }
    }

    private func symbol(_ k: FavoriteKind, _ f: FavoriteRef) -> String {
        switch k {
        case .room: return store.home?.room(f.id)?.symbol ?? "square.split.bottomrightquarter"
        case .device: return store.home?.devices?.first { $0.id == f.id }.map(DeviceSymbols.symbol) ?? "lightbulb"
        case .camera: return "video.fill"
        case .mode: return store.home?.modes?.first { $0.id == f.id }?.symbol ?? "sparkles"
        default: return "star"
        }
    }
}
