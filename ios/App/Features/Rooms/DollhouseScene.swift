import SceneKit
import SwiftUI
import UIKit

/// The cutaway, drawn with SceneKit from the room's USDZ.
/// Device light, glow and the fireplace sit on a studio rig whose hue follows the time of day.
/// Room life (scenes, the cat, ambient motion) only changes this model. It never calls the hub.
final class DollhouseController {
    let room: DollhouseRoom
    private(set) var scene: SCNScene?
    private(set) var cameraNode: SCNNode?
    private(set) var error: String?
    weak var viewport: SCNView?

    private var nodes: [String: SCNNode] = [:]
    private var prepared: [String: Prepared] = [:]
    private var lamps: [String: SCNNode] = [:]
    private var hitNames: [String: String] = [:]
    private var last: [String: Int] = [:]
    private var tickets: [String: Int] = [:]
    private var life: DollhouseLife?
    private var lifeRunning = false
    private var motionReduced = false
    private var ambientLight: SCNNode?
    private var keyLight: SCNNode?
    private var windowLight: SCNNode?
    private var restPosition = SCNVector3Zero
    private var restTarget = SCNVector3Zero
    private var restFov: CGFloat = 30
    private var simulationRunning = true
    /// Pinch and two-finger pan. Zoom is 1 (the fitted shot) to `maxZoom`, never out past the fit.
    /// Pan slides the look-at point in the camera's own right/up plane, so the viewing angle never changes.
    private(set) var zoom: Float = 1
    private var panX: Float = 0
    private var panY: Float = 0
    private var fitRadius: Float = 3
    static let maxZoom: Float = 2.5

    init(room: DollhouseRoom) { self.room = room }

    deinit {
        life?.stop()
    }

    func load() {
        guard scene == nil else { return }
        guard let url = DollhouseFiles.url(room.model) else {
            error = "The room model isn't in the app bundle."
            return
        }
        do {
            let loaded = try SCNScene(url: url, options: [
                SCNSceneSource.LoadingOption.animationImportPolicy: SCNSceneSource.AnimationImportPolicy.doNotPlay
            ])
            scene = loaded
            Self.dropSurroundings(loaded.rootNode)
            DollhouseCat.hidePoses(in: loaded.rootNode)
            index(loaded.rootNode)
            buildHitNames()
            prepareMaterials()
            addDeviceLights()
            addStudio(to: loaded)
            place(.dollhouse, animated: false)
        } catch {
            self.error = "The room model couldn't be opened."
        }
    }

    /// Forgiving: looks through props, the cat and glow decals, walks up to the mapped device node, and
    /// if the finger landed just beside a lamp, tries a small ring of points around it.
    func device(at point: CGPoint, in view: SCNView) -> String? {
        if let name = deviceExactly(at: point, in: view) { return name }
        let r: CGFloat = 16
        let ring = [CGPoint(x: r, y: 0), CGPoint(x: -r, y: 0), CGPoint(x: 0, y: r), CGPoint(x: 0, y: -r),
                    CGPoint(x: r, y: r), CGPoint(x: -r, y: r), CGPoint(x: r, y: -r), CGPoint(x: -r, y: -r)]
        for offset in ring {
            if let name = deviceExactly(at: CGPoint(x: point.x + offset.x, y: point.y + offset.y), in: view) { return name }
        }
        return nil
    }

    private func deviceExactly(at point: CGPoint, in view: SCNView) -> String? {
        let hits = view.hitTest(point, options: [
            SCNHitTestOption.searchMode: SCNHitTestSearchMode.all.rawValue,
            SCNHitTestOption.ignoreHiddenNodes: true
        ])
        for hit in hits.prefix(8) {
            var node: SCNNode? = hit.node
            var passThrough = false
            while let current = node {
                if let name = current.name {
                    if let device = hitNames[name] { return device }
                    if name.hasPrefix("prop_") || name.hasPrefix("pet_cat") || name.hasPrefix("glow_") { passThrough = true }
                }
                node = current.parent
            }
            if !passThrough { return nil }
        }
        return nil
    }

    func startLife(real: [String: Double], owned: Set<String>, sunrise: Date?, sunset: Date?) {
        if life == nil { life = DollhouseLife(room: room, stage: self) }
        life?.setReduceMotion(motionReduced)
        life?.setRunning(lifeRunning)
        life?.start(real: real, owned: owned, sunrise: sunrise, sunset: sunset)
    }

    func noteHub(real: [String: Double], owned: Set<String>) {
        life?.noteHub(real: real, owned: owned)
    }

    func noteSun(sunrise: Date?, sunset: Date?) {
        life?.noteSun(sunrise: sunrise, sunset: sunset)
    }

    func setLifeRunning(_ on: Bool) {
        lifeRunning = on
        life?.setRunning(on)
    }

    func setReduceMotion(_ on: Bool) {
        motionReduced = on
        life?.setReduceMotion(on)
    }

    func place(_ preset: CameraPreset, animated: Bool) {
        guard let cameraNode, let camera = cameraNode.camera else { return }
        let shot = preset == .dollhouse ? fitted(room.shot(preset)) : room.shot(preset)
        restPosition = shot.position
        restTarget = shot.target
        restFov = shot.fov
        let dx = shot.position.x - shot.target.x, dy = shot.position.y - shot.target.y, dz = shot.position.z - shot.target.z
        fitRadius = max(0.5, sqrt(dx * dx + dy * dy + dz * dz) * sin(Float(shot.fov) * .pi / 360) / 0.92)
        zoom = 1
        panX = 0
        panY = 0
        let move = {
            camera.fieldOfView = shot.fov
            self.applyView()
        }
        if animated {
            SCNTransaction.begin()
            SCNTransaction.animationDuration = 0.7
            SCNTransaction.animationTimingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
            move()
            SCNTransaction.commit()
        } else {
            move()
        }
    }

    /// Keeps the catalog's viewing angle but backs the camera off until the whole model,
    /// floor edge and wall tops included, sits inside the frame with a little air around it.
    private func fitted(_ shot: DollhouseShot) -> DollhouseShot {
        guard let root = scene?.rootNode else { return shot }
        var lo = SCNVector3(Float.greatestFiniteMagnitude, Float.greatestFiniteMagnitude, Float.greatestFiniteMagnitude)
        var hi = SCNVector3(-Float.greatestFiniteMagnitude, -Float.greatestFiniteMagnitude, -Float.greatestFiniteMagnitude)
        root.enumerateHierarchy { node, _ in
            guard node.geometry != nil, !node.isHidden else { return }
            let (a, b) = node.boundingBox
            for x in [a.x, b.x] { for y in [a.y, b.y] { for z in [a.z, b.z] {
                let w = node.convertPosition(SCNVector3(x, y, z), to: nil)
                lo = SCNVector3(min(lo.x, w.x), min(lo.y, w.y), min(lo.z, w.z))
                hi = SCNVector3(max(hi.x, w.x), max(hi.y, w.y), max(hi.z, w.z))
            } } }
        }
        guard lo.x <= hi.x else { return shot }
        let center = SCNVector3((lo.x + hi.x) / 2, (lo.y + hi.y) / 2, (lo.z + hi.z) / 2)
        let dx = hi.x - lo.x, dy = hi.y - lo.y, dz = hi.z - lo.z
        let radius = sqrt(dx * dx + dy * dy + dz * dz) / 2
        var dir = SCNVector3(shot.position.x - shot.target.x, shot.position.y - shot.target.y, shot.position.z - shot.target.z)
        let len = sqrt(dir.x * dir.x + dir.y * dir.y + dir.z * dir.z)
        guard len > 0, radius > 0 else { return shot }
        dir = SCNVector3(dir.x / len, dir.y / len, dir.z / len)
        let half = Float(shot.fov) * .pi / 360
        let distance = radius / sin(half) * 0.92
        return DollhouseShot(position: SCNVector3(center.x + dir.x * distance, center.y + dir.y * distance, center.z + dir.z * distance),
                             target: center, fov: shot.fov)
    }

    /// The page shows the room alone, like a dollhouse on a table. The generators also build what is outside
    /// the room (deck, yard, patio, lawn, shrubs, glimpses of the next room). Those stay in the source models
    /// for the full-house view, and are dropped here.
    private static func dropSurroundings(_ root: SCNNode) {
        var doomed: [SCNNode] = []
        root.enumerateHierarchy { node, stop in
            guard let name = node.name?.lowercased() else { return }
            if name.hasPrefix("exterior_") || name.hasSuffix("_glimpse") { doomed.append(node) }
        }
        doomed.forEach { $0.removeFromParentNode() }
    }

    // MARK: load

    private func index(_ node: SCNNode) {
        if let name = node.name, !name.isEmpty, name != "mesh" {
            if nodes[name] == nil { nodes[name] = node }
            let leaf = name.split(separator: "/").last.map(String.init) ?? name
            if nodes[leaf] == nil { nodes[leaf] = node }
        }
        node.childNodes.forEach(index)
    }

    private func buildHitNames() {
        for device in room.devices.devices {
            for name in device.hitNames {
                guard let key = resolved(name) else { continue }
                hitNames[key] = device.name
            }
        }
    }

    /// USD prim names cannot contain a dot, so `light_recessed_row_1.3` is stored as `light_recessed_row_1_3`.
    private func resolved(_ name: String) -> String? {
        if nodes[name] != nil { return name }
        let safe = name.replacingOccurrences(of: ".", with: "_")
        return nodes[safe] != nil ? safe : nil
    }

    private func mesh(for name: String) -> SCNNode? {
        guard let key = resolved(name), let node = nodes[key] else { return nil }
        if node.geometry != nil { return node }
        return node.childNodes.first { $0.geometry != nil }
    }

    private func prep(_ name: String) -> Prepared? {
        prepared[name] ?? prepared[name.replacingOccurrences(of: ".", with: "_")]
    }

    private func prepareMaterials() {
        for (name, look) in room.appearance.nodes {
            guard let node = mesh(for: name), let geometry = node.geometry else { continue }
            let copy = geometry.copy() as? SCNGeometry ?? geometry
            copy.materials = geometry.materials.map { ($0.copy() as? SCNMaterial) ?? $0 }
            node.geometry = copy
            let picture = texture(look.baseTexture) ?? texture(look.emissiveTexture) ?? image(from: copy.firstMaterial?.diffuse.contents)
            prepared[name] = Prepared(node: node, materials: copy.materials, look: look, picture: picture)
            rest(name, level: 0)
            if look.kind == "glow" || look.kind == "flame" || look.kind == "cone" { node.isHidden = true }
        }
    }

    private func addDeviceLights() {
        for (name, spec) in room.appearance.lights {
            guard let key = resolved(name), let parent = nodes[key] else { continue }
            let light = SCNLight()
            light.type = spec.type == "spot" ? .spot : .omni
            light.color = UIColor(srgb: spec.colorSrgb)
            light.intensity = 0
            light.castsShadow = false
            light.attenuationStartDistance = 0
            light.attenuationEndDistance = CGFloat(spec.range > 0 ? spec.range : 6)
            light.attenuationFalloffExponent = 2
            if spec.type == "spot" {
                light.spotInnerAngle = CGFloat(spec.innerDeg ?? 18)
                light.spotOuterAngle = CGFloat(spec.outerDeg ?? 43)
            }
            let node = SCNNode()
            node.light = light
            node.name = name
            parent.addChildNode(node)
            lamps[name] = node
            lamps[key] = node
        }
    }

    private func addStudio(to scene: SCNScene) {
        scene.background.contents = UIColor.clear
        scene.lightingEnvironment.contents = Self.gradient(top: 0xFFF3E3, bottom: 0x5B4636)
        scene.lightingEnvironment.intensity = 0.16

        let ambient = SCNNode()
        ambient.name = "dollhouse-ambient"
        ambient.light = SCNLight()
        ambient.light?.type = .ambient
        ambient.light?.color = UIColor(hex: 0xFFF3E3)
        ambient.light?.intensity = 260
        scene.rootNode.addChildNode(ambient)
        ambientLight = ambient

        let key = SCNNode()
        key.name = "dollhouse-key"
        key.light = SCNLight()
        key.light?.type = .directional
        key.light?.color = UIColor(hex: 0xFFF1DD)
        key.light?.intensity = 750
        key.light?.castsShadow = true
        key.light?.shadowMode = .deferred
        key.light?.shadowSampleCount = 16
        key.light?.shadowRadius = 8
        key.light?.shadowMapSize = CGSize(width: 2048, height: 2048)
        key.light?.shadowColor = UIColor(white: 0, alpha: 0.28)
        key.light?.zNear = 0.5
        key.light?.zFar = 80
        let aim = room.shot(.dollhouse)
        let span = Double(max(Float(8), Float(abs(aim.position.x - aim.target.x))))
        key.light?.orthographicScale = span
        key.position = SCNVector3(aim.target.x - 5, aim.target.y + 10, aim.target.z + 4)
        key.look(at: aim.target)
        scene.rootNode.addChildNode(key)
        keyLight = key
        placeWindowLight(in: scene)

        let camera = SCNNode()
        camera.camera = SCNCamera()
        camera.camera?.zNear = 0.05
        camera.camera?.zFar = 100
        camera.camera?.wantsHDR = true
        camera.camera?.wantsExposureAdaptation = false
        camera.camera?.exposureOffset = -0.6
        camera.camera?.bloomIntensity = 0.18
        camera.camera?.bloomThreshold = 1.3
        camera.camera?.bloomBlurRadius = 12
        camera.name = "dollhouse-camera"
        scene.rootNode.addChildNode(camera)
        cameraNode = camera
    }

    // MARK: state

    /// Each device is a 0...1 brightness. 0 is off. Glow, emission and lamps ease to it.
    /// Flame flicker, the fan and the drum are owned by room life, after this fade finishes.
    func showLevels(_ levels: [String: Double], duration: TimeInterval, completion: (() -> Void)? = nil) {
        guard scene != nil else { completion?(); return }
        struct Change {
            var device: DollhouseDevice
            var level: Double
            var ticket: Int
        }
        var changes: [Change] = []
        for device in room.devices.devices {
            let level = min(1, max(0, levels[device.name] ?? 0))
            let key = Int((level * 100).rounded())
            if last[device.name] == key { continue }
            last[device.name] = key
            let ticket = (tickets[device.name] ?? 0) + 1
            tickets[device.name] = ticket
            changes.append(Change(device: device, level: level, ticket: ticket))
        }
        guard !changes.isEmpty else { completion?(); return }
        for change in changes where change.level > 0.001 {
            for name in change.device.glowDecals + change.device.onState.shows {
                prep(name)?.node.isHidden = false
            }
        }
        SCNTransaction.begin()
        SCNTransaction.animationDuration = duration
        SCNTransaction.animationTimingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
        for change in changes {
            let names = change.device.meshNodes + change.device.glowDecals + change.device.onState.shows
            for name in names { rest(name, level: change.level) }
            for (index, name) in change.device.lightNodes.enumerated() {
                let spec = lightSpec(name)
                let listed = index < change.device.lightOnIntensity.count ? change.device.lightOnIntensity[index] : (spec?.intensity ?? 0)
                let candela = listed > 0 ? listed : (spec?.intensity ?? 0)
                let lumens = CGFloat(candela * Self.lumensPerCandela) * CGFloat(change.level)
                lamps[name]?.light?.intensity = lumens
                lamps[name.replacingOccurrences(of: ".", with: "_")]?.light?.intensity = lumens
            }
        }
        SCNTransaction.completionBlock = { [weak self] in
            guard let self else { completion?(); return }
            for change in changes where self.tickets[change.device.name] == change.ticket && change.level <= 0.001 {
                for name in change.device.glowDecals + change.device.onState.shows {
                    self.prep(name)?.node.isHidden = true
                }
            }
            completion?()
        }
        SCNTransaction.commit()
    }

    private func rest(_ name: String, level: Double) {
        guard let item = prep(name) else { return }
        let look = item.look
        let on = level > 0.001
        switch look.kind {
        case "glow":
            for mat in item.materials {
                mat.lightingModel = .constant
                mat.isDoubleSided = true
                mat.writesToDepthBuffer = false
                mat.blendMode = .add
                mat.transparencyMode = .aOne
                if let picture = item.picture { mat.diffuse.contents = picture }
                mat.multiply.contents = UIColor(srgb: look.baseSrgb)
                mat.transparency = CGFloat(look.opacity)
            }
            // Same curve as the three.js builder: a dim light keeps a little glow, then scales up.
            item.node.opacity = on ? CGFloat(0.2 + 0.8 * level) : 0
        case "flame", "cone":
            paintEmission(item, level: level, strength: look.strength)
            item.node.opacity = CGFloat(look.opacity) * CGFloat(level)
        case "screen":
            for mat in item.materials {
                mat.lightingModel = .physicallyBased
                if on, let picture = item.picture {
                    mat.diffuse.contents = picture
                    mat.emission.contents = picture
                    mat.emission.intensity = CGFloat(max(look.strength, 1)) * CGFloat(level)
                    mat.roughness.contents = 0.4
                    mat.metalness.contents = 0
                } else {
                    mat.diffuse.contents = UIColor(white: 0.024, alpha: 1)
                    mat.emission.contents = UIColor.black
                    mat.emission.intensity = 0
                    mat.roughness.contents = 0.12
                    mat.metalness.contents = 0.3
                }
            }
        case "bulb", "shade", "led", "embers":
            paintEmission(item, level: level, strength: look.strength)
        default:
            paintEmission(item, level: level, strength: look.strength)
        }
    }

    private func paintEmission(_ item: Prepared, level: Double, strength: Double) {
        let color = UIColor(srgb: item.look.emissiveSrgb)
        for mat in item.materials {
            mat.lightingModel = .physicallyBased
            mat.emission.contents = color
            // Strength stays in the intensity so a value above 1 is what the bloom threshold sees.
            mat.emission.intensity = CGFloat(strength) * CGFloat(level)
            if item.look.doubleSided { mat.isDoubleSided = true }
        }
    }

    private func lightSpec(_ name: String) -> LitLight? {
        room.appearance.lights[name] ?? room.appearance.lights[name.replacingOccurrences(of: ".", with: "_")]
    }

    private func texture(_ name: String?) -> UIImage? {
        guard let name else { return nil }
        let folder = (room.model as NSString).deletingLastPathComponent
        let relative = folder.isEmpty ? "textures/\(name)" : "\(folder)/textures/\(name)"
        guard let url = DollhouseFiles.url(relative) else { return nil }
        return UIImage(contentsOfFile: url.path)
    }

    private func image(from contents: Any?) -> UIImage? {
        if let image = contents as? UIImage { return image }
        if let contents, CFGetTypeID(contents as CFTypeRef) == CGImage.typeID { return UIImage(cgImage: contents as! CGImage) }
        return nil
    }

    private static func gradient(top: UInt32, bottom: UInt32) -> UIImage {
        let size = CGSize(width: 16, height: 512)
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        return UIGraphicsImageRenderer(size: size, format: format).image { ctx in
            let colors = [UIColor(hex: top).cgColor, UIColor(hex: bottom).cgColor] as CFArray
            let space = CGColorSpaceCreateDeviceRGB()
            guard let gradient = CGGradient(colorsSpace: space, colors: colors, locations: [0, 1]) else { return }
            ctx.cgContext.drawLinearGradient(gradient, start: .zero, end: CGPoint(x: 0, y: size.height), options: [])
        }
    }

    /// glTF point-light intensity is candela. SceneKit's omni intensity is lumens, and the room is a miniature,
    /// so this scale keeps a shelf puck a pool of light instead of a second sun.
    private static let lumensPerCandela: Double = 45
}

extension DollhouseController: DollhouseStage {
    func node(named name: String) -> SCNNode? {
        guard let key = resolved(name) else { return nil }
        return nodes[key]
    }

    func lamp(named name: String) -> SCNNode? {
        if let lamp = lamps[name], lamp.light != nil { return lamp }
        let safe = name.replacingOccurrences(of: ".", with: "_")
        if let lamp = lamps[safe], lamp.light != nil { return lamp }
        return nil
    }

    func eachNamedNode(_ body: (String, SCNNode) -> Void) {
        scene?.rootNode.enumerateHierarchy { node, _ in
            guard let name = node.name, !name.isEmpty, name != "mesh" else { return }
            body(name, node)
        }
    }

    func setDaylight(_ daylight: DollhouseDaylight, animated: Bool) {
        SCNTransaction.begin()
        SCNTransaction.animationDuration = animated ? DollhouseLifeTiming.scene : 0
        ambientLight?.light?.color = daylight.ambient
        keyLight?.light?.color = daylight.key
        windowLight?.light?.color = daylight.window
        windowLight?.light?.intensity = daylight.windowIntensity
        scene?.lightingEnvironment.contents = Self.gradient(top: daylight.envTop, bottom: daylight.envBottom)
        scene?.lightingEnvironment.intensity = 0.16
        SCNTransaction.commit()
    }

    func setSimulation(running: Bool, animated: Bool) {
        simulationRunning = running
        scene?.isPaused = !running
        viewport?.scene?.isPaused = !running
        viewport?.rendersContinuously = running && animated
        holdCamera()
    }

    /// The camera never moves on its own. Build 36 orbited it a few degrees every 48 s and tilted it with
    /// the phone's gyro, which read as the whole room spinning. Life only moves small parts now.
    func setCameraMotion(enabled: Bool) {
        holdCamera()
    }

    private func holdCamera() {
        guard let cameraNode else { return }
        cameraNode.removeAllActions()
        cameraNode.camera?.fieldOfView = restFov
        applyView()
    }

    // MARK: zoom and pan

    /// How far the look-at point may slide: about 18% of the room's radius at the fit, more when zoomed in.
    private var panLimit: Float { fitRadius * 0.18 * zoom }

    private var basis: (dir: SCNVector3, right: SCNVector3, up: SCNVector3, distance: Float) {
        var dir = SCNVector3(restPosition.x - restTarget.x, restPosition.y - restTarget.y, restPosition.z - restTarget.z)
        let distance = max(0.001, sqrt(dir.x * dir.x + dir.y * dir.y + dir.z * dir.z))
        dir = SCNVector3(dir.x / distance, dir.y / distance, dir.z / distance)
        // right = worldUp x dir, up = dir x right
        var right = SCNVector3(dir.z, 0, -dir.x)
        let rl = max(0.001, sqrt(right.x * right.x + right.z * right.z))
        right = SCNVector3(right.x / rl, 0, right.z / rl)
        let up = SCNVector3(dir.y * right.z - dir.z * right.y, dir.z * right.x - dir.x * right.z, dir.x * right.y - dir.y * right.x)
        return (dir, right, up, distance)
    }

    private func applyView() {
        guard let cameraNode else { return }
        let b = basis
        let target = SCNVector3(restTarget.x + b.right.x * panX + b.up.x * panY,
                                restTarget.y + b.right.y * panX + b.up.y * panY,
                                restTarget.z + b.right.z * panX + b.up.z * panY)
        let distance = b.distance / zoom
        cameraNode.position = SCNVector3(target.x + b.dir.x * distance, target.y + b.dir.y * distance, target.z + b.dir.z * distance)
        cameraNode.look(at: target)
    }

    private func clampPan() {
        let limit = panLimit
        let length = sqrt(panX * panX + panY * panY)
        if length > limit, length > 0 {
            panX *= limit / length
            panY *= limit / length
        }
    }

    func setZoom(_ value: Float) {
        zoom = min(Self.maxZoom, max(1, value))
        clampPan()
        applyView()
    }

    /// `dx`/`dy` are points the fingers moved; the model follows the fingers.
    func pan(byPoints dx: CGFloat, dy: CGFloat, viewHeight: CGFloat) {
        guard viewHeight > 0 else { return }
        let b = basis
        let visible = 2 * (b.distance / zoom) * tan(Float(restFov) * .pi / 360)
        let perPoint = visible / Float(viewHeight)
        panX -= Float(dx) * perPoint
        panY += Float(dy) * perPoint
        clampPan()
        applyView()
    }

    var isZoomedOrPanned: Bool { zoom > 1.01 || abs(panX) > 0.001 || abs(panY) > 0.001 }

    /// Springs back to the fitted shot.
    func resetView(animated: Bool) {
        zoom = 1
        panX = 0
        panY = 0
        SCNTransaction.begin()
        SCNTransaction.animationDuration = animated ? 0.45 : 0
        SCNTransaction.animationTimingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
        applyView()
        SCNTransaction.commit()
    }

    private func placeWindowLight(in scene: SCNScene) {
        let light = SCNLight()
        light.type = .directional
        light.color = UIColor(hex: 0xF4F0E6)
        light.intensity = 22
        light.castsShadow = false
        let node = SCNNode()
        node.light = light
        node.name = "dollhouse-window-light"
        var origin: SCNVector3?
        scene.rootNode.enumerateHierarchy { child, _ in
            guard origin == nil, let name = child.name?.lowercased() else { return }
            if (name.contains("window") && name.contains("glass")) || name == "skylight_diffuser" {
                origin = child.worldPosition
            }
        }
        let aim = room.shot(.dollhouse).target
        node.position = origin ?? SCNVector3(aim.x + 4, aim.y + 3, aim.z + 2)
        node.look(at: aim)
        scene.rootNode.addChildNode(node)
        windowLight = node
    }
}

private struct Prepared {
    var node: SCNNode
    var materials: [SCNMaterial]
    var look: LitNode
    var picture: UIImage?
}

extension DollhouseRoom {
    /// The catalog's camera, or the living-room angle when a room doesn't list one.
    fileprivate func shot(_ preset: CameraPreset) -> DollhouseShot {
        let spec: DollhouseShotSpec?
        switch preset {
        case .dollhouse: spec = views?.dollhouse
        case .close: spec = views?.close
        case .top: spec = views?.top
        }
        if let spec, spec.position.count == 3, spec.target.count == 3 {
            return DollhouseShot(position: SCNVector3(spec.position[0], spec.position[1], spec.position[2]),
                                 target: SCNVector3(spec.target[0], spec.target[1], spec.target[2]),
                                 fov: CGFloat(spec.fov))
        }
        return preset.builtIn
    }
}

struct DollhouseShot {
    var position: SCNVector3
    var target: SCNVector3
    var fov: CGFloat
}

enum CameraPreset {
    case dollhouse, close, top

    var builtIn: DollhouseShot {
        switch self {
        case .dollhouse: DollhouseShot(position: SCNVector3(-8.4, 6.9, 8.6), target: SCNVector3(-1.75, 0.95, 1.8), fov: 30)
        case .close: DollhouseShot(position: SCNVector3(-6.6, 4.6, 6.9), target: SCNVector3(-1.6, 1.15, 1.6), fov: 30)
        case .top: DollhouseShot(position: SCNVector3(-4.2, 9.5, 5.2), target: SCNVector3(-1.9, 0.4, 1.9), fov: 34)
        }
    }
}

private extension UIColor {
    convenience init(srgb: [Double], scale: CGFloat = 1) {
        let r = CGFloat(srgb.count > 0 ? srgb[0] : 0) * scale
        let g = CGFloat(srgb.count > 1 ? srgb[1] : 0) * scale
        let b = CGFloat(srgb.count > 2 ? srgb[2] : 0) * scale
        self.init(red: r, green: g, blue: b, alpha: 1)
    }
}

struct DollhouseViewport: UIViewRepresentable {
    var controller: DollhouseController
    var onTap: (String) -> Void
    var onLongPress: (String) -> Void = { _ in }

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> SCNView {
        let view = SCNView()
        view.backgroundColor = .clear
        view.antialiasingMode = .multisampling4X
        view.autoenablesDefaultLighting = false
        view.rendersContinuously = true
        // Never SceneKit's camera control: it orbits. Zoom and pan are our own, angle fixed.
        view.allowsCameraControl = false
        let coordinator = context.coordinator

        let pinch = UIPinchGestureRecognizer(target: coordinator, action: #selector(Coordinator.pinched(_:)))
        let pan = UIPanGestureRecognizer(target: coordinator, action: #selector(Coordinator.panned(_:)))
        // Two fingers, so one-finger drags stay with the page's vertical scroll.
        pan.minimumNumberOfTouches = 2
        pan.maximumNumberOfTouches = 2
        // No double-tap: it would make every single tap on a light wait to see if a second one follows.
        let tap = UITapGestureRecognizer(target: coordinator, action: #selector(Coordinator.tapped(_:)))
        let hold = UILongPressGestureRecognizer(target: coordinator, action: #selector(Coordinator.held(_:)))
        hold.minimumPressDuration = 0.45
        for recognizer in [pinch, pan, tap, hold] as [UIGestureRecognizer] {
            recognizer.delegate = coordinator
            view.addGestureRecognizer(recognizer)
        }
        coordinator.pinch = pinch
        coordinator.pan = pan
        return view
    }

    func updateUIView(_ view: SCNView, context: Context) {
        context.coordinator.controller = controller
        context.coordinator.onTap = onTap
        context.coordinator.onLongPress = onLongPress
        controller.viewport = view
        if view.scene !== controller.scene {
            view.scene = controller.scene
            view.pointOfView = controller.cameraNode
        }
    }

    final class Coordinator: NSObject, UIGestureRecognizerDelegate {
        var controller: DollhouseController?
        var onTap: (String) -> Void = { _ in }
        var onLongPress: (String) -> Void = { _ in }
        weak var pinch: UIPinchGestureRecognizer?
        weak var pan: UIPanGestureRecognizer?
        private var startZoom: Float = 1
        private let haptic = UIImpactFeedbackGenerator(style: .light)

        @objc func tapped(_ gesture: UITapGestureRecognizer) {
            guard let view = gesture.view as? SCNView, let controller else { return }
            guard let name = controller.device(at: gesture.location(in: view), in: view) else {
                // A tap on the room (not a device) springs a zoomed or panned view back to the fit.
                if controller.isZoomedOrPanned { controller.resetView(animated: true) }
                return
            }
            haptic.impactOccurred()
            onTap(name)
        }

        @objc func held(_ gesture: UILongPressGestureRecognizer) {
            guard gesture.state == .began, let view = gesture.view as? SCNView, let controller else { return }
            guard let name = controller.device(at: gesture.location(in: view), in: view) else { return }
            UIImpactFeedbackGenerator(style: .medium).impactOccurred()
            onLongPress(name)
        }

        @objc func pinched(_ gesture: UIPinchGestureRecognizer) {
            guard let controller else { return }
            switch gesture.state {
            case .began:
                startZoom = controller.zoom
                holdPageScroll(gesture.view, true)
            case .changed:
                controller.setZoom(startZoom * Float(gesture.scale))
            default:
                holdPageScroll(gesture.view, false)
            }
        }

        @objc func panned(_ gesture: UIPanGestureRecognizer) {
            guard let controller, let view = gesture.view else { return }
            switch gesture.state {
            case .began:
                holdPageScroll(view, true)
            case .changed:
                let t = gesture.translation(in: view)
                controller.pan(byPoints: t.x, dy: t.y, viewHeight: view.bounds.height)
                gesture.setTranslation(.zero, in: view)
            default:
                holdPageScroll(view, false)
            }
        }

        /// While two fingers zoom or pan the model, the page must not scroll under them.
        private func holdPageScroll(_ view: UIView?, _ hold: Bool) {
            var up = view?.superview
            while let current = up {
                if let scroll = current as? UIScrollView, scroll.isScrollEnabled || !hold {
                    if scroll.panGestureRecognizer.view == scroll && scroll.alwaysBounceHorizontal == false {
                        scroll.panGestureRecognizer.isEnabled = !hold
                        scroll.pinchGestureRecognizer?.isEnabled = !hold
                    }
                }
                up = current.superview
            }
        }

        func gestureRecognizer(_ gesture: UIGestureRecognizer,
                               shouldRecognizeSimultaneouslyWith other: UIGestureRecognizer) -> Bool {
            (gesture === pinch && other === pan) || (gesture === pan && other === pinch)
        }
    }
}
