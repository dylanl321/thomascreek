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
    /// The dollhouse page for a room that has a model. The existing room screen stays on `.room`.
    case dollhouse(String)
    case climate, climateHistory(String), doors, cameras, camera(String), cameraEvent(String), appliances, outdoor, workshop, printLibrary
    case holiday, holidayControllers, house3D
    case remote(String), music, watch, requests, pipeline, askMedia, mediaTitle(String), mediaRequest(String)
    case kidsIPads                      // what's on the kids' iPads; send them shows
    case tvSetup                        // find and add TVs and boxes for everyone's remote
    case tvGuide                        // live guide, series and movies, played on a house TV
    case tvSeries(String), tvMovie(String)
    case roomEdit(String), hiddenDevices  // admin: set up a room; devices hidden for everyone and clean-up
    case calendar, meals, recipe(String), shopping, chores, inventory
    case kitchen(String?), dinners, recipes
    case trips, trip(Int), tripDay(Int, String)
    case systems, storage, activity, people, kidNet, vpnPool, hubWeb, youtube
    case nasApps(String?), nasApp(String), nasLogs(String, String?), nasUpdates, nasSettings
    case today, leaving, intercom, guestPasses, bedtimes, feedback
    case weather, weatherAlert(String), weatherRadar, weatherOutlook
    /// Weather for one place: "home", "here" or "trip:<id>" (a trip's forecast).
    case weatherPlace(String)
    /// One device on its own page, and one person (the web of links: anything mentioned opens).
    case device(String), person(String)
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
    /// `thomascreek://tv/{code}` from the QR a TV shows while it waits to be approved.
    var tvApprove: String?

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

    /// Switch tab (and optionally open a screen there). While following a link in place, the screen
    /// opens on top of the current one instead, so Back returns to where the person tapped.
    func show(_ t: AppTab, _ d: Destination? = nil) {
        if inPlace, let d { go(d); return }
        tab = t
        if let d { paths[t] = [d] }
    }

    /// Open several screens at once (Weather › an alert), as `show` does for one.
    private func stack(_ t: AppTab, _ ds: [Destination]) {
        if inPlace { paths[tab, default: []] += ds; return }
        tab = t
        paths[t] = ds
    }

    private var inPlace = false

    /// Follow a reference the person tapped (a temperature, a device, a person…): open it on top of
    /// this screen. Skips it when that exact screen is already on top.
    func follow(_ d: Destination) {
        guard paths[tab]?.last != d else { return }
        go(d)
    }

    /// Follow a thomascreek:// reference from the hub (a card, a status line, an answer) in place.
    /// Anything the link rules don't allow for this person falls back exactly as a deep link does.
    func follow(_ url: URL) {
        guard url.scheme == "thomascreek" || url.scheme == "christmaslights" else {
            UIApplication.shared.open(url); return
        }
        inPlace = true
        defer { inPlace = false }
        open(url)
    }

    /// Whether this person may open a screen from a link. Mirrors the hub's capabilities; the hub refuses anyway.
    func allows(_ d: Destination) -> Bool {
        let store = HomeStore.shared
        let me = store.me
        let kid = store.role == .child
        switch d {
        case .weather, .weatherPlace, .weatherAlert, .weatherRadar, .weatherOutlook, .today: return true
        case .climate: return !kid && store.role != .guest
        case .climateHistory: return me?.can(.climateAll) == true || me?.can(.climateOwnRoom) == true
        case .device(let id):
            guard let dev = store.home?.device(id) else { return false }
            return !kid && store.role != .guest && Self.deviceAllowed(dev, me)
        case .room, .dollhouse: return !kid && store.role != .guest
        case .person: return store.role.isGrownUp
        case .camera, .cameraEvent, .cameras: return me?.can(.cameras) == true
        case .doors: return me?.can(.doors) == true
        case .kitchen, .appliances: return me?.can(.appliances) == true
        case .workshop, .printLibrary: return me?.can(.appliances) == true || kid
        case .storage, .nasApps, .nasApp, .nasLogs, .nasUpdates, .nasSettings: return store.role == .admin
        case .systems, .activity: return me?.can(.systems) == true || me?.can(.activity) == true
        case .people, .feedback, .vpnPool, .hubWeb: return store.role == .admin
        case .trips, .trip, .tripDay: return me?.can(.tripsView) == true
        case .recipe, .recipes, .meals, .dinners: return true
        case .tvGuide, .tvSeries, .tvMovie: return me?.can(.mediaAll) == true || me?.can(.mediaOwnTV) == true
        case .calendar, .shopping, .inventory, .chores: return !kid && store.role != .guest
        case .kidNet: return me?.can(.networkManage) == true
        default: return !kid && store.role != .guest
        }
    }

    private static func deviceAllowed(_ d: DeviceItem, _ me: Me?) -> Bool {
        switch d.kind {
        case .lock, .garage: me?.can(.doors) == true
        case .camera: me?.can(.cameras) == true
        case .oven, .vacuum, .printer, .shower: me?.can(.appliances) == true
        case .climate: me?.can(.climateAll) == true || me?.can(.climateOwnRoom) == true
        default: true
        }
    }

    func popToRoot() { paths[tab] = [] }

    /// Back to Home with nothing open (starting or ending "View as": the tabs change underneath).
    func reset() {
        paths = [:]; tab = .home
        showInbox = false; showProfile = false; showVPN = false; showOffline = false; showSearch = false; showAssistant = false
        showEditHome = false; showViewAs = false; youtubeAdd = nil; tvApprove = nil
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
        case "dollhouse", "living-room", "livingroom":
            let id = parts.count > 1 ? parts[1] : "living_room"
            kid ? show(.myRoom) : show(.house, .dollhouse(id))
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
        case "guide": show(kid ? .watch : .media, .tvGuide)
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
        case "tvs": if role == .admin || role == .adult { show(.media, .tvSetup) } else { show(.home) }
        case "tv":
            // thomascreek://tv/{code}: the QR on a TV that is waiting to be approved.
            let code = parts.count > 1 ? parts[1] : ""
            if !code.isEmpty { tvApprove = code }
        case "remote": if parts.count > 1, !kid { show(.media, .remote(parts[1])) } else if kid { show(.myRoom) }
        case "climate":
            // thomascreek://climate, /climate/<entity_id> (one thermostat's history)
            if parts.count > 1, parts[1].hasPrefix("climate.") {
                if kid { show(.myRoom, .climateHistory(parts[1])) } else { tab = .house; paths[.house] = [.climate, .climateHistory(parts[1])] }
            } else { kid ? show(.myRoom) : show(.house, .climate) }
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
            else if sub == "alert", parts.count > 2 { stack(.home, [.weather, .weatherAlert(parts[2...].joined(separator: "/"))]) }
            else if sub == "radar" { stack(.home, [.weather, .weatherRadar]) }
            else if sub == "outlook" { stack(.home, [.weather, .weatherOutlook]) }
            else if sub == "place", parts.count > 2 { show(.home, .weatherPlace(parts[2...].joined(separator: "/"))) }
            else { show(.home, .weather) }
        case "device":
            // thomascreek://device/<entity id>: the device's own page (kids get their room).
            if parts.count > 1 {
                let d = Destination.device(parts[1...].joined(separator: "/"))
                if allows(d) { show(.house, d) } else if kid { show(.myRoom) }
            }
        case "person":
            if parts.count > 1, role.isGrownUp { show(.family, .person(parts[1])) }
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
