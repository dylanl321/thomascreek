import SceneKit
import UIKit

/// Small motions on nodes the room already has. A missing name is skipped.
/// Nothing here calls the hub. Looping work is torn down when motion is off
/// (the view left the screen, the app backgrounded, Low Power Mode, or Reduce Motion).
final class DollhouseAmbient {
    let room: DollhouseRoom
    private var props: [SCNNode] = []
    private var propGeneration = 0
    private var particles: [String: (SCNNode, SCNParticleSystem)] = [:]
    private var flames: Set<String> = []
    private var spins: Set<String> = []
    private var curtains: Set<String> = []
    private var screens: Set<String> = []
    private var didScanCurtains = false
    private let motion = DollhouseMotion()

    init(room: DollhouseRoom) { self.room = room }

    func prepare(stage: DollhouseStage) {
        props = []
        stage.eachNamedNode { name, node in
            guard name.hasPrefix("prop_") else { return }
            node.opacity = 0
            node.isHidden = true
            props.append(node)
        }
    }

    func suspendGlowLoops(stage: DollhouseStage) {
        stopFlames(stage)
        flames = []
        for name in screens {
            drawable(stage, name)?.removeAction(forKey: "life-screen")
        }
        screens = []
    }

    func stop(stage: DollhouseStage) {
        clearMotion(stage)
    }

    func setProps(names: [String], duration: TimeInterval) {
        propGeneration += 1
        let generation = propGeneration
        let show = Set(names)
        var hiding: [SCNNode] = []
        SCNTransaction.begin()
        SCNTransaction.animationDuration = duration
        for node in props {
            let on = show.contains(node.name ?? "")
            if on { node.isHidden = false }
            node.opacity = on ? 1 : 0
            if !on { hiding.append(node) }
        }
        SCNTransaction.completionBlock = { [weak self] in
            guard let self, self.propGeneration == generation else { return }
            hiding.forEach { $0.isHidden = true }
        }
        SCNTransaction.commit()
    }

    /// Called after device materials have finished their fade, so flame brightness matches the new level.
    func sync(stage: DollhouseStage, levels: [String: Double], steam: [String], motion: Bool, part: DollhouseDayPart) {
        guard motion else {
            clearMotion(stage)
            return
        }
        syncFlames(stage, levels: levels)
        syncSpins(stage, levels: levels)
        syncCurtains(stage)
        syncScreens(stage, levels: levels)
        syncSteam(stage, names: steam)
        syncDust(stage, part: part)
        syncSparkles(stage, part: part)
    }

    // MARK: flames, spin, curtains, screens

    private func syncFlames(_ stage: DollhouseStage, levels: [String: Double]) {
        let on = room.devices.devices.contains { $0.type == .fireplace && (levels[$0.name] ?? 0) > 0.02 }
        guard on else {
            if !flames.isEmpty { stopFlames(stage); flames = [] }
            return
        }
        var wanted: [String: CGFloat] = [:]
        stage.eachNamedNode { name, node in
            guard name.hasPrefix("fire_flame") else { return }
            let shaped = self.shaped(node)
            guard shaped.geometry != nil else { return }
            let base = shaped.geometry?.firstMaterial?.emission.intensity ?? 1
            wanted[name] = base
        }
        if let lamp = stage.lamp(named: "light_fireplace"), let base = lamp.light?.intensity, base > 1 {
            wanted["light_fireplace"] = base
        }
        guard Set(wanted.keys) != flames else { return }
        stopFlames(stage)
        var index = 0
        for (name, base) in wanted {
            let node = name == "light_fireplace" ? stage.lamp(named: name) : drawable(stage, name)
            guard let node else { continue }
            pulse(node, base: base, amount: name == "light_fireplace" ? 0.22 : 0.34, duration: 0.55 + Double(index) * 0.11)
            index += 1
        }
        flames = Set(wanted.keys)
    }

    private func pulse(_ node: SCNNode, base: CGFloat, amount: CGFloat, duration: TimeInterval) {
        node.removeAction(forKey: "life-flicker")
        let action = SCNAction.customAction(duration: duration) { node, elapsed in
            let wave = sin((elapsed / CGFloat(duration)) * .pi * 2)
            let value = base * (1 - amount + amount * (0.5 + 0.5 * wave))
            if node.light != nil {
                node.light?.intensity = value
            } else {
                node.geometry?.materials.forEach { $0.emission.intensity = value }
            }
        }
        node.runAction(.repeatForever(action), forKey: "life-flicker")
    }

    private func stopFlames(_ stage: DollhouseStage) {
        for name in flames {
            let node = name == "light_fireplace" ? stage.lamp(named: name) : drawable(stage, name)
            node?.removeAction(forKey: "life-flicker")
        }
    }

    /// Only these exact leaves ever spin. Each pivots on itself (fan hub, drum axle), so a turn can't
    /// carry the room with it. Drums turn about their local +Z, which points out of the door.
    private func syncSpins(_ stage: DollhouseStage, levels: [String: Double]) {
        var wanted: [String: TimeInterval] = [:]
        for device in room.devices.devices {
            let level = levels[device.name] ?? 0
            guard level > 0.02 else { continue }
            if device.type == .fan {
                wanted["ceiling_fan_blades"] = level > 0.7 ? 1.4 : 2.6
            }
            if device.type == .appliance, Self.drums.contains(device.name + "_drum") {
                wanted[device.name + "_drum"] = 1.8
            }
        }
        guard Set(wanted.keys) != spins else { return }
        for name in spins where wanted[name] == nil {
            if let node = stage.node(named: name) { motion.restore(node, key: "life-spin") }
        }
        var running: Set<String> = []
        for (name, duration) in wanted {
            guard let node = stage.node(named: name), node.isSmallSelfPivot() else { continue }
            running.insert(name)
            guard !spins.contains(name) else { continue }
            let axis = name == "ceiling_fan_blades" ? SCNVector3(0, 1, 0) : SCNVector3(0, 0, 1)
            motion.loop(node, key: "life-spin", axis: axis, period: duration) { phase in phase * 2 * .pi }
        }
        spins = running
    }

    static let drums: Set<String> = ["washer_drum", "dryer_drum"]

    /// The curtain meshes, by exact name. Each pivots at its heading (top rod) and hangs down -Y,
    /// so a small turn about local X (the rod) is a sway, never a slide across the room.
    static let curtainNames = [
        "fireplace_curtain_1", "fireplace_curtain_2", "screen_curtain_1", "screen_curtain_2",
        "rear_black_curtain_cut"
    ]

    private func syncCurtains(_ stage: DollhouseStage) {
        guard !didScanCurtains else { return }
        didScanCurtains = true
        for (index, name) in Self.curtainNames.enumerated() {
            guard let node = stage.node(named: name), node.isSmallSelfPivot(maxRadius: 2.5) else { continue }
            let period = 6.4 + Double(index) * 0.7
            motion.loop(node, key: "life-sway", axis: SCNVector3(1, 0, 0), period: period) { phase in
                0.03 * sin(phase * 2 * .pi)
            }
            curtains.insert(name)
        }
    }

    private func syncScreens(_ stage: DollhouseStage, levels: [String: Double]) {
        var wanted: Set<String> = []
        if (levels["tv"] ?? 0) > 0.02 { wanted.insert("tv_screen") }
        if (levels["projector"] ?? 0) > 0.02 { wanted.insert("screen_surface") }
        guard wanted != screens else { return }
        for name in screens where !wanted.contains(name) {
            guard let node = drawable(stage, name) else { continue }
            node.removeAction(forKey: "life-screen")
            node.geometry?.materials.forEach {
                $0.emission.contents = UIColor.black
                $0.emission.intensity = 0
            }
        }
        for name in wanted where !screens.contains(name) {
            guard let node = drawable(stage, name) else { continue }
            isolate(node)
            let palette: [UIColor] = [
                UIColor(hex: 0x1C3C6E), UIColor(hex: 0x8C3A2A), UIColor(hex: 0x1E4C48), UIColor(hex: 0x3A3058)
            ]
            let action = SCNAction.customAction(duration: 9) { node, elapsed in
                let span = palette.count
                let t = (Float(elapsed) / 9) * Float(span)
                let index = Int(t) % span
                let next = (index + 1) % span
                let mix = CGFloat(t - Float(Int(t)))
                node.geometry?.materials.forEach { material in
                    material.emission.contents = palette[index].mix(palette[next], amount: mix)
                    material.emission.intensity = 0.85
                }
            }
            node.runAction(.repeatForever(action), forKey: "life-screen")
        }
        screens = wanted
    }

    /// USD prims are transforms. The drawable is often a child named mesh.
    private func shaped(_ node: SCNNode) -> SCNNode {
        if node.geometry != nil { return node }
        return node.childNodes.first { $0.geometry != nil } ?? node
    }

    private func drawable(_ stage: DollhouseStage, _ name: String) -> SCNNode? {
        guard let node = stage.node(named: name) else { return nil }
        return shaped(node)
    }

    /// SceneKit shares one material across meshes. Copy before the screen starts tinting it.
    private func isolate(_ node: SCNNode) {
        guard let geometry = node.geometry else { return }
        let copy = geometry.copy() as? SCNGeometry ?? geometry
        copy.materials = geometry.materials.map { ($0.copy() as? SCNMaterial) ?? $0 }
        node.geometry = copy
    }

    // MARK: particles

    private func syncSteam(_ stage: DollhouseStage, names: [String]) {
        var keep: Set<String> = []
        for name in names {
            let key = "steam:" + name
            guard let node = stage.node(named: name), placed(node) else { continue }
            keep.insert(key)
            if particles[key] == nil {
                // Props pivot on the surface under them, so the steam starts at the rim instead.
                let (lo, hi) = node.contentBounds() ?? (SCNVector3Zero, SCNVector3Zero)
                let emitter = SCNNode()
                emitter.name = Self.emitterName
                emitter.position = SCNVector3((lo.x + hi.x) / 2, hi.y, (lo.z + hi.z) / 2)
                node.addChildNode(emitter)
                particles[key] = (emitter, emitter.add(system: steam()))
            }
        }
        dropParticles(prefix: "steam:", keep: keep)
    }

    private func syncDust(_ stage: DollhouseStage, part: DollhouseDayPart) {
        guard part != .night else {
            dropParticles(prefix: "dust:", keep: [])
            return
        }
        var keep: Set<String> = []
        stage.eachNamedNode { name, node in
            let key = name.lowercased()
            let window = key.contains("window") && key.contains("glass")
            guard (window || key == "skylight_diffuser"), placed(node) else { return }
            let id = "dust:" + name
            keep.insert(id)
            if particles[id] == nil {
                particles[id] = (node, node.add(system: dust()))
            }
        }
        dropParticles(prefix: "dust:", keep: keep)
    }

    private func syncSparkles(_ stage: DollhouseStage, part: DollhouseDayPart) {
        guard part == .night else {
            dropParticles(prefix: "spark:", keep: [])
            return
        }
        var keep: Set<String> = []
        for name in Self.sparkNodes {
            guard let node = stage.node(named: name), placed(node) else { continue }
            let id = "spark:" + name
            keep.insert(id)
            if particles[id] == nil {
                particles[id] = (node, node.add(system: sparkle()))
            }
        }
        dropParticles(prefix: "spark:", keep: keep)
    }

    /// Groups exported at the room origin would puff steam in the corner. Meshes sit on the object.
    private func placed(_ node: SCNNode) -> Bool {
        let p = node.worldPosition
        return abs(p.x) + abs(p.y) + abs(p.z) > 0.25
    }

    private func dropParticles(prefix: String, keep: Set<String>) {
        for (key, pair) in particles where key.hasPrefix(prefix) && !keep.contains(key) {
            Self.detach(pair)
            particles.removeValue(forKey: key)
        }
    }

    private func clearMotion(_ stage: DollhouseStage) {
        stopFlames(stage)
        flames = []
        for name in spins {
            if let node = stage.node(named: name) { motion.restore(node, key: "life-spin") }
        }
        spins = []
        for name in curtains {
            if let node = stage.node(named: name) { motion.restore(node, key: "life-sway") }
        }
        curtains = []
        didScanCurtains = false
        for name in screens {
            drawable(stage, name)?.removeAction(forKey: "life-screen")
        }
        screens = []
        for (_, pair) in particles { Self.detach(pair) }
        particles = [:]
    }

    private static let emitterName = "life-steam-emitter"

    private static func detach(_ pair: (SCNNode, SCNParticleSystem)) {
        pair.0.removeParticleSystem(pair.1)
        if pair.0.name == emitterName { pair.0.removeFromParentNode() }
    }

    private func steam() -> SCNParticleSystem {
        let system = puff(rate: 7, life: 1.5, size: 0.018, speed: 0.045, angle: 14)
        system.particleColor = UIColor(white: 1, alpha: 0.42)
        system.blendMode = .alpha
        system.acceleration = SCNVector3(0, 0.07, 0)
        return system
    }

    private func dust() -> SCNParticleSystem {
        let system = puff(rate: 2.2, life: 7, size: 0.007, speed: 0.012, angle: 180)
        system.particleColor = UIColor(hex: 0xF4E2C4, alpha: 0.38)
        system.blendMode = .alpha
        system.acceleration = SCNVector3(0.004, -0.004, 0)
        return system
    }

    private func sparkle() -> SCNParticleSystem {
        let system = puff(rate: 1.3, life: 2.4, size: 0.009, speed: 0.02, angle: 40)
        system.particleColor = UIColor(hex: 0xFFE3A3, alpha: 0.85)
        system.blendMode = .additive
        system.acceleration = SCNVector3(0, 0.01, 0)
        return system
    }

    private func puff(rate: CGFloat, life: CGFloat, size: CGFloat, speed: CGFloat, angle: CGFloat) -> SCNParticleSystem {
        let system = SCNParticleSystem()
        system.birthRate = rate
        system.particleLifeSpan = life
        system.particleLifeSpanVariation = life * 0.25
        system.particleSize = size
        system.particleSizeVariation = size * 0.35
        system.emitterShape = SCNSphere(radius: 0.05)
        system.birthLocation = .surface
        system.particleVelocity = speed
        system.particleVelocityVariation = speed * 0.4
        system.emittingDirection = SCNVector3(0, 1, 0)
        system.spreadingAngle = angle
        system.loops = true
        system.isAffectedByGravity = false
        system.warmupDuration = 0.25
        system.particleImage = Self.blob
        return system
    }

    /// Shade and bulb meshes, not the device group. Groups in these models sit at the room origin.
    static let sparkNodes = [
        "table_lamp_shade", "table_lamp_booth_shade", "table_lamp_1_shade", "table_lamp_2_shade",
        "dresser_lamp_shade", "sconce_left_shade", "sconce_right_shade",
        "sconce_left_bulb_1", "sconce_right_bulb_1", "chandelier_bulb_1", "chandelier_bulb_4", "chandelier_lower_bulb_1",
        "dining_pendant_bulb", "flower_light_head", "flower_light_bloom_1", "prop_lamp"
    ]

    private static let blob: UIImage = {
        let side: CGFloat = 32
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        return UIGraphicsImageRenderer(size: CGSize(width: side, height: side), format: format).image { ctx in
            let colors = [UIColor.white.cgColor, UIColor.white.withAlphaComponent(0).cgColor] as CFArray
            guard let gradient = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(), colors: colors, locations: [0, 1]) else { return }
            let center = CGPoint(x: side / 2, y: side / 2)
            ctx.cgContext.drawRadialGradient(gradient, startCenter: center, startRadius: 0, endCenter: center, endRadius: side / 2, options: [])
        }
    }()
}

private extension SCNNode {
    func add(system: SCNParticleSystem) -> SCNParticleSystem {
        addParticleSystem(system)
        return system
    }
}

private extension UIColor {
    func mix(_ other: UIColor, amount: CGFloat) -> UIColor {
        var r1: CGFloat = 0, g1: CGFloat = 0, b1: CGFloat = 0, a1: CGFloat = 0
        var r2: CGFloat = 0, g2: CGFloat = 0, b2: CGFloat = 0, a2: CGFloat = 0
        getRed(&r1, green: &g1, blue: &b1, alpha: &a1)
        other.getRed(&r2, green: &g2, blue: &b2, alpha: &a2)
        let t = min(1, max(0, amount))
        return UIColor(red: r1 + (r2 - r1) * t, green: g1 + (g2 - g1) * t, blue: b1 + (b2 - b1) * t, alpha: a1 + (a2 - a1) * t)
    }
}

/// Turns a node about one of its own axes from a captured rest pose, so an interrupted loop can't
/// leave it a little more rotated each time, and stopping puts it back exactly where the model had it.
final class DollhouseMotion {
    private var rests: [ObjectIdentifier: SCNMatrix4] = [:]

    func rest(of node: SCNNode) -> SCNMatrix4 {
        let id = ObjectIdentifier(node)
        if let rest = rests[id] { return rest }
        rests[id] = node.transform
        return node.transform
    }

    /// `angle` gets the phase (0 to 1) of each period.
    func loop(_ node: SCNNode, key: String, axis: SCNVector3, period: TimeInterval, angle: @escaping (Float) -> Float) {
        let base = rest(of: node)
        node.removeAction(forKey: key)
        let action = SCNAction.customAction(duration: period) { node, elapsed in
            let turn = angle(Float(elapsed) / Float(period))
            node.transform = SCNMatrix4Mult(SCNMatrix4MakeRotation(turn, axis.x, axis.y, axis.z), base)
        }
        node.runAction(.repeatForever(action), forKey: key)
    }

    /// Waits, then plays one wiggle (`angle` of the 0-1 phase), forever.
    func every(_ node: SCNNode, key: String, wait: TimeInterval, jitter: TimeInterval = 0, axis: SCNVector3,
               duration: TimeInterval, angle: @escaping (Float) -> Float) {
        let base = rest(of: node)
        node.removeAction(forKey: key)
        node.transform = base
        let wiggle = SCNAction.customAction(duration: duration) { node, elapsed in
            let turn = angle(Float(elapsed) / Float(duration))
            node.transform = SCNMatrix4Mult(SCNMatrix4MakeRotation(turn, axis.x, axis.y, axis.z), base)
        }
        let settle = SCNAction.run { node in node.transform = base }
        let pause = jitter > 0 ? SCNAction.wait(duration: wait, withRange: jitter) : SCNAction.wait(duration: wait)
        node.runAction(.repeatForever(.sequence([pause, wiggle, settle])), forKey: key)
    }

    func restore(_ node: SCNNode, key: String) {
        node.removeAction(forKey: key)
        if let rest = rests[ObjectIdentifier(node)] { node.transform = rest }
    }
}

extension SCNNode {
    /// Bounds of every mesh under this node, in this node's own space. Nil when there is no mesh.
    func contentBounds() -> (SCNVector3, SCNVector3)? {
        var lo = SCNVector3(Float.greatestFiniteMagnitude, Float.greatestFiniteMagnitude, Float.greatestFiniteMagnitude)
        var hi = SCNVector3(-Float.greatestFiniteMagnitude, -Float.greatestFiniteMagnitude, -Float.greatestFiniteMagnitude)
        enumerateHierarchy { node, _ in
            guard node.geometry != nil else { return }
            let (a, b) = node.boundingBox
            for x in [a.x, b.x] { for y in [a.y, b.y] { for z in [a.z, b.z] {
                let p = node === self ? SCNVector3(x, y, z) : node.convertPosition(SCNVector3(x, y, z), to: self)
                lo = SCNVector3(min(lo.x, p.x), min(lo.y, p.y), min(lo.z, p.z))
                hi = SCNVector3(max(hi.x, p.x), max(hi.y, p.y), max(hi.z, p.z))
            } } }
        }
        return lo.x <= hi.x ? (lo, hi) : nil
    }

    /// True only for a small part that turns about a point on itself: never the scene root, the room
    /// root, or a big group (groups in these models often sit at the room origin, metres away).
    func isSmallSelfPivot(maxRadius: Float = 1.2) -> Bool {
        var depth = 0
        var up = parent
        while let node = up { depth += 1; up = node.parent }
        // scene root -> file root -> room group -> part
        guard depth >= 3 else { return false }
        guard let bounds = contentBounds() else { return false }
        let (lo, hi) = bounds
        let diagonal = convertVector(SCNVector3(hi.x - lo.x, hi.y - lo.y, hi.z - lo.z), to: nil)
        let radius = sqrt(diagonal.x * diagonal.x + diagonal.y * diagonal.y + diagonal.z * diagonal.z) / 2
        guard radius > 0, radius <= maxRadius else { return false }
        let unit = convertVector(SCNVector3(1, 0, 0), to: nil)
        let scale = max(sqrt(unit.x * unit.x + unit.y * unit.y + unit.z * unit.z), 0.0001)
        let margin = 0.15 / scale
        return lo.x - margin <= 0 && 0 <= hi.x + margin
            && lo.y - margin <= 0 && 0 <= hi.y + margin
            && lo.z - margin <= 0 && 0 <= hi.z + margin
    }
}
