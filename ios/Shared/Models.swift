import Foundation

// Shapes the hub sends. Keys arrive in snake_case and are converted to camelCase by HubClient.decoder,
// including keys inside free-form `state` dictionaries ("target_temp" becomes "targetTemp").

// MARK: - Free-form JSON

enum JSONValue: Codable, Hashable {
    case string(String), number(Double), bool(Bool), object([String: JSONValue]), array([JSONValue]), null

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let b = try? c.decode(Bool.self) { self = .bool(b) }
        else if let n = try? c.decode(Double.self) { self = .number(n) }
        else if let s = try? c.decode(String.self) { self = .string(s) }
        else if let a = try? c.decode([JSONValue].self) { self = .array(a) }
        else { self = .object(try c.decode([String: JSONValue].self)) }
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .string(let s): try c.encode(s)
        case .number(let n): try c.encode(n)
        case .bool(let b): try c.encode(b)
        case .object(let o): try c.encode(o)
        case .array(let a): try c.encode(a)
        case .null: try c.encodeNil()
        }
    }

    var bool: Bool? { if case .bool(let b) = self { b } else if case .number(let n) = self { n != 0 } else { nil } }
    var double: Double? { if case .number(let n) = self { n } else if case .string(let s) = self { Double(s) } else { nil } }
    var string: String? {
        switch self {
        case .string(let s): s
        case .number(let n): n == n.rounded() ? String(Int(n)) : String(n)
        case .bool(let b): b ? "true" : "false"
        default: nil
        }
    }
    var array: [JSONValue]? { if case .array(let a) = self { a } else { nil } }
}

// MARK: - Who am I

enum Role: String, Codable {
    case admin, adult, child, guest, pending, tablet
    var isGrownUp: Bool { self == .admin || self == .adult || self == .tablet }
}

enum UIStyle: String, Codable {
    case standard, kid, little
}

struct Person: Codable, Hashable, Identifiable {
    var id: String
    var name: String
    var role: Role?
    var avatar: String?
    var color: String?
    var home: Bool?
    var uiStyle: UIStyle?
    var accessUntil: String?
    var area: String?
}

struct Me: Codable, Hashable {
    var person: Person
    var role: Role
    var capabilities: [String]
    var device: DeviceInfo?
    var remoteUrl: String?
    var atHome: Bool?
    /// The hub is in practice mode: it reads real data but every change is pretend.
    var dryRun: Bool?
    var child: ChildInfo?
    var guest: GuestInfo?

    struct DeviceInfo: Codable, Hashable { var id: String; var name: String?; var status: String? }
    struct ChildInfo: Codable, Hashable {
        var roomId: String?
        var tvDeviceId: String?
        var internet: Internet?
        struct Internet: Codable, Hashable { var paused: Bool; var until: String?; var reason: String? }
    }
    struct GuestInfo: Codable, Hashable {
        var wifiSsid: String?
        var wifiPassword: String?
        var hostName: String?
        var guidance: [Guidance]?
        struct Guidance: Codable, Hashable, Identifiable { var icon: String?; var title: String; var detail: String; var id: String { title } }
    }

    func can(_ c: Capability) -> Bool { role == .admin || capabilities.contains(c.rawValue) }
    var style: UIStyle { person.uiStyle ?? (role == .child ? .kid : .standard) }
    var firstName: String { String(person.name.split(separator: " ").first ?? Substring(person.name)) }
}

/// The capability strings the hub returns in /v1/me. The app hides anything not granted; the hub refuses it anyway.
enum Capability: String {
    case lightsAll = "lights.all", lightsOwnRoom = "lights.own_room"
    case climateAll = "climate.all", climateOwnRoom = "climate.own_room"
    case doors, cameras, appliances
    case mediaAll = "media.all", mediaOwnTV = "media.own_tv", mediaRequest = "media.request", mediaApprove = "media.approve"
    case pipeline
    case holidayFull = "holiday.full", holidayLooks = "holiday.looks", holidayPick = "holiday.pick"
    case familyEdit = "family.edit", familyView = "family.view", shoppingAdd = "shopping.add"
    case choresManage = "chores.manage", choresDo = "chores.do"
    case inventoryEdit = "inventory.edit", inventorySearch = "inventory.search"
    case networkManage = "network.manage", networkAsk = "network.ask"
    case systems, people, activity
    case tripsView = "trips.view", tripsPlan = "trips.plan"
}

// MARK: - The house snapshot (GET /v1/home)

struct HomeSnapshot: Codable, Hashable {
    var generatedAt: String?
    var status: HouseStatus
    var people: [Person]?
    var modes: [Mode]?
    var floors: [Floor]?
    var rooms: [Room]?
    var devices: [DeviceItem]?
    var cameras: [Camera]?
    var nowPlaying: [NowPlaying]?
    var today: Today?
    var systems: Systems?
    var seasonal: Seasonal?
    var weather: Weather?

    struct HouseStatus: Codable, Hashable {
        var level: String
        var line: String
        var items: [StatusItem]?
    }
    struct StatusItem: Codable, Hashable, Identifiable {
        var id: String
        var level: String
        var title: String
        var detail: String?
        var deviceId: String?
        var action: String?
        var actionTitle: String?
        var ref: String?
    }
    struct Seasonal: Codable, Hashable { var holidayLights: Bool? }
    struct Weather: Codable, Hashable { var temp: Double?; var condition: String?; var symbol: String? }

    var generated: Date? { generatedAt.flatMap(ISO.parse) }
    func device(_ id: String?) -> DeviceItem? { id.flatMap { i in devices?.first { $0.id == i } } }
    func room(_ id: String?) -> Room? { id.flatMap { i in rooms?.first { $0.id == i } } }
    func devices(in room: Room) -> [DeviceItem] { room.deviceIds.compactMap { device($0) } }
    func devices(kind: DeviceKind) -> [DeviceItem] { (devices ?? []).filter { $0.kind == kind } }
}

struct Mode: Codable, Hashable, Identifiable { var id: String; var name: String; var symbol: String?; var active: Bool? }
struct Floor: Codable, Hashable, Identifiable { var id: String; var name: String; var roomIds: [String] }

struct Room: Codable, Hashable, Identifiable {
    var id: String
    var name: String
    var floorId: String?
    var symbol: String?
    var summary: String?
    var favorite: Bool?
    var deviceIds: [String]
}

enum DeviceKind: String, Codable {
    case light, fan, `switch`, climate, lock, garage, media, speaker, camera, sensor, vacuum, oven, printer, valve, shower, unknown
    init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = DeviceKind(rawValue: raw) ?? .unknown
    }
}

struct DeviceItem: Codable, Hashable, Identifiable {
    var id: String
    var kind: DeviceKind
    var name: String
    var roomId: String?
    var state: [String: JSONValue]
    var updatedAt: String?
    var available: Bool?
    var actions: [String]?

    subscript(_ key: String) -> JSONValue? { state[key] }
    var isOn: Bool { state["on"]?.bool ?? (state["state"]?.string == "on") }
    var brightness: Double? { state["brightness"]?.double }
    var isAvailable: Bool { available ?? true }
    var updated: Date? { updatedAt.flatMap(ISO.parse) }
}

/// A Frigate camera. Width and height are its real frame size, so very wide (panoramic or dual-lens)
/// cameras are shown at their true shape instead of being cropped to 16:9.
struct Camera: Codable, Hashable, Identifiable {
    var id: String
    var name: String
    var roomId: String?
    var snapshotUrl: String?
    var updatedAt: String?
    var event: String?
    var width: Double?
    var height: Double?
    var online: Bool?
    var lastEvent: LastEvent?
    struct LastEvent: Codable, Hashable { var label: String; var start: String }

    var aspect: Double {
        guard let w = width, let h = height, w > 0, h > 0 else { return 16 / 9 }
        return min(max(w / h, 0.5), 5)
    }
    /// Wider than about 2:1, worth a full-width row.
    var isWide: Bool { aspect > 2.1 }
}

struct NowPlaying: Codable, Hashable, Identifiable {
    var deviceId: String
    var roomId: String?
    var title: String
    var subtitle: String?
    var artUrl: String?
    var state: String?
    var progress: Double?
    var volume: Double?
    var id: String { deviceId }
}

struct Today: Codable, Hashable {
    var events: [CalendarEvent]?
    var dinner: Dinner?
}
struct CalendarEvent: Codable, Hashable, Identifiable {
    var id: String?
    var title: String
    var start: String
    var end: String?
    var who: String?
    var allDay: Bool?
    var location: String?
    var stableID: String { id ?? "\(title)-\(start)" }
}
struct Dinner: Codable, Hashable { var title: String; var imageUrl: String?; var emoji: String?; var recipeId: String? }

struct Systems: Codable, Hashable {
    var storage: [Pool]?
    var apps: Apps?
    var internet: Internet?
    var backups: Backups?
    struct Pool: Codable, Hashable, Identifiable {
        var name: String; var usedPct: Double?; var sizeTb: Double?; var scrubPct: Double?; var healthy: Bool?; var status: String?
        var id: String { name }
        var used: Double { usedPct ?? 0 }
    }
    struct Apps: Codable, Hashable { var total: Int?; var crashed: Int; var updates: Int?; var crashedNames: [String]? }
    struct Internet: Codable, Hashable { var up: Bool; var latencyMs: Double?; var downMbps: Double?; var since: String? }
    struct Backups: Codable, Hashable { var ok: Bool; var last: String? }
}

// MARK: - Inbox, requests, chores, media

struct InboxItem: Codable, Hashable, Identifiable {
    var id: String
    var t: Double
    var level: String?
    var cat: String
    var title: String
    var body: String
    var actions: [InboxAction]?
    var ref: String?
    var acked: Bool?

    struct InboxAction: Codable, Hashable, Identifiable { var id: String; var title: String; var destructive: Bool? }
    var date: Date { Date(timeIntervalSince1970: t) }
    var bucket: InboxLevel { InboxLevel(rawValue: level ?? "fyi") ?? .fyi }
}
enum InboxLevel: String, CaseIterable {
    case actNow = "act_now", decision, maintenance, fyi
    var title: String {
        switch self { case .actNow: "Act now"; case .decision: "Needs a decision"; case .maintenance: "Maintenance"; case .fyi: "FYI" }
    }
}
struct InboxList: Codable { var items: [InboxItem] }

struct MediaRequest: Codable, Hashable, Identifiable {
    var id: String
    var title: String
    var posterUrl: String?
    var who: String?
    var status: String
    var progress: Double?
    var createdAt: String?
    var kind: String?
    var text: String?
    var waiting: Bool { status == "waiting" }
}
struct RequestList: Codable { var items: [MediaRequest] }

struct MediaItem: Codable, Hashable, Identifiable {
    var id: String
    var title: String
    var year: Int?
    var kind: String?
    var posterUrl: String?
    var rating: String?
    var summary: String?
    var progress: Double?
    var subtitle: String?
}
struct MediaList: Codable { var items: [MediaItem]; var requestable: [MediaItem]? }

/// A child's chores. Decoding is forgiving because the family board's own shape passes through the hub.
struct ChoreBoard: Codable, Hashable {
    var person: String?
    var personId: String?
    var points: Int
    var streak: Int?
    var goal: Int?
    var items: [Chore]
    var rewards: [Reward]?
    struct Chore: Codable, Hashable, Identifiable {
        var id: String; var title: String; var icon: String?; var done: Bool; var points: Int?; var who: String?
        /// Done, waiting for a parent to check it.
        var waiting: Bool = false
        /// Another chore comes first.
        var locked: Bool = false
        var time: String?
        var word: String?
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: AnyKey.self)
            id = c.string("id") ?? UUID().uuidString
            title = c.string("title") ?? c.string("name") ?? "Chore"
            icon = c.string("icon") ?? c.string("emoji")
            done = c.bool("done") ?? c.bool("completed") ?? c.bool("isDone") ?? false
            points = c.int("points") ?? c.int("value")
            who = c.string("who")
            waiting = c.bool("waiting") ?? false
            locked = c.bool("locked") ?? false
            time = c.string("time")
            word = c.string("word")
        }
    }
    struct Reward: Codable, Hashable, Identifiable {
        var id: String; var title: String; var icon: String?; var cost: Int; var available: Bool?
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: AnyKey.self)
            id = c.string("id") ?? UUID().uuidString
            title = c.string("title") ?? c.string("name") ?? "Reward"
            icon = c.string("icon") ?? c.string("emoji")
            cost = c.int("cost") ?? c.int("points") ?? 0
            available = c.bool("available")
        }
    }
    var doneCount: Int { items.filter(\.done).count }
    var date: String?

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        if let name = c.string("person") { person = name }
        else if let p = try? c.nestedContainer(keyedBy: AnyKey.self, forKey: AnyKey("person")) {
            person = p.string("name"); personId = p.string("id")
        }
        points = c.int("points") ?? c.int("balance") ?? 0
        streak = c.int("streak")
        goal = c.int("goal")
        date = c.string("date")
        items = (try? c.decode([Chore].self, forKey: AnyKey("items"))) ?? (try? c.decode([Chore].self, forKey: AnyKey("chores"))) ?? []
        rewards = (try? c.decode([Reward].self, forKey: AnyKey("rewards")))
    }
}

/// Keyed access to JSON whose exact shape we don't control.
struct AnyKey: CodingKey {
    var stringValue: String; var intValue: Int?
    init(_ s: String) { stringValue = s }
    init?(stringValue: String) { self.stringValue = stringValue }
    init?(intValue: Int) { self.stringValue = String(intValue); self.intValue = intValue }
}

extension KeyedDecodingContainer where K == AnyKey {
    func string(_ k: String) -> String? {
        if let s = try? decodeIfPresent(String.self, forKey: AnyKey(k)) { return s }
        if let i = try? decodeIfPresent(Int.self, forKey: AnyKey(k)) { return String(i) }
        return nil
    }
    func int(_ k: String) -> Int? {
        if let i = try? decodeIfPresent(Int.self, forKey: AnyKey(k)) { return i }
        if let d = try? decodeIfPresent(Double.self, forKey: AnyKey(k)) { return Int(d) }
        return nil
    }
    func bool(_ k: String) -> Bool? { try? decodeIfPresent(Bool.self, forKey: AnyKey(k)) }
}

struct ShoppingLists: Codable, Hashable {
    var lists: [ShoppingList]
    struct ShoppingList: Codable, Hashable, Identifiable { var id: String; var name: String; var items: [Item] }
    struct Item: Codable, Hashable, Identifiable { var id: String; var text: String; var done: Bool }
}

struct MealWeek: Codable, Hashable {
    var items: [Day]
    struct Day: Codable, Hashable, Identifiable {
        var date: String; var meal: String?; var title: String?; var emoji: String?; var imageUrl: String?; var slug: String?
        var id: String { "\(date)-\(meal ?? "")-\(title ?? "")" }
    }
}
struct Recipe: Codable, Hashable {
    var id: String
    var title: String
    var imageUrl: String?
    var totalTime: String?
    var servings: String?
    var ingredients: [String]
    var steps: [String]
}

struct InventoryItem: Codable, Hashable, Identifiable {
    var id: String
    var name: String
    var location: String?
    var detail: String?
    var lastService: String?
}
struct InventoryList: Codable { var items: [InventoryItem] }

struct ActivityEntry: Codable, Hashable, Identifiable {
    var id: String
    var t: Double
    var who: String?
    var action: String?
    var target: String?
    var outcome: String?
    var detail: String?
    var source: String?
    var date: Date { Date(timeIntervalSince1970: t) }
    /// "Opened the large garage door" style line.
    var what: String { detail ?? [action, target].compactMap { $0 }.joined(separator: " ") }

    private enum K: String, CodingKey { case id, t, at, who, action, target, outcome, detail, source }
    /// Lenient: id may be a number or a string, and the time may be epoch seconds or ISO text.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: K.self)
        if let s = try? c.decode(String.self, forKey: .id) { id = s }
        else if let n = try? c.decode(Int.self, forKey: .id) { id = String(n) }
        else { id = UUID().uuidString }
        if let n = try? c.decode(Double.self, forKey: .t) { t = n }
        else if let s = (try? c.decode(String.self, forKey: .t)) ?? (try? c.decode(String.self, forKey: .at)), let d = ISO.parse(s) { t = d.timeIntervalSince1970 }
        else { t = 0 }
        who = try? c.decode(String.self, forKey: .who)
        action = try? c.decode(String.self, forKey: .action)
        target = try? c.decode(String.self, forKey: .target)
        outcome = try? c.decode(String.self, forKey: .outcome)
        detail = try? c.decode(String.self, forKey: .detail)
        source = try? c.decode(String.self, forKey: .source)
    }
    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: K.self)
        try c.encode(id, forKey: .id); try c.encode(t, forKey: .t)
        try c.encodeIfPresent(who, forKey: .who); try c.encodeIfPresent(action, forKey: .action)
        try c.encodeIfPresent(target, forKey: .target); try c.encodeIfPresent(outcome, forKey: .outcome)
        try c.encodeIfPresent(detail, forKey: .detail); try c.encodeIfPresent(source, forKey: .source)
    }
}
struct ActivityList: Codable {
    var items: [ActivityEntry]
    var nextBefore: JSONValue?
    var before: String? { nextBefore?.string }
}

struct EnrolledDevice: Codable, Hashable, Identifiable {
    var id: String
    var name: String
    var model: String?
    var status: String
    var lastSeen: String?
}
struct PeopleList: Codable {
    var people: [PersonEntry]
    var unassigned: [EnrolledDevice]?
    struct PersonEntry: Codable, Hashable, Identifiable {
        var id: String; var name: String; var role: Role?; var color: String?; var devices: [EnrolledDevice]?
        var person: Person { Person(id: id, name: name, role: role, color: color) }
    }
}

struct CameraPass: Codable { var playerUrl: String; var hlsUrl: String?; var expiresAt: String? }
/// One Frigate detection (person, car, package...).
struct CameraEvent: Codable, Hashable, Identifiable {
    var id: String
    var label: String
    var start: String
    var end: String?
    var camera: String?
    var cameraName: String?
    var subLabel: String?
    var score: Double?
    var zones: [String]?
    var thumbUrl: String?
    var snapshotUrl: String?
    var clipUrl: String?

    var date: Date { ISO.parse(start) ?? .distantPast }
    var title: String { (subLabel?.isEmpty == false ? subLabel! : label).capitalized }
    var symbol: String {
        switch label.lowercased() {
        case "person", "face": "figure.walk"
        case "car", "truck", "motorcycle", "bus": "car.fill"
        case "bicycle": "bicycle"
        case "dog": "dog.fill"
        case "cat": "cat.fill"
        case "bird": "bird.fill"
        case "deer", "bear", "horse", "animal": "pawprint.fill"
        case "package": "shippingbox.fill"
        default: "video.fill"
        }
    }
}
struct CameraEvents: Codable { var items: [CameraEvent]; var nextBefore: String? }
struct CameraList: Codable { var cameras: [Camera] }
struct StreamInfo: Codable { var hlsUrl: String; var expiresAt: String? }

/// Requests (Overseerr), active downloads, what was recently added, what's coming up, and anything broken.
struct Pipeline: Codable, Hashable {
    var requests: [Request]?
    var downloads: [Download]?
    var recent: [Item]?
    var upcoming: [Item]?
    var failures: [Failure]?
    struct Request: Codable, Hashable {
        var kind: String?; var title: String; var year: Int?; var posterUrl: String?
        var status: String?; var mediaStatus: String?; var requestedBy: String?; var requestedAt: String?
        var stableID: String { title + (requestedAt ?? "") }
    }
    struct Download: Codable, Hashable, Identifiable {
        var title: String; var progress: Double?; var etaS: Double?; var state: String?; var source: String?; var error: String?
        var id: String { title }
    }
    struct Item: Codable, Hashable, Identifiable {
        var kind: String?; var title: String; var detail: String?; var at: String?; var source: String?; var hasFile: Bool?
        var id: String { title + (detail ?? "") + (at ?? "") }
        var date: Date? { at.flatMap(ISO.parse) }
    }
    struct Failure: Codable, Hashable, Identifiable {
        var source: String?; var error: String?; var title: String?; var detail: String?
        var id: String { (source ?? "") + (error ?? title ?? "") }
    }
}

struct ActionReply: Codable { var ok: Bool?; var message: String?; var dryRun: Bool? }

/// One SSE message from /v1/events.
struct HubEvent: Decodable {
    var type: String
    var data: JSONValue?
}

enum ISO {
    static func parse(_ s: String) -> Date? {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = f.date(from: s) { return d }
        f.formatOptions = [.withInternetDateTime]
        if let d = f.date(from: s) { return d }
        f.formatOptions = [.withFullDate]
        f.timeZone = .current
        return f.date(from: s)
    }
}
