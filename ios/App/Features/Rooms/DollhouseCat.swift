import Foundation
import SceneKit

/// Life for the living-room cat (`pet_cat`).
///
/// Pose groups from Model Generator win when they are in the file: `pet_cat_pose_sleep`,
/// `pet_cat_pose_sit`, `pet_cat_pose_stretch`, and `pet_cat_pose_walk` / `pet_cat_pose_walk_*`.
/// The app fades those on and off. Until they exist, the same cues drive the mesh that is
/// already in the room: a slow breath, a tail flick, an ear twitch, and a short walk.
final class DollhouseCat {
    private var cat: SCNNode?
    private var body: SCNNode?
    private var tail: SCNNode?
    private var ears: [SCNNode] = []
    private var bodyScale: SCNVector3?
    private var poses: [String: SCNNode] = [:]
    private var procedural: [SCNNode] = []
    private var walkTimer: Timer?
    private var token = 0

    func prepare(stage: DollhouseStage) {
        guard let cat = stage.node(named: "pet_cat") else { return }
        self.cat = cat
        body = stage.node(named: "pet_cat_body")
        tail = stage.node(named: "pet_cat_tail")
        ears = ["pet_cat_ear_left", "pet_cat_ear_inner_left", "pet_cat_ear_right", "pet_cat_ear_inner_right"]
            .compactMap { stage.node(named: $0) }
        if let body, bodyScale == nil { bodyScale = body.scale }
        poses = [:]
        stage.eachNamedNode { name, node in
            guard name.hasPrefix("pet_cat_pose_") else { return }
            node.isHidden = false
            poses[name] = node
        }
        procedural = cat.childNodes.filter { node in
            guard let name = node.name else { return true }
            return !name.hasPrefix("pet_cat_pose_")
        }
        if !poses.isEmpty {
            for node in poses.values { node.opacity = 0 }
        }
    }

    func stop() {
        token += 1
        walkTimer?.invalidate()
        walkTimer = nil
        cat?.removeAction(forKey: "life-walk")
        cat?.removeAction(forKey: "life-after-stretch")
        body?.removeAction(forKey: "life-breath")
        tail?.removeAction(forKey: "life-tail")
        ears.forEach { $0.removeAction(forKey: "life-ear") }
        if let body, let bodyScale { body.scale = bodyScale }
    }

    /// `motion` is false under Reduce Motion. The cat still ends on the right spot, already curled up.
    func play(cue: DollhouseCatCue?, spots: [String: DollhouseSpot], motion: Bool) {
        guard let cat else { return }
        token += 1
        let ticket = token
        stopMoving()
        let activity = DollhouseCatActivity(raw: cue?.activity ?? "sleep")
        let route = route(for: activity, cue: cue, spots: spots)
        guard motion else {
            if let last = route.last {
                place(cat, at: last.point, yaw: last.yaw)
            }
            show(activity == .sit ? .sit : .sleep, duration: 0)
            return
        }
        guard route.count > 1 || needsWalk(cat, to: route.last) else {
            if let last = route.last { place(cat, at: last.point, yaw: last.yaw) }
            begin(activity, ticket: ticket)
            return
        }
        if activity == .walk { showWalkPoses() } else { show(.sleep, duration: 0.4) }
        cat.runAction(walk(route), forKey: "life-walk") { [weak self] in
            DispatchQueue.main.async {
                guard let self, self.token == ticket else { return }
                self.walkTimer?.invalidate()
                self.begin(activity == .walk ? .sleep : activity, ticket: ticket)
            }
        }
    }

    // MARK: route

    private struct Stop {
        var point: SCNVector3
        var yaw: Float?
    }

    private func route(for activity: DollhouseCatActivity, cue: DollhouseCatCue?, spots: [String: DollhouseSpot]) -> [Stop] {
        let names: [String]
        switch activity {
        case .walk:
            let path = cue?.path ?? []
            names = path.isEmpty ? [cue?.spot].compactMap { $0 } : path
        case .sleep, .sit, .stretch:
            names = [cue?.spot].compactMap { $0 }
        }
        return names.compactMap { name in
            guard let spot = spots[name], spot.position.count == 3 else { return nil }
            let point = SCNVector3(Float(spot.position[0]), Float(spot.position[1]), Float(spot.position[2]))
            return Stop(point: point, yaw: spot.yaw.map { Float($0) })
        }
    }

    private func needsWalk(_ cat: SCNNode, to stop: Stop?) -> Bool {
        guard let stop else { return false }
        let dx = cat.position.x - stop.point.x
        let dy = cat.position.y - stop.point.y
        let dz = cat.position.z - stop.point.z
        return (dx * dx + dy * dy + dz * dz) > 0.006
    }

    private func walk(_ route: [Stop]) -> SCNAction {
        var steps: [SCNAction] = []
        var here = cat?.position ?? route[0].point
        for stop in route {
            let dx = stop.point.x - here.x
            let dy = stop.point.y - here.y
            let dz = stop.point.z - here.z
            let distance = (dx * dx + dy * dy + dz * dz).squareRoot()
            if distance > 0.02 {
                let facing = SCNAction.rotateTo(x: 0, y: CGFloat(yaw(toward: dx, dz: dz)), z: 0, duration: 0.6, usesShortestUnitArc: true)
                let seconds = TimeInterval(distance) / 0.22
                let move = SCNAction.move(to: stop.point, duration: max(1.3, seconds))
                move.timingMode = .easeInEaseOut
                steps.append(.sequence([facing, move]))
            }
            if let yaw = stop.yaw {
                steps.append(.rotateTo(x: 0, y: CGFloat(yaw), z: 0, duration: 0.5, usesShortestUnitArc: true))
            }
            here = stop.point
        }
        if steps.isEmpty { return .wait(duration: 0.01) }
        return .sequence(steps)
    }

    /// The head is the group's local +X, so this yaw points that axis along the step.
    private func yaw(toward dx: Float, dz: Float) -> Float {
        atan2(-dz, dx)
    }

    private func place(_ cat: SCNNode, at point: SCNVector3, yaw: Float?) {
        cat.position = point
        guard let yaw else { return }
        var euler = cat.eulerAngles
        euler.y = yaw
        cat.eulerAngles = euler
    }

    // MARK: poses and procedural idle

    private enum Pose {
        case sleep, sit, stretch, walk
    }

    private func begin(_ activity: DollhouseCatActivity, ticket: Int) {
        switch activity {
        case .sleep, .walk:
            show(.sleep, duration: 0.6)
            breathe(period: 3.6, amount: 0.045)
            fidget(earEvery: 7.5, tailEvery: 5.5)
        case .sit:
            show(.sit, duration: 0.6)
            breathe(period: 4.2, amount: 0.03)
            fidget(earEvery: 3.2, tailEvery: 6.5)
        case .stretch:
            show(.stretch, duration: 0.45)
            if poses["pet_cat_pose_stretch"] == nil { proceduralStretch() }
            cat?.removeAction(forKey: "life-after-stretch")
            cat?.runAction(.wait(duration: 1.8), forKey: "life-after-stretch") { [weak self] in
                DispatchQueue.main.async {
                    guard let self, self.token == ticket else { return }
                    self.show(.sleep, duration: 0.6)
                    self.breathe(period: 3.6, amount: 0.045)
                    self.fidget(earEvery: 7.5, tailEvery: 5.5)
                }
            }
        }
    }

    private func show(_ pose: Pose, duration: TimeInterval) {
        walkTimer?.invalidate()
        walkTimer = nil
        let name: String?
        switch pose {
        case .sleep: name = "pet_cat_pose_sleep"
        case .sit: name = "pet_cat_pose_sit"
        case .stretch: name = "pet_cat_pose_stretch"
        case .walk: name = walkPoseNames.first
        }
        if let name, poses[name] != nil {
            crossfade(showing: [name], duration: duration)
        } else {
            crossfade(showing: [], duration: duration)
        }
    }

    private func showWalkPoses() {
        let names = walkPoseNames
        guard !names.isEmpty else {
            crossfade(showing: [], duration: 0.35)
            fasterTail()
            return
        }
        var index = 0
        crossfade(showing: [names[0]], duration: 0.35)
        guard names.count > 1 else { return }
        walkTimer = Timer.scheduledTimer(withTimeInterval: 0.42, repeats: true) { [weak self] _ in
            guard let self else { return }
            index = (index + 1) % names.count
            self.crossfade(showing: [names[index]], duration: 0.28)
        }
    }

    private var walkPoseNames: [String] {
        poses.keys.filter { $0 == "pet_cat_pose_walk" || $0.hasPrefix("pet_cat_pose_walk_") }.sorted()
    }

    /// An empty `showing` list fades back to the procedural mesh.
    private func crossfade(showing: [String], duration: TimeInterval) {
        guard !poses.isEmpty else { return }
        let wanted = Set(showing)
        SCNTransaction.begin()
        SCNTransaction.animationDuration = duration
        let posed = !wanted.isEmpty
        for node in procedural { node.opacity = posed ? 0 : 1 }
        for (name, node) in poses { node.opacity = wanted.contains(name) ? 1 : 0 }
        SCNTransaction.commit()
    }

    private func breathe(period: TimeInterval, amount: Float) {
        guard let body, let base = bodyScale else { return }
        body.removeAction(forKey: "life-breath")
        let action = SCNAction.customAction(duration: period) { node, elapsed in
            let wave = sin(Float(elapsed / CGFloat(period)) * Float.pi)
            node.scale = SCNVector3(base.x, base.y * (1 + amount * wave), base.z)
        }
        body.runAction(.repeatForever(action), forKey: "life-breath")
    }

    private func proceduralStretch() {
        guard let body, let base = bodyScale else { return }
        body.removeAction(forKey: "life-breath")
        let action = SCNAction.customAction(duration: 1.6) { node, elapsed in
            let wave = sin((Float(elapsed) / 1.6) * Float.pi)
            node.scale = SCNVector3(base.x * (1 + 0.08 * wave), base.y * (1 + 0.12 * wave), base.z)
        }
        body.runAction(action, forKey: "life-breath")
    }

    private func fidget(earEvery: TimeInterval, tailEvery: TimeInterval) {
        guard let tail else { earTwitch(every: earEvery); return }
        tail.removeAction(forKey: "life-tail")
        let flick = SCNAction.sequence([
            .wait(duration: tailEvery),
            .rotateBy(x: 0, y: 0.45, z: 0, duration: 0.16),
            .rotateBy(x: 0, y: -0.45, z: 0, duration: 0.28)
        ])
        tail.runAction(.repeatForever(flick), forKey: "life-tail")
        earTwitch(every: earEvery)
    }

    private func fasterTail() {
        guard let tail else { return }
        tail.removeAction(forKey: "life-tail")
        let flick = SCNAction.sequence([
            .rotateBy(x: 0.15, y: 0.2, z: 0, duration: 0.18),
            .rotateBy(x: -0.15, y: -0.2, z: 0, duration: 0.18)
        ])
        tail.runAction(.repeatForever(flick), forKey: "life-tail")
    }

    private func earTwitch(every: TimeInterval) {
        for ear in ears {
            ear.removeAction(forKey: "life-ear")
            let twitch = SCNAction.sequence([
                .wait(duration: every + Double.random(in: 0...1.4)),
                .rotateBy(x: 0, y: 0, z: 0.22, duration: 0.08),
                .rotateBy(x: 0, y: 0, z: -0.22, duration: 0.14)
            ])
            ear.runAction(.repeatForever(twitch), forKey: "life-ear")
        }
    }

    private func stopMoving() {
        walkTimer?.invalidate()
        walkTimer = nil
        cat?.removeAction(forKey: "life-walk")
        cat?.removeAction(forKey: "life-after-stretch")
        body?.removeAction(forKey: "life-breath")
        tail?.removeAction(forKey: "life-tail")
        ears.forEach { $0.removeAction(forKey: "life-ear") }
        if let body, let bodyScale { body.scale = bodyScale }
    }
}

enum DollhouseCatActivity: String {
    case sleep, sit, walk, stretch

    init(raw: String) {
        switch raw {
        case "sleep": self = .sleep
        case "sit": self = .sit
        case "walk": self = .walk
        case "stretch": self = .stretch
        default:
            self = .sleep
        }
    }
}
