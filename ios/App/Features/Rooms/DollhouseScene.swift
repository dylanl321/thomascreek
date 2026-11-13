import SceneKit
import UIKit

/// The cutaway, drawn with SceneKit from the room's USDZ.
/// Studio light is fixed. Device light, glow and the fireplace are layered on top of it.
final class DollhouseController {
    let room: DollhouseRoom
    private(set) var scene: SCNScene?
    private(set) var cameraNode: SCNNode?
    private(set) var error: String?

    private var nodes: [String: SCNNode] = [:]
    private var prepared: [String: Prepared] = [:]
    private var lamps: [String: SCNNode] = [:]
    private var hitNames: [String: String] = [:]
    private var last: [String: Bool] = [:]
    private var tickets: [String: Int] = [:]

    init(room: DollhouseRoom) { self.room = room }

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

    func device(at point: CGPoint, in view: SCNView) -> String? {
        let hits = view.hitTest(point, options: [
            SCNHitTestOption.searchMode: SCNHitTestSearchMode.closest.rawValue
        ])
        for hit in hits {
            var node: SCNNode? = hit.node
            while let current = node {
                if let name = current.name, let device = hitNames[name] { return device }
                node = current.parent
            }
        }
        return nil
    }

    /// `on` is the set of device names that should be lit.
    func apply(_ on: [String: Bool], animated: Bool) {
        guard scene != nil else { return }
        for device in room.devices.devices {
            let lit = on[device.name] ?? false
            if last[device.name] == lit { continue }
            last[device.name] = lit
            apply(device, on: lit, animated: animated)
        }
    }

    func place(_ preset: CameraPreset, animated: Bool) {
        guard let cameraNode, let camera = cameraNode.camera else { return }
        let move = {
            cameraNode.position = preset.position
            cameraNode.look(at: preset.target)
            camera.fieldOfView = preset.fov
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
            for name in device.hitNames where nodes[name] != nil {
                hitNames[name] = device.name
            }
        }
    }

    private func mesh(for name: String) -> SCNNode? {
        guard let node = nodes[name] else { return nil }
        if node.geometry != nil { return node }
        return node.childNodes.first { $0.geometry != nil }
    }

    private func prepareMaterials() {
        for (name, look) in room.appearance.nodes {
            guard let node = mesh(for: name), let geometry = node.geometry else { continue }
            let copy = geometry.copy() as? SCNGeometry ?? geometry
            copy.materials = geometry.materials.map { ($0.copy() as? SCNMaterial) ?? $0 }
            node.geometry = copy
            let picture = texture(look.baseTexture) ?? texture(look.emissiveTexture) ?? image(from: copy.firstMaterial?.diffuse.contents)
            prepared[name] = Prepared(node: node, materials: copy.materials, look: look, picture: picture)
            rest(name, on: false)
            if look.kind == "glow" || look.kind == "flame" { node.isHidden = true }
        }
    }

    private func addDeviceLights() {
        for (name, spec) in room.appearance.lights {
            guard let parent = nodes[name] else { continue }
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
        }
    }

    private func addStudio(to scene: SCNScene) {
        scene.background.contents = Self.gradient(top: 0xF1ECE4, bottom: 0xD9D0C3)
        scene.lightingEnvironment.contents = Self.gradient(top: 0xFFF3E3, bottom: 0x5B4636)
        scene.lightingEnvironment.intensity = 0.28

        let ambient = SCNNode()
        ambient.light = SCNLight()
        ambient.light?.type = .ambient
        ambient.light?.color = UIColor(hex: 0xFFF3E3)
        ambient.light?.intensity = 700
        scene.rootNode.addChildNode(ambient)

        let key = SCNNode()
        key.light = SCNLight()
        key.light?.type = .directional
        key.light?.color = UIColor(hex: 0xFFF1DD)
        key.light?.intensity = 1250
        key.light?.castsShadow = true
        key.light?.shadowMode = .deferred
        key.light?.shadowSampleCount = 16
        key.light?.shadowRadius = 8
        key.light?.shadowMapSize = CGSize(width: 2048, height: 2048)
        key.light?.shadowColor = UIColor(white: 0, alpha: 0.28)
        key.light?.zNear = 0.5
        key.light?.zFar = 40
        key.light?.orthographicScale = 7
        key.position = SCNVector3(-7, 10, 6)
        key.look(at: SCNVector3(-2, 0.4, 2))
        scene.rootNode.addChildNode(key)

        let camera = SCNNode()
        camera.camera = SCNCamera()
        camera.camera?.zNear = 0.05
        camera.camera?.zFar = 100
        camera.camera?.wantsHDR = true
        camera.camera?.wantsExposureAdaptation = false
        camera.camera?.exposureOffset = 0
        camera.camera?.bloomIntensity = 0.38
        camera.camera?.bloomThreshold = 1
        camera.camera?.bloomBlurRadius = 12
        camera.name = "dollhouse-camera"
        scene.rootNode.addChildNode(camera)
        cameraNode = camera
    }

    // MARK: state

    private func apply(_ device: DollhouseDevice, on: Bool, animated: Bool) {
        let ticket = (tickets[device.name] ?? 0) + 1
        tickets[device.name] = ticket
        if !on { stopFlicker(device) }
        let names = device.meshNodes + device.glowDecals + device.onState.shows
        if on {
            for name in device.glowDecals + device.onState.shows {
                prepared[name]?.node.isHidden = false
            }
        }
        SCNTransaction.begin()
        SCNTransaction.animationDuration = animated ? 0.45 : 0
        SCNTransaction.animationTimingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
        for name in names { rest(name, on: on) }
        for (index, name) in device.lightNodes.enumerated() {
            let spec = room.appearance.lights[name]
            let listed = index < device.lightOnIntensity.count ? device.lightOnIntensity[index] : (spec?.intensity ?? 0)
            let candela = listed > 0 ? listed : (spec?.intensity ?? 0)
            lamps[name]?.light?.intensity = on ? CGFloat(candela * Self.lumensPerCandela) : 0
        }
        SCNTransaction.completionBlock = { [weak self] in
            guard let self, self.tickets[device.name] == ticket else { return }
            if on {
                if device.type == .fireplace { self.startFlicker(device) }
            } else {
                self.stopFlicker(device)
                for name in device.glowDecals + device.onState.shows {
                    self.prepared[name]?.node.isHidden = true
                }
            }
        }
        SCNTransaction.commit()
    }

    private func rest(_ name: String, on: Bool) {
        guard let item = prepared[name] else { return }
        let look = item.look
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
            item.node.opacity = on ? 1 : 0
        case "flame":
            paintEmission(item, on: on, strength: look.strength)
            item.node.opacity = on ? CGFloat(look.opacity) : 0
        case "screen":
            for mat in item.materials {
                mat.lightingModel = .physicallyBased
                if on, let picture = item.picture {
                    mat.diffuse.contents = picture
                    mat.emission.contents = picture
                    mat.emission.intensity = CGFloat(max(look.strength, 1))
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
            paintEmission(item, on: on, strength: look.strength)
        default:
            paintEmission(item, on: on, strength: look.strength)
        }
    }

    private func paintEmission(_ item: Prepared, on: Bool, strength: Double) {
        let color = UIColor(srgb: item.look.emissiveSrgb)
        for mat in item.materials {
            mat.lightingModel = .physicallyBased
            mat.emission.contents = color
            // Strength stays in the intensity so a value above 1 is what the bloom threshold sees.
            mat.emission.intensity = on ? CGFloat(strength) : 0
            if item.look.doubleSided { mat.isDoubleSided = true }
        }
    }

    private func startFlicker(_ device: DollhouseDevice) {
        for (index, name) in device.onState.shows.enumerated() {
            guard let item = prepared[name] else { continue }
            pulse(item.node, base: CGFloat(max(item.look.strength, 0.2)), amount: 0.34, duration: 0.55 + Double(index) * 0.11)
        }
        if let lamp = lamps["light_fireplace"], let base = lamp.light?.intensity, base > 0 {
            pulse(lamp, base: base, amount: 0.22, duration: 0.8)
        }
    }

    private func stopFlicker(_ device: DollhouseDevice) {
        for name in device.onState.shows { prepared[name]?.node.removeAction(forKey: "flicker") }
        lamps["light_fireplace"]?.removeAction(forKey: "flicker")
    }

    private func pulse(_ node: SCNNode, base: CGFloat, amount: CGFloat, duration: TimeInterval) {
        node.removeAction(forKey: "flicker")
        let action = SCNAction.customAction(duration: duration) { node, elapsed in
            let wave = sin((elapsed / CGFloat(duration)) * .pi * 2)
            let value = base * (1 - amount + amount * (0.5 + 0.5 * wave))
            if node.light != nil {
                node.light?.intensity = value
            } else {
                node.geometry?.materials.forEach { $0.emission.intensity = value }
            }
        }
        node.runAction(.repeatForever(action), forKey: "flicker")
    }

    private func texture(_ name: String?) -> UIImage? {
        guard let name else { return nil }
        let relative = "LivingRoom/textures/\(name)"
        guard let url = DollhouseFiles.url(relative) ?? DollhouseFiles.url("textures/\(name)") else { return nil }
        return UIImage(contentsOfFile: url.path)
    }

    private func image(from contents: Any?) -> UIImage? {
        if let image = contents as? UIImage { return image }
        if let image = contents as? CGImage { return UIImage(cgImage: image) }
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
    private static let lumensPerCandela: Double = 90
}

private struct Prepared {
    var node: SCNNode
    var materials: [SCNMaterial]
    var look: LitNode
    var picture: UIImage?
}

enum CameraPreset {
    case dollhouse, close, top

    var position: SCNVector3 {
        switch self {
        case .dollhouse: SCNVector3(-8.4, 6.9, 8.6)
        case .close: SCNVector3(-6.6, 4.6, 6.9)
        case .top: SCNVector3(-4.2, 9.5, 5.2)
        }
    }
    var target: SCNVector3 {
        switch self {
        case .dollhouse: SCNVector3(-1.75, 0.95, 1.8)
        case .close: SCNVector3(-1.6, 1.15, 1.6)
        case .top: SCNVector3(-1.9, 0.4, 1.9)
        }
    }
    var fov: CGFloat {
        switch self {
        case .dollhouse, .close: 30
        case .top: 34
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

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> SCNView {
        let view = SCNView()
        view.backgroundColor = .clear
        view.antialiasingMode = .multisampling4X
        view.autoenablesDefaultLighting = false
        view.rendersContinuously = true
        view.allowsCameraControl = false
        let tap = UITapGestureRecognizer(target: context.coordinator, action: #selector(Coordinator.tapped(_:)))
        view.addGestureRecognizer(tap)
        return view
    }

    func updateUIView(_ view: SCNView, context: Context) {
        context.coordinator.controller = controller
        context.coordinator.onTap = onTap
        if view.scene !== controller.scene {
            view.scene = controller.scene
            view.pointOfView = controller.cameraNode
        }
    }

    final class Coordinator: NSObject {
        var controller: DollhouseController?
        var onTap: (String) -> Void = { _ in }
        @objc func tapped(_ gesture: UITapGestureRecognizer) {
            guard let view = gesture.view as? SCNView, let controller else { return }
            if let name = controller.device(at: gesture.location(in: view), in: view) { onTap(name) }
        }
    }
}
