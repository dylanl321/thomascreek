import Foundation
import SwiftUI

// MARK: - Holiday lights: the show player's /status, passed through the hub at /v1/lights/status

struct ShowStatus: Codable, Equatable {
    var running: Bool
    var scene: String?
    var since: String?
    var log: [String]?
    var sceneSince: String?
    var position: Int?
    var length: Int?
    var queue: [String]?
    var picked: Int?
    var scenes: [String]?
    var brightness: Int
    var look: String?
    var schedule: Schedule?
    var health: Health?
    var ha: Bool?

    struct Schedule: Codable, Equatable {
        var enabled: Bool
        var skip: Bool
        var night: String
        var start: String
        var calm: String
        var off: String
        var phase: String
    }

    struct Health: Codable, Equatable {
        var checked: String?
        var controllers: [String: Controller]?
    }

    struct Controller: Codable, Equatable {
        var name: String
        var ok: Bool
        var live: Bool?
        var ms: Int?
    }

    /// Controllers sorted by name, with their IP.
    var controllerList: [(ip: String, c: Controller)] {
        (health?.controllers ?? [:]).map { ($0.key, $0.value) }.sorted { $0.c.name < $1.c.name }
    }
    var controllersDown: Int { controllerList.filter { !$0.c.ok }.count }

    var phase: ShowPhase { ShowPhase(rawValue: schedule?.phase ?? "day") ?? .day }
    var currentLook: Look? { look.flatMap { Look(rawValue: $0) } }

    /// One line for widgets and Siri: "Playing Snowfall", "Calm", "Lights off".
    var headline: String {
        if running, let scene { return scene }
        if running { return "Loop starting" }
        if let l = currentLook { return l == .off ? "Lights off" : l.title }
        return "Idle"
    }
    var brightnessPercent: Int { Int((Double(brightness) * 100 / 255).rounded()) }
}

enum ShowPhase: String {
    case show, calm, day
    var title: String {
        switch self { case .show: "Show time"; case .calm: "Calm for the night"; case .day: "Daytime" }
    }
    var symbol: String {
        switch self { case .show: "sparkles"; case .calm: "moon.stars.fill"; case .day: "sun.max.fill" }
    }
}

// MARK: - Looks (WLED presets on every controller)

enum Look: String, CaseIterable, Identifiable, Codable {
    case elegant, twinkle, festive, calm, off
    var id: String { rawValue }
    var title: String {
        switch self {
        case .elegant: "Elegant"; case .twinkle: "Twinkle"; case .festive: "Festive"; case .calm: "Calm"; case .off: "Off"
        }
    }
    var detail: String {
        switch self {
        case .elegant: "Warm white"
        case .twinkle: "Warm white with slow twinkles"
        case .festive: "Red, green and gold"
        case .calm: "Steady warm white"
        case .off: "All lights off"
        }
    }
    var symbol: String {
        switch self {
        case .elegant: "lightbulb.fill"; case .twinkle: "sparkle"; case .festive: "gift.fill"
        case .calm: "moon.fill"; case .off: "power"
        }
    }
    /// Text on a selected look tile: black reads on the bright tints, white on Off's grey.
    var onText: Color { self == .off ? .white : .black }

    var tint: Color {
        switch self {
        case .elegant: Color(red: 1.0, green: 0.78, blue: 0.45)
        case .twinkle: Color(red: 1.0, green: 0.88, blue: 0.6)
        case .festive: Color(red: 0.86, green: 0.18, blue: 0.2)
        case .calm: Color(red: 0.98, green: 0.7, blue: 0.4)
        case .off: Color.gray
        }
    }
}

struct ResultReply: Codable { var result: String?; var message: String?; var dryRun: Bool? }
