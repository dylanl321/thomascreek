import CoreSpotlight
import UniformTypeIdentifiers

/// Puts this person's rooms, devices, cameras and screens into iPhone search (Spotlight).
/// Each item's identifier is its deep link, so tapping it opens that screen. Only what this
/// person can use is indexed, and the whole index is replaced when the house or the person changes.
@MainActor
enum SpotlightIndex {
    private static var lastKey = ""
    private static var lastAt = Date.distantPast
    private static let domain = "house"

    static func update(_ home: HomeSnapshot?, me: Me?) {
        guard let home, let me, me.role != .pending, CSSearchableIndex.isIndexingAvailable() else { return }
        let rooms = home.rooms ?? [], devices = home.devices ?? [], cameras = home.cameras ?? []
        let key = "\(me.person.id)|\(me.role.rawValue)|\(rooms.map(\.id).joined())|\(devices.count)|\(cameras.count)"
        // Same person and same house: refresh at most every 6 hours.
        if key == lastKey && Date.now.timeIntervalSince(lastAt) < 6 * 3600 { return }
        lastKey = key; lastAt = .now

        var items: [CSSearchableItem] = []
        func add(_ link: String, _ title: String, _ detail: String?, _ words: [String]) {
            let a = CSSearchableItemAttributeSet(contentType: .content)
            a.title = title
            a.contentDescription = detail
            a.keywords = words + ["Thomas Creek"]
            items.append(CSSearchableItem(uniqueIdentifier: link, domainIdentifier: domain, attributeSet: a))
        }
        for r in rooms { add("thomascreek://room/\(r.id)", r.name, "Room", ["room", "lights"]) }
        let roomName = Dictionary(rooms.map { ($0.id, $0.name) }, uniquingKeysWith: { a, _ in a })
        var seen = Set<String>()
        for d in devices where d.isAvailable && seen.insert(d.name.lowercased()).inserted {
            let room = d.roomId.flatMap { roomName[$0] }
            switch d.kind {
            case .light, .fan, .switch:
                add("thomascreek://room/\(d.roomId ?? "")", d.name, ["Light", room].compactMap { $0 }.joined(separator: " · "), ["light", "lamp"])
            case .garage, .lock:
                add("thomascreek://doors", d.name, d.kind == .garage ? "Garage door" : "Lock", ["door", "garage", "lock"])
            case .media:
                add("thomascreek://remote/\(d.id)", d.name, ["TV remote", room].compactMap { $0 }.joined(separator: " · "), ["tv", "remote"])
            default: break
            }
        }
        if me.can(.cameras) {
            for c in cameras { add("thomascreek://camera/\(c.id)", c.name, "Camera · live", ["camera", "live"]) }
        }
        for s in screens(me) { add(s.link, s.title, s.detail, s.words) }

        let index = CSSearchableIndex.default()
        index.deleteSearchableItems(withDomainIdentifiers: [domain]) { _ in
            index.indexSearchableItems(items) { _ in }
        }
    }

    static func clear() {
        lastKey = ""
        CSSearchableIndex.default().deleteSearchableItems(withDomainIdentifiers: [domain]) { _ in }
    }

    private struct Screen { var link: String; var title: String; var detail: String; var words: [String] }

    private static func screens(_ me: Me) -> [Screen] {
        let grown = me.role == .admin || me.role == .adult
        var out: [Screen] = []
        if grown { out.append(Screen(link: "thomascreek://profile", title: "Away from home", detail: "VPN", words: ["vpn", "wireguard"])) }
        if me.can(.networkManage) { out.append(Screen(link: "thomascreek://kids", title: "Kids' devices", detail: "Pause and screen time", words: ["internet", "wifi", "pause"])) }
        if me.can(.appliances) { out.append(Screen(link: "thomascreek://kitchen", title: "Kitchen", detail: "Ovens and timers", words: ["oven", "preheat"])) }
        if me.can(.familyEdit) {
            out.append(Screen(link: "thomascreek://recipes", title: "Recipes", detail: "Mealie", words: ["recipe", "cook"]))
            out.append(Screen(link: "thomascreek://dinners", title: "Dinners", detail: "Meal plan", words: ["dinner", "meals"]))
        }
        if me.can(.tripsView) { out.append(Screen(link: "thomascreek://trips", title: "Trips", detail: "Plans and packing", words: ["trip", "vacation"])) }
        if me.can(.choresDo) || me.can(.choresManage) { out.append(Screen(link: "thomascreek://chores", title: "Chores", detail: "Jobs and stars", words: ["chores", "jobs"])) }
        if me.can(.systems) { out.append(Screen(link: "thomascreek://hubweb", title: "Hub web page", detail: "Add keys from a computer", words: ["hub", "keys"])) }
        out.append(Screen(link: "thomascreek://holiday", title: "Holiday lights", detail: "Show and looks", words: ["christmas", "lights", "show"]))
        return out
    }
}
