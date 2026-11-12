import SwiftUI
import Observation

enum AppTab: String, Hashable {
    case home, house, media, family, systems
    case myRoom, watch, chores          // children
    case space                          // guests
}

/// Every screen a deep link, notification or card can open.
enum Destination: Hashable {
    case room(String)
    case climate, doors, cameras, camera(String), cameraEvent(String), appliances, outdoor, workshop
    case holiday, holidayControllers, house3D
    case remote(String), music, watch, requests, pipeline
    case calendar, meals, recipe(String), shopping, chores, inventory
    case trips, trip(Int), tripDay(Int, String)
    case systems, storage, activity, people, kidNet, vpnPool
}

@MainActor
@Observable
final class Router {
    static let shared = Router()

    var tab: AppTab = .home
    var paths: [AppTab: [Destination]] = [:]
    var showInbox = false
    /// A WireGuard .conf handed to the app (Files or the Firewalla share sheet); admins add it to the hub's pool.
    var vpnImport: URL?
    var showProfile = false
    var showSearch = false

    func path(_ tab: AppTab) -> Binding<[Destination]> {
        Binding(get: { self.paths[tab] ?? [] }, set: { self.paths[tab] = $0 })
    }

    /// Push a screen on the current tab.
    func go(_ d: Destination) { paths[tab, default: []].append(d) }

    /// Switch tab (and optionally open a screen there).
    func show(_ t: AppTab, _ d: Destination? = nil) {
        tab = t
        if let d { paths[t] = [d] }
    }

    func popToRoot() { paths[tab] = [] }

    /// thomascreek://room/kitchen, thomascreek://inbox, christmaslights://show ...
    func open(_ url: URL) {
        if url.isFileURL { if url.pathExtension.lowercased() == "conf" { vpnImport = url }; return }
        let parts = [url.host ?? ""] + url.pathComponents.filter { $0 != "/" }
        let role = HomeStore.shared.role
        let kid = role == .child
        switch parts.first ?? "" {
        case "home", "": show(.home)
        case "show", "lights", "holiday": kid ? show(.home, .holiday) : show(.house, .holiday)
        case "house3d": show(.house, .house3D)
        case "controllers": show(.house, .holidayControllers)
        case "alerts", "inbox": showInbox = true
        case "room": if parts.count > 1 { kid ? show(.myRoom) : show(.house, .room(parts[1])) }
        case "camera": if parts.count > 1 { show(.house, .camera(parts[1])) } else { show(.house, .cameras) }
        case "doors", "garage": show(.house, .doors)
        case "chores": kid ? show(.chores) : show(.family, .chores)
        case "trips", "trip":
            let id = parts.count > 1 ? Int(parts[1]) : nil
            let tab: AppTab = kid ? .home : .family
            show(tab, id.map { Destination.trip($0) } ?? (kid ? nil : Destination.trips))
        case "requests": kid ? show(.watch) : show(.media, .requests)
        case "media": show(kid ? .watch : .media)
        case "systems", "storage": show(.systems, parts.first == "storage" ? .storage : nil)
        case "people", "devices": show(role == .admin ? .systems : .home, role == .admin ? .people : nil); if role != .admin { showProfile = true }
        case "activity": show(.systems, .activity)
        case "settings", "profile": showProfile = true
        default: show(.home)
        }
    }

    func openCategory(_ cat: String?) {
        switch cat ?? "" {
        case "health", "manager": show(.house, .holidayControllers)
        case "renders", "scene", "show", "control", "weather", "summary": open(URL(string: "thomascreek://holiday")!)
        case "approval", "request": showInbox = true
        default: showInbox = true
        }
    }
}
