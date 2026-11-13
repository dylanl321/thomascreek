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
    case climate, doors, cameras, camera(String), cameraEvent(String), appliances, outdoor, workshop, printLibrary
    case holiday, holidayControllers, house3D
    case remote(String), music, watch, requests, pipeline, askMedia, mediaTitle(String), mediaRequest(String)
    case kidsIPads                      // what's on the kids' iPads; send them shows
    case calendar, meals, recipe(String), shopping, chores, inventory
    case kitchen(String?), dinners, recipes
    case trips, trip(Int), tripDay(Int, String)
    case systems, storage, activity, people, kidNet, vpnPool, hubWeb, youtube
    case nasApps(String?), nasApp(String), nasLogs(String, String?), nasUpdates, nasSettings
    case today, leaving, intercom, guestPasses, bedtimes, feedback
    case weather, weatherAlert(String), weatherRadar, weatherOutlook
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
    var showVPN = false
    /// Storage and downloads (from the offline strip).
    var showOffline = false
    var showSearch = false
    /// Words to put in the search box when it opens (Spotlight, Siri, thomascreek://search?q=).
    var searchQuery: String?
    /// Opens "Add from YouTube", prefilled with this link ("" for an empty box).
    var youtubeAdd: String?

    /// Ask Thomas Creek, the quick chat and voice panel.
    var showAssistant = false
    /// Reorder and hide Home sections, and arrange favorites.
    var showEditHome = false
    /// Dylan's "View as" picker.
    var showViewAs = false

    /// Open search, optionally with words already typed.
    func startSearch(_ q: String? = nil) {
        searchQuery = q
        showSearch = true
    }

    /// Hand a question from search to Ask Thomas Creek, once the search sheet has gone.
    func ask(_ q: String) {
        showSearch = false
        Task { @MainActor in
            try? await Task.sleep(for: .milliseconds(450))
            showAssistant = true
            AssistantModel.shared.send(q, screen: screenName)
        }
    }

    /// Where the person is, so "turn that off" can mean the thing on screen.
    var screenName: String {
        let top = paths[tab]?.last.map { " › \(String(describing: $0))" } ?? ""
        return tab.rawValue.capitalized + top
    }

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

    /// Back to Home with nothing open (starting or ending "View as": the tabs change underneath).
    func reset() {
        paths = [:]; tab = .home
        showInbox = false; showProfile = false; showVPN = false; showOffline = false; showSearch = false; showAssistant = false
        showEditHome = false; showViewAs = false; youtubeAdd = nil
    }

    /// thomascreek://room/kitchen, thomascreek://inbox, christmaslights://show ...
    func open(_ url: URL) {
        if url.isFileURL { if url.pathExtension.lowercased() == "conf" { vpnImport = url }; return }
        let parts = [url.host ?? ""] + url.pathComponents.filter { $0 != "/" }
        let role = HomeStore.shared.role
        let kid = role == .child
        switch parts.first ?? "" {
        case "home", "": show(.home)
        case "ask", "talk":
            showAssistant = true
            if parts.first == "talk" { Task { await AssistantModel.shared.startListening(screen: screenName) } }
        case "show", "lights", "holiday": kid ? show(.home, .holiday) : show(.house, .holiday)
        case "house3d": show(.house, .house3D)
        case "controllers": show(.house, .holidayControllers)
        case "alerts", "inbox": showInbox = true
        case "room": if parts.count > 1 { kid ? show(.myRoom) : show(.house, .room(parts[1])) }
        case "camera": if parts.count > 1 { show(.house, .camera(parts[1])) } else { show(.house, .cameras) }
        case "doors", "garage": show(.house, .doors)
        case "kitchen", "oven": kid ? show(.home) : show(.family, .kitchen(parts.count > 1 ? parts[1] : nil))
        case "dinners", "meals": kid ? show(.home) : show(.family, .meals)
        case "recipes": kid ? show(.home) : show(.family, .recipes)
        case "recipe": if parts.count > 1 { show(kid ? .home : .family, .recipe(parts[1])) }
        case "kids": if !kid { show(.family, .kidNet) }
        case "workshop", "printer", "printers":
            let lib = parts.count > 1 && parts[1] == "library"
            kid ? show(.home, lib ? .printLibrary : .workshop) : show(.house, lib ? .printLibrary : .workshop)
        case "event": if parts.count > 1, !kid { show(.house, .cameraEvent(parts[1])) }
        case "chores": kid ? show(.chores) : show(.family, .chores)
        case "trips", "trip":
            let id = parts.count > 1 ? Int(parts[1]) : nil
            let tab: AppTab = kid ? .home : .family
            show(tab, id.map { Destination.trip($0) } ?? (kid ? nil : Destination.trips))
        case "requests": kid ? show(.watch) : show(.media, .requests)
        case "media":
            // thomascreek://media/requests/123 (the "ready to watch" push), thomascreek://media/ask
            let t: AppTab = kid ? .watch : .media
            if parts.count > 2, parts[1] == "requests" { tab = t; paths[t] = (kid ? [] : [.requests]) + [.mediaRequest(parts[2])] }
            else if parts.count > 1, parts[1] == "requests" { kid ? show(.watch) : show(.media, .requests) }
            else if parts.count > 1, parts[1] == "ask" { show(t, .askMedia) }
            else { show(t) }
        case "youtube":
            // thomascreek://youtube/add?url=<link> (shared in), or the note parents get when a child adds one.
            let link = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?.first { $0.name == "url" }?.value
            if parts.count > 1 && parts[1] == "add" || link != nil {
                show(kid ? .watch : .media); youtubeAdd = link ?? ""
            } else if role == .admin || role == .adult { show(.family, .youtube) } else { show(.watch) }
        case "systems", "storage": show(.systems, parts.first == "storage" ? .storage : nil)
        case "nas":
            guard role == .admin else { show(.home); break }
            switch parts.count > 1 ? parts[1] : "" {
            case "app" where parts.count > 2: paths[.systems] = [.storage, .nasApp(parts[2])]; tab = .systems
            case "updates": paths[.systems] = [.storage, .nasUpdates]; tab = .systems
            case "apps": paths[.systems] = [.storage, .nasApps(nil)]; tab = .systems
            default: show(.systems, .storage)
            }
        case "people", "devices": show(role == .admin ? .systems : .home, role == .admin ? .people : nil); if role != .admin { showProfile = true }
        case "activity": show(.systems, .activity)
        case "settings", "profile": showProfile = true
        case "vpn": if !kid { showVPN = true }
        case "favorites", "edit-home": if !kid { show(.home) }; showEditHome = true
        case "view-as": if role == .admin { showProfile = true }
        case "search": startSearch(URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?.first { $0.name == "q" }?.value)
        case "remote": if parts.count > 1, !kid { show(.media, .remote(parts[1])) } else if kid { show(.myRoom) }
        case "climate": kid ? show(.myRoom) : show(.house, .climate)
        case "music": if !kid { show(.media, .music) }
        case "calendar": if !kid { show(.family, .calendar) }
        case "shopping": if !kid { show(.family, .shopping) }
        case "inventory": if !kid { show(.family, .inventory) }
        case "pipeline": if !kid { show(.media, .pipeline) }
        case "kidsipads", "sends": kid ? show(.watch) : show(.media, .kidsIPads)
        case "hubweb": show(.systems, .hubWeb)
        case "vpnpool": show(.systems, .vpnPool)
        case "downloads": showOffline = true
        case "today": show(.home, .today)
        case "weather":
            // thomascreek://weather, /weather/alert/<id> (pushes and the warning card), /weather/radar, /weather/outlook
            let sub = parts.count > 1 ? parts[1] : ""
            if kid { show(.home, .weather) }
            else if sub == "alert", parts.count > 2 { tab = .home; paths[.home] = [.weather, .weatherAlert(parts[2...].joined(separator: "/"))] }
            else if sub == "radar" { tab = .home; paths[.home] = [.weather, .weatherRadar] }
            else if sub == "outlook" { tab = .home; paths[.home] = [.weather, .weatherOutlook] }
            else { show(.home, .weather) }
        case "leaving", "lastout": if !kid { show(.home, .leaving) }
        case "intercom", "announce": if !kid { show(.family, .intercom) }
        case "guests", "guestpass": if !kid { show(.family, .guestPasses) }
        case "bedtime", "bedtimes": if !kid { show(.family, .bedtimes) }
        case "feedback": if role == .admin { show(.systems, .feedback) }
        case "tell": TellDylan.shared.open()
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
