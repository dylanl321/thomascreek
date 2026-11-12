import Foundation

// Shapes for family life: Today, who's home, leaving, intercom, guest passes, bedtime, feedback.
// Shared with the widgets and the Watch bridge.

struct FamilyMember: Codable, Hashable, Identifiable {
    var id: String
    var name: String
    var first: String
    var role: Role?
    var avatar: String?
    var uiStyle: UIStyle?
    var home: Bool?
    var since: String?
    var `where`: String?
    var me: Bool?
    var sinceDate: Date? { since.flatMap(ISO.parse) }
}

struct FamilyPresence: Codable { var people: [FamilyMember] }

struct TodayInfo: Codable, Hashable {
    var date: String
    var partOfDay: String
    var headline: String
    var weather: TodayWeather?
    var events: [CalendarEvent]?
    var dinner: TodayDinner?
    var family: [FamilyMember]?
    var chores: TodayChores?
    var bedtime: Bedtime?
    var kidsBedtimes: [KidBedtime]?
    var trip: TodayTrip?

    struct TodayWeather: Codable, Hashable {
        var temp: Double?; var condition: String?; var symbol: String?
        var high: Double?; var low: Double?; var rainChance: Double?; var rainAt: String?
        var sunset: String?; var sunrise: String?; var tonightLow: Double?
        var line: String?
    }
    struct TodayDinner: Codable, Hashable { var title: String; var imageUrl: String?; var slug: String?; var time: String? }
    struct TodayChores: Codable, Hashable {
        var mine: Mine?
        var kids: [Kid]?
        struct Mine: Codable, Hashable { var done: Int; var total: Int; var points: Int; var next: String? }
        struct Kid: Codable, Hashable, Identifiable { var id: String; var name: String; var done: Int; var total: Int; var points: Int }
    }
    struct KidBedtime: Codable, Hashable, Identifiable {
        var id: String; var name: String
        var enabled: Bool?; var time: String?; var wake: String?; var label: String?; var wakeLabel: String?
        var active: Bool?; var minutesLeft: Int?; var extendedUntil: String?
    }
    struct TodayTrip: Codable, Hashable { var id: Int?; var title: String; var startDate: String?; var status: String?; var daysUntil: Int? }
}

struct Bedtime: Codable, Hashable {
    var enabled: Bool?
    var time: String?
    var wake: String?
    var label: String?
    var wakeLabel: String?
    var active: Bool?
    var minutesLeft: Int?
    var extendedUntil: String?
}

struct Departure: Codable, Hashable {
    var lastOneOut: Bool
    var othersHome: [String]
    var open: [OpenThing]
    var allClear: Bool
    var line: String?

    struct OpenThing: Codable, Hashable, Identifiable {
        var id: String
        var kind: String
        var title: String
        var fix: String
        var action: String
        var target: String?
        var params: JSONValue?
        var secure: Bool?
    }
}

struct PresenceReply: Codable {
    var ok: Bool?
    var departure: Departure?
    var welcome: Welcome?
    struct Welcome: Codable { var mode: String?; var name: String?; var message: String? }
}

struct WelcomeSetting: Codable {
    var mode: String?
    var modes: [Mode]?
}

struct IntercomInfo: Codable {
    var phrases: [Phrase]
    var rooms: [IntercomRoom]
    struct Phrase: Codable, Hashable, Identifiable { var id: String; var emoji: String; var text: String }
    struct IntercomRoom: Codable, Hashable, Identifiable { var id: String; var name: String; var speakers: Int }
}

struct GuestPass: Codable, Hashable, Identifiable {
    var id: String
    var name: String
    var startsAt: String?
    var expiresAt: String?
    var state: String
    var views: Int?
    var showWifi: Bool?
    var hasDoorCode: Bool?
    var doorName: String?
    var lockSet: Bool?
    var note: String?
    var url: String?
    var expires: Date? { expiresAt.flatMap(ISO.parse) }
    var starts: Date? { startsAt.flatMap(ISO.parse) }
}

struct GuestPassList: Codable {
    var passes: [GuestPass]
    var wifiSsid: String?
    var canSetLock: Bool?
    var doorName: String?
    var remote: Bool?
}

struct FeedbackItem: Codable, Hashable, Identifiable {
    var id: String
    var t: String?
    var who: String
    var text: String
    var screen: String?
    var appBuild: String?
    var shot: String?
    var done: Bool?
    var date: Date? { t.flatMap(ISO.parse) }
}

struct FeedbackList: Codable { var items: [FeedbackItem] }
