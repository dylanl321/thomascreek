import SwiftUI

/// Grown-ups' tools for the kids, in Family where Lauren can reach them (they used to sit in Dylan's
/// Systems tab): what the kids added on YouTube, internet and device time, and each kid's favorites.
struct ParentsSection: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router

    var body: some View {
        SectionHeader("Parents")
        RowsCard {
            NavigationLink(value: Destination.youtube) {
                InfoRow("play.rectangle.fill", "Kids' YouTube", "What the kids added, remove or block", chevron: true)
            }
            if store.me?.can(.networkManage) == true {
                NavigationLink(value: Destination.kidNet) {
                    InfoRow("wifi", "Internet and device time", "Pause, bedtime, extra time", chevron: true)
                }
            }
            ForEach(kids) { k in
                NavigationLink { KidFavoritesView(kid: k) } label: {
                    InfoRow("star.fill", color: Color(hexString: k.color) ?? Theme.star, "\(k.name)'s favorites", "What shows first on their Home", chevron: true)
                }
            }
            if store.role == .admin {
                Button { router.showViewAs = true } label: {
                    InfoRow("eye.fill", "View as…", "See the app the way a kid or guest does", chevron: true)
                }
            }
        }
        .buttonStyle(.plain)
    }

    private var kids: [Person] { (store.home?.people ?? []).filter { $0.role == .child } }
}

/// A parent sets up one kid's favorites from their own phone (their own room, their TV, their shows).
/// Changes reach the kid's iPad at once. The hub only accepts things the kid is allowed to use.
struct KidFavoritesView: View {
    @Environment(HomeStore.self) private var store
    let kid: Person
    @State private var settings: PersonSettings?
    @State private var error: String?

    private let kinds: [FavoriteKind] = [.room, .device, .media, .youtube]

    var body: some View {
        List {
            if let settings {
                ForEach(kinds, id: \.self) { kind in
                    let list = settings.favorites(kind)
                    Section(header: Text(title(kind))) {
                        if list.isEmpty {
                            Text("Nothing yet").foregroundStyle(Theme.dim)
                        }
                        ForEach(list) { f in Text(name(kind, f)) }
                            .onDelete { idx in for i in idx { remove(kind, list[i].id) } }
                        if kind == .room || kind == .device {
                            Menu {
                                ForEach(options(kind).filter { o in !list.contains { $0.id == o.id } }) { o in
                                    Button(o.name) { add(kind, o.id) }
                                }
                            } label: { Label("Add", systemImage: "plus") }
                        }
                    }
                }
                Section {
                    Text("Shows and YouTube channels are easiest to add with View as: open the app as \(kid.name) and tap the star on a show.")
                        .font(.footnote).foregroundStyle(Theme.dim)
                }
            } else if let error {
                Text(error).foregroundStyle(Theme.dim)
            } else {
                ProgressView()
            }
        }
        .navigationTitle("\(kid.name)'s favorites")
        .toolbar { EditButton() }
        .task { await load() }
    }

    private func title(_ k: FavoriteKind) -> String {
        switch k {
        case .room: "Rooms"
        case .device: "Lights and TVs"
        case .media: "Shows and movies"
        case .youtube: "YouTube channels"
        default: "Favorites"
        }
    }

    private func name(_ k: FavoriteKind, _ f: FavoriteRef) -> String {
        switch k {
        case .room: store.home?.room(f.id)?.name ?? f.title ?? f.id
        case .device: store.home?.devices?.first { $0.id == f.id }?.name ?? f.title ?? f.id
        default: f.title ?? f.id
        }
    }

    /// What can be offered; the hub still refuses anything the kid can't use (a door lock, the garage).
    private struct Option: Identifiable { var id: String; var name: String }
    private func options(_ k: FavoriteKind) -> [Option] {
        let h = store.home
        switch k {
        case .room: return (h?.rooms ?? []).map { Option(id: $0.id, name: $0.name) }
        case .device: return (h?.devices ?? []).filter { [.light, .fan, .media, .speaker].contains($0.kind) }.map { Option(id: $0.id, name: $0.name) }
        default: return []
        }
    }

    private func load() async {
        do { settings = try await SettingsStore.shared.forPerson(kid.id) }
        catch { self.error = "Can't load \(kid.name)'s favorites right now." }
    }

    private func add(_ k: FavoriteKind, _ id: String) { change("PUT", k, id) }
    private func remove(_ k: FavoriteKind, _ id: String) { change("DELETE", k, id) }

    private func change(_ method: String, _ k: FavoriteKind, _ id: String) {
        Task {
            do {
                let data = try await HubClient.shared.raw(method, "/v1/people/\(kid.id)/favorites/\(k.rawValue)/\(SettingsStore.escape(id))")
                settings = try PersonSettings.decoder.decode(PersonSettings.self, from: data)
            } catch HubClient.HubError.server(_, let m) {
                store.show(m, isError: true)
            } catch {
                store.show(error.localizedDescription, isError: true)
            }
        }
    }
}
