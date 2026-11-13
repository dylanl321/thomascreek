import SwiftUI
import Observation

/// This person's settings and favorites, kept on the hub so they follow them to every device.
///
/// Screens read from here and change things through `star`, `set` and `reorder`. A change shows at
/// once, goes to the hub, and the hub tells the person's other devices (a `settings` event), which
/// re-read. Made while home can't be reached, it waits in the outbox. Widgets and Control Center read
/// the App Group copy.
@MainActor
@Observable
final class SettingsStore {
    static let shared = SettingsStore()

    private(set) var current: PersonSettings = PersonSettings.cached ?? PersonSettings()
    private var loading = false

    // MARK: reading

    func favorites(_ kind: FavoriteKind) -> [FavoriteRef] { current.favorites(kind) }
    func isStarred(_ kind: FavoriteKind, _ id: String) -> Bool { current.isStarred(kind, id) }
    var anyFavorites: Bool { FavoriteKind.allCases.contains { !current.favorites($0).isEmpty } }
    func anyFavorites(_ kind: FavoriteKind) -> Bool { !current.favorites(kind).isEmpty }

    func bool(_ key: String, _ fallback: Bool = false) -> Bool { current.bool(key, fallback) }
    func string(_ key: String, _ fallback: String) -> String { current.string(key) ?? fallback }
    func optionalString(_ key: String) -> String? { current.string(key) }

    /// Home sections in this person's order, without the ones they hid. Sections the hub doesn't
    /// know yet keep their default place.
    func homeSections(default defaults: [HomeSection]) -> [HomeSection] {
        let order = current.strings(SettingKey.homeOrder).compactMap(HomeSection.init(rawValue:))
        let hidden = Set(current.strings(SettingKey.homeHidden))
        var out = order.filter { defaults.contains($0) }
        for s in defaults where !out.contains(s) {
            // A section added in a later version slots in after the one it follows by default.
            if let i = defaults.firstIndex(of: s), i > 0, let after = out.firstIndex(of: defaults[i - 1]) {
                out.insert(s, at: after + 1)
            } else if defaults.first == s {
                out.insert(s, at: 0)  // a new first section (Who's home) goes on top, not at the bottom
            } else { out.append(s) }
        }
        return out.filter { !hidden.contains($0.rawValue) }
    }

    func isHidden(_ s: HomeSection) -> Bool { current.strings(SettingKey.homeHidden).contains(s.rawValue) }

    // MARK: loading

    /// Read from the hub (on launch, after a `settings` event, when /v1/me says we're behind).
    func load() async {
        guard !loading, HomeStore.shared.me?.role != .pending else { return }
        loading = true; defer { loading = false }
        do {
            let data = try await HubClient.shared.raw("GET", "/v1/me/settings")
            let s = try PersonSettings.decoder.decode(PersonSettings.self, from: data)
            apply(s)
            await migrateOnce()
        } catch {
            // Offline: keep showing the last copy.
        }
    }

    /// Called by HomeStore with the `settings` event or /v1/me's version.
    func hubVersion(_ version: Int, from device: String? = nil) {
        if device != nil && device == AppSettings.deviceID { return }  // our own change coming back
        if version != current.version { Task { await load() } }
    }

    private func apply(_ s: PersonSettings) {
        current = s
        guard HubClient.viewAs == nil else { return }  // a preview never touches this phone's own copy
        PersonSettings.cached = s
        // Code that reads these locally (downloads, Live Activity) sees the person's choice.
        if let q = s.string(SettingKey.downloadQuality) { AppSettings.downloadQuality = q }
        AppSettings.downloadOnCellular = s.bool(SettingKey.downloadCellular, false)
        AppSettings.removeWatchedDownloads = s.bool(SettingKey.removeWatched, true)
        AppSettings.autoLiveActivity = s.bool(SettingKey.liveActivityAuto, true)
        AppSettings.refreshSurfaces()
    }

    /// Before settings synced, these lived only on this phone. Send them once; the hub keeps whatever
    /// another device already sent.
    private func migrateOnce() async {
        guard HubClient.viewAs == nil, !AppSettings.defaults.bool(forKey: "settingsMigrated") else { return }
        let local: [String: Any] = [
            SettingKey.downloadQuality: AppSettings.downloadQuality,
            SettingKey.downloadCellular: AppSettings.downloadOnCellular,
            SettingKey.removeWatched: AppSettings.removeWatchedDownloads,
            SettingKey.liveActivityAuto: AppSettings.autoLiveActivity,
        ]
        guard let body = try? JSONSerialization.data(withJSONObject: ["local": local]),
              let data = try? await HubClient.shared.raw("POST", "/v1/me/settings/migrate", body: body),
              let s = try? PersonSettings.decoder.decode(PersonSettings.self, from: data) else { return }
        AppSettings.defaults.set(true, forKey: "settingsMigrated")
        apply(s)
    }

    // MARK: changing

    /// Star or un-star. `label` is what to show for it elsewhere (a show's title and poster).
    func star(_ kind: FavoriteKind, _ id: String, _ on: Bool, label: FavoriteRef? = nil, name: String? = nil) {
        var s = current
        var list = s.favorites(kind)
        if on {
            guard !list.contains(where: { $0.id == id }) else { return }
            list.append(label.map { FavoriteRef(id: id, title: $0.title, subtitle: $0.subtitle, image: $0.image) } ?? FavoriteRef(id: id))
        } else {
            list.removeAll { $0.id == id }
        }
        s.favorites[kind.rawValue] = list
        apply(s)
        let path = "/v1/me/favorites/\(kind.rawValue)/\(Self.escape(id))"
        var body: [String: Any]?
        if on, let label {
            body = [:]
            if let t = label.title { body?["title"] = t }
            if let t = label.subtitle { body?["subtitle"] = t }
            if let t = label.image { body?["image"] = t }
        }
        send(on ? "PUT" : "DELETE", path, body, label: name.map { on ? "Star \($0)" : "Unstar \($0)" },
             done: name.map { on ? "Added \($0) to Favorites" : "Removed \($0) from Favorites" })
    }

    func toggle(_ kind: FavoriteKind, _ id: String, label: FavoriteRef? = nil, name: String? = nil) {
        star(kind, id, !isStarred(kind, id), label: label, name: name)
    }

    func reorder(_ kind: FavoriteKind, _ ids: [String]) {
        var s = current
        let by = Dictionary(s.favorites(kind).map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a })
        s.favorites[kind.rawValue] = ids.compactMap { by[$0] }
        apply(s)
        send("POST", "/v1/me/favorites/\(kind.rawValue)/order", ["ids": ids], label: nil, done: nil)
    }

    /// Change one setting (nil puts it back to the default).
    func set(_ key: String, _ value: Any?) {
        var s = current
        s.settings[key] = Self.json(value)
        apply(s)
        send("PATCH", "/v1/me/settings", ["changes": [key: value ?? NSNull()]], label: nil, done: nil)
    }

    func setHomeSections(order: [HomeSection], hidden: Set<HomeSection>) {
        var s = current
        s.settings[SettingKey.homeOrder] = .array(order.map { .string($0.rawValue) })
        s.settings[SettingKey.homeHidden] = .array(order.filter { hidden.contains($0) }.map { .string($0.rawValue) })
        apply(s)
        send("PATCH", "/v1/me/settings", ["changes": [SettingKey.homeOrder: order.map(\.rawValue),
                                                      SettingKey.homeHidden: order.filter { hidden.contains($0) }.map(\.rawValue)]],
             label: nil, done: nil)
    }

    private func send(_ method: String, _ path: String, _ body: [String: Any]?, label: String?, done: String?) {
        Task {
            do {
                let data = try await HubClient.shared.raw(method, path, body: body.flatMap { try? JSONSerialization.data(withJSONObject: $0) })
                if let s = try? PersonSettings.decoder.decode(PersonSettings.self, from: data) { apply(s) }
                if let done { HomeStore.shared.show(done) }
            } catch HubClient.HubError.unreachable {
                guard HubClient.viewAs == nil else { HomeStore.shared.show("Can't reach home", isError: true); return }
                // Merge settings changes so one PATCH carries the latest of each key.
                Outbox.shared.enqueueSetting(method, path, body, label: label)
                HomeStore.shared.markUnreachable()
                if let done { HomeStore.shared.show(done, "Saved here. It reaches your other devices when home is back.") }
            } catch {
                HomeStore.shared.show(error.localizedDescription, isError: true)
                await load()  // put back what the hub really has
            }
        }
    }

    /// Someone else's settings: a parent setting up a child's favorites and Home.
    func forPerson(_ id: String) async throws -> PersonSettings {
        let data = try await HubClient.shared.raw("GET", "/v1/people/\(id)/settings")
        return try PersonSettings.decoder.decode(PersonSettings.self, from: data)
    }

    /// The previous person's settings must not show for the next one (role change, preview end).
    func reset() {
        current = PersonSettings.cached ?? PersonSettings()
    }

    static func escape(_ id: String) -> String {
        id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed.subtracting(CharacterSet(charactersIn: "?#"))) ?? id
    }

    private static func json(_ v: Any?) -> JSONValue {
        switch v {
        case nil: return .null
        case let b as Bool: return .bool(b)
        case let n as Int: return .number(Double(n))
        case let n as Double: return .number(n)
        case let s as String: return .string(s)
        case let a as [String]: return .array(a.map { .string($0) })
        default: return .null
        }
    }
}

/// The sections a grown-up's Home can show, in their default order. Each person can reorder and hide them.
enum HomeSection: String, CaseIterable, Identifiable {
    case whosHome = "whos_home", rightNow = "rightnow", weather, status, favorites, modes, kids, playing, printer, cameras, holiday, trip, today, systems

    var id: String { rawValue }
    var title: String {
        switch self {
        case .whosHome: "Who's home"
        case .rightNow: "Right now"
        case .weather: "Weather"
        case .printer: "3D printer"
        case .status: "House status"
        case .favorites: "Favorites"
        case .modes: "Modes"
        case .kids: "Kids online"
        case .playing: "Now playing"
        case .cameras: "Recent at the cameras"
        case .holiday: "Tonight's show"
        case .trip: "Next trip"
        case .today: "Today"
        case .systems: "Systems"
        }
    }
    var symbol: String {
        switch self {
        case .whosHome: "person.2.fill"
        case .rightNow: "sparkles"
        case .weather: "cloud.sun.fill"
        case .printer: "printer.fill"
        case .status: "checkmark.shield.fill"
        case .favorites: "star.fill"
        case .modes: "wand.and.stars"
        case .kids: "wifi"
        case .playing: "play.circle.fill"
        case .cameras: "video.fill"
        case .holiday: "sparkles.tv.fill"
        case .trip: "suitcase.rolling.fill"
        case .today: "calendar"
        case .systems: "server.rack"
        }
    }
}
