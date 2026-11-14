import Foundation
import SceneKit
import UIKit

/// What the dollhouse controller exposes to room life. Life never calls the hub.
protocol DollhouseStage: AnyObject {
    func node(named name: String) -> SCNNode?
    func lamp(named name: String) -> SCNNode?
    func eachNamedNode(_ body: (String, SCNNode) -> Void)
    func showLevels(_ levels: [String: Double], duration: TimeInterval, completion: (() -> Void)?)
    func setDaylight(_ daylight: DollhouseDaylight, animated: Bool)
    func setCameraMotion(enabled: Bool)
    func setSimulation(running: Bool, animated: Bool)
}

enum DollhouseLifeTiming {
    static let hub: TimeInterval = 0.6
    static let scene: TimeInterval = 2
    static let gapMin: TimeInterval = 45
    static let gapMax: TimeInterval = 120
}

enum DollhouseDayPart {
    case morning, midday, dusk, night

    var key: String {
        switch self {
        case .morning: "morning"
        case .midday: "midday"
        case .dusk: "dusk"
        case .night: "night"
        }
    }
}

/// Studio colours for one part of the day. Intensities of the key and ambient stay at the
/// levels already accepted for the dollhouse; only the hue, and a small window fill, change.
struct DollhouseDaylight {
    var ambient: UIColor
    var key: UIColor
    var window: UIColor
    var windowIntensity: CGFloat
    var envTop: UInt32
    var envBottom: UInt32

    static func look(_ part: DollhouseDayPart) -> DollhouseDaylight {
        switch part {
        case .morning:
            DollhouseDaylight(ambient: UIColor(hex: 0xFFE4C4), key: UIColor(hex: 0xFFD0A6),
                              window: UIColor(hex: 0xFFD8B0), windowIntensity: 34,
                              envTop: 0xFFE6C8, envBottom: 0xC4A07A)
        case .midday:
            DollhouseDaylight(ambient: UIColor(hex: 0xFFF3E3), key: UIColor(hex: 0xFFF1DD),
                              window: UIColor(hex: 0xF4F0E6), windowIntensity: 22,
                              envTop: 0xFFF3E3, envBottom: 0x5B4636)
        case .dusk:
            DollhouseDaylight(ambient: UIColor(hex: 0xFFD2A8), key: UIColor(hex: 0xFFC08A),
                              window: UIColor(hex: 0xFFBE7A), windowIntensity: 40,
                              envTop: 0xFFC89A, envBottom: 0x8A5A3A)
        case .night:
            DollhouseDaylight(ambient: UIColor(hex: 0xE7ECF6), key: UIColor(hex: 0xD5DDEC),
                              window: UIColor(hex: 0x9BB6E0), windowIntensity: 46,
                              envTop: 0x1C2433, envBottom: 0x0E1218)
        }
    }
}

struct DollhouseSun {
    var sunrise: Date?
    var sunset: Date?

    func part(at date: Date = Date()) -> DollhouseDayPart {
        let span = resolved(on: date)
        if date < span.rise || date >= span.set.addingTimeInterval(45 * 60) { return .night }
        if date < span.rise.addingTimeInterval(3 * 3600) { return .morning }
        if date < span.set.addingTimeInterval(-90 * 60) { return .midday }
        return .dusk
    }

    /// Hub sunrise and sunset when they belong to this local day. Otherwise a mid-latitude estimate.
    private func resolved(on date: Date) -> (rise: Date, set: Date) {
        let calendar = Calendar.current
        if let sunrise, let sunset, sunset > sunrise, calendar.isDate(sunrise, inSameDayAs: date) {
            return (sunrise, sunset)
        }
        return Self.approximate(on: date, calendar: calendar)
    }

    /// June solstice (day 172) is the long day. December is the short one. Northern hemisphere.
    private static func approximate(on date: Date, calendar: Calendar) -> (rise: Date, set: Date) {
        let day = Double(calendar.ordinality(of: .day, in: .year, for: date) ?? 180)
        let season = cos((day - 172) / 365 * 2 * Double.pi)
        let riseHour = 6.3 - 1.4 * season
        let setHour = 18.4 + 2.0 * season
        return (clock(riseHour, on: date, calendar: calendar), clock(setHour, on: date, calendar: calendar))
    }

    private static func clock(_ hour: Double, on date: Date, calendar: Calendar) -> Date {
        var parts = calendar.dateComponents([.year, .month, .day], from: date)
        let whole = Int(hour)
        parts.hour = whole
        parts.minute = Int((hour - Double(whole)) * 60)
        parts.second = 0
        return calendar.date(from: parts) ?? date
    }
}

struct DollhouseLifeScript: Decodable {
    var spots: [String: DollhouseSpot]
    var scenes: [DollhouseLifeScene]

    init(spots: [String: DollhouseSpot], scenes: [DollhouseLifeScene]) {
        self.spots = spots
        self.scenes = scenes
    }

    private enum CodingKeys: String, CodingKey { case spots, scenes }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        spots = try c.decodeIfPresent([String: DollhouseSpot].self, forKey: .spots) ?? [:]
        scenes = try c.decodeIfPresent([DollhouseLifeScene].self, forKey: .scenes) ?? []
    }
}

struct DollhouseSpot: Decodable {
    var position: [Double]
    var yaw: Double?
}

struct DollhouseCatCue: Decodable {
    var activity: String
    var spot: String?
    var path: [String]?
}

struct DollhouseLifeScene: Decodable {
    var id: String
    var title: String
    var weights: [String: Double]
    var devices: [String: Double]
    var props: [String]
    var steam: [String]
    var whileOn: [String]
    var cat: DollhouseCatCue?

    private enum CodingKeys: String, CodingKey {
        case id, title, weights, devices, props, steam, whileOn, cat
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        title = try c.decodeIfPresent(String.self, forKey: .title) ?? id
        weights = try c.decodeIfPresent([String: Double].self, forKey: .weights) ?? [:]
        devices = try c.decodeIfPresent([String: Double].self, forKey: .devices) ?? [:]
        props = try c.decodeIfPresent([String].self, forKey: .props) ?? []
        steam = try c.decodeIfPresent([String].self, forKey: .steam) ?? []
        whileOn = try c.decodeIfPresent([String].self, forKey: .whileOn) ?? []
        cat = try c.decodeIfPresent(DollhouseCatCue.self, forKey: .cat)
    }
}

/// Picks a life scene, crossfades it, and keeps real hub levels in front of the script.
/// A scene only changes the model. It does not call the hub.
final class DollhouseLife {
    let room: DollhouseRoom
    private weak var stage: DollhouseStage?
    private let script: DollhouseLifeScript
    private let ambient: DollhouseAmbient
    private let cat = DollhouseCat()
    private var sun = DollhouseSun()
    private var real: [String: Double] = [:]
    private var owned: Set<String> = []
    private var current: DollhouseLifeScene?
    private var running = false
    private var reduceMotion = false
    private var started = false
    private var timer: Timer?
    private var dayTimer: Timer?
    private var lastPart: DollhouseDayPart?
    private var applyToken = 0

    init(room: DollhouseRoom, stage: DollhouseStage) {
        self.room = room
        self.stage = stage
        script = room.life ?? DollhouseLifeScript(spots: [:], scenes: [])
        ambient = DollhouseAmbient(room: room)
    }

    deinit {
        timer?.invalidate()
        dayTimer?.invalidate()
    }

    private var motionAllowed: Bool { running && !reduceMotion }

    func start(real: [String: Double], owned: Set<String>, sunrise: Date?, sunset: Date?) {
        guard !started, stage != nil else { return }
        started = true
        self.real = real
        self.owned = owned
        sun.sunrise = sunrise
        sun.sunset = sunset
        if let stage {
            cat.prepare(stage: stage)
            ambient.prepare(stage: stage)
        }
        apply(pick(excluding: nil), duration: 0, moveCat: true)
        if running { armTimers() }
    }

    func stop() {
        timer?.invalidate()
        dayTimer?.invalidate()
        timer = nil
        dayTimer = nil
        cat.stop()
    }

    /// Hub brightness changed. Owned devices replace the scene. A device that just turned on
    /// can pull its scene forward (the washer running, the TV on).
    func noteHub(real: [String: Double], owned: Set<String>) {
        guard started else {
            self.real = real
            self.owned = owned
            return
        }
        let before = lockedIDs()
        self.real = real
        self.owned = owned
        if lockedIDs() != before {
            let next = pick(excluding: nil)
            let same = next?.id == current?.id
            apply(next, duration: same ? DollhouseLifeTiming.hub : DollhouseLifeTiming.scene, moveCat: !same)
            if !same { armSceneTimer() }
        } else {
            apply(current, duration: DollhouseLifeTiming.hub, moveCat: false)
        }
    }

    func noteSun(sunrise: Date?, sunset: Date?) {
        sun.sunrise = sunrise
        sun.sunset = sunset
        guard started else { return }
        refreshDaylight(animated: true)
    }

    func setRunning(_ on: Bool) {
        running = on
        guard started, let stage else { return }
        stage.setSimulation(running: on, animated: motionAllowed)
        guard on else {
            timer?.invalidate()
            dayTimer?.invalidate()
            timer = nil
            dayTimer = nil
            stage.setCameraMotion(enabled: false)
            ambient.sync(stage: stage, levels: merged(current), steam: current?.steam ?? [], motion: false, part: sun.part())
            return
        }
        armTimers()
        stage.setCameraMotion(enabled: motionAllowed)
        if let current, motionAllowed {
            cat.play(cue: current.cat, spots: script.spots, motion: true)
        }
        ambient.sync(stage: stage, levels: merged(current), steam: current?.steam ?? [], motion: motionAllowed, part: sun.part())
    }

    func setReduceMotion(_ on: Bool) {
        reduceMotion = on
        guard started, let stage else { return }
        stage.setSimulation(running: running, animated: motionAllowed)
        stage.setCameraMotion(enabled: running && motionAllowed)
        guard running else { return }
        if let current {
            cat.play(cue: current.cat, spots: script.spots, motion: motionAllowed)
        }
        ambient.sync(stage: stage, levels: merged(current), steam: current?.steam ?? [], motion: motionAllowed, part: sun.part())
    }

    // MARK: scenes

    private func apply(_ scene: DollhouseLifeScene?, duration: TimeInterval, moveCat: Bool) {
        guard let stage else { return }
        current = scene
        let levels = merged(scene)
        let part = sun.part()
        lastPart = part
        applyToken += 1
        let token = applyToken
        stage.setDaylight(.look(part), animated: duration > 0)
        ambient.setProps(names: scene?.props ?? [], duration: duration)
        if moveCat {
            cat.play(cue: scene?.cat, spots: script.spots, motion: motionAllowed)
        }
        stage.setCameraMotion(enabled: running && motionAllowed)
        if duration > 0 { ambient.suspendGlowLoops(stage: stage) }
        let steam = scene?.steam ?? []
        stage.showLevels(levels, duration: duration) { [weak self] in
            guard let self, self.applyToken == token, let stage = self.stage else { return }
            self.ambient.sync(stage: stage, levels: levels, steam: steam, motion: self.motionAllowed, part: part)
        }
    }

    /// Owned devices keep the hub's 0...1 level. Everything else may take the scene's level.
    private func merged(_ scene: DollhouseLifeScene?) -> [String: Double] {
        var levels: [String: Double] = [:]
        for device in room.devices.devices {
            if owned.contains(device.name) {
                levels[device.name] = min(1, max(0, real[device.name] ?? 0))
            } else if let scripted = scene?.devices[device.name] {
                levels[device.name] = min(1, max(0, scripted))
            } else {
                levels[device.name] = 0
            }
        }
        return levels
    }

    private func locked(_ scene: DollhouseLifeScene) -> Bool {
        for name in scene.whileOn where owned.contains(name) {
            if (real[name] ?? 0) > 0.02 { return true }
        }
        return false
    }

    private func lockedIDs() -> Set<String> {
        Set(script.scenes.filter { self.locked($0) }.map(\.id))
    }

    private func pick(excluding: String?) -> DollhouseLifeScene? {
        let forced = script.scenes.filter { self.locked($0) }
        var pool = forced.isEmpty ? script.scenes : forced
        if pool.count > 1, let excluding {
            let trimmed = pool.filter { $0.id != excluding }
            if !trimmed.isEmpty { pool = trimmed }
        }
        guard !pool.isEmpty else { return nil }
        let forcing = !forced.isEmpty
        let weights = pool.map { weight($0, forcing: forcing) }
        let total = weights.reduce(0, +)
        if total <= 0 { return pool.randomElement() }
        var ticket = Double.random(in: 0..<total)
        var chosen = pool[0]
        for (scene, w) in zip(pool, weights) where w > 0 {
            chosen = scene
            ticket -= w
            if ticket < 0 { return scene }
        }
        return chosen
    }

    private func weight(_ scene: DollhouseLifeScene, forcing: Bool) -> Double {
        let raw = scene.weights[sun.part().key] ?? 1
        if forcing { return max(raw, 1) }
        return max(0, raw)
    }

    private func advance() {
        refreshDaylight(animated: true)
        let next = pick(excluding: current?.id)
        if next?.id != current?.id {
            apply(next, duration: DollhouseLifeTiming.scene, moveCat: true)
        }
        armSceneTimer()
    }

    private func refreshDaylight(animated: Bool) {
        let part = sun.part()
        guard part != lastPart else { return }
        lastPart = part
        stage?.setDaylight(.look(part), animated: animated)
        guard let stage else { return }
        ambient.sync(stage: stage, levels: merged(current), steam: current?.steam ?? [], motion: motionAllowed, part: part)
    }

    private func armTimers() {
        armSceneTimer()
        dayTimer?.invalidate()
        dayTimer = Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in
            self?.refreshDaylight(animated: true)
        }
    }

    private func armSceneTimer() {
        timer?.invalidate()
        guard running, !script.scenes.isEmpty else { timer = nil; return }
        let wait = TimeInterval.random(in: DollhouseLifeTiming.gapMin...DollhouseLifeTiming.gapMax)
        timer = Timer.scheduledTimer(withTimeInterval: wait, repeats: false) { [weak self] _ in
            self?.advance()
        }
    }
}
