// Offscreen exposure check for the dollhouse rooms, on a Mac.
// Mirrors the app's studio rig, device lights and emission rules closely enough to compare lighting changes.
//   swiftc -O ios/scripts/dollhouse_snapshot.swift -o /tmp/dollhouse_snapshot
//   /tmp/dollhouse_snapshot ios/Rooms <room_id> <off|allon|under> <morning|midday|dusk|night> ios/scripts/dollhouse_rig.json <out.png>
// dollhouse_rig.json holds the same numbers as `Rig` in DollhouseScene.swift. Change both together.
// Prints: mean luminance (0-255, lit model pixels only), % near-white (Y >= 235), % clipped (any channel >= 254).
import AppKit
import Foundation
import SceneKit

struct Rig: Decodable {
    var ambient: Double
    var key: Double
    var keyShadowAlpha: Double
    var envIntensity: Double
    var windowScale: Double
    var exposure: Double
    var bloomIntensity: Double
    var bloomThreshold: Double
    var whitePoint: Double
    var lumensPerCandela: Double
    var shareAbove: Int           // device lights past this many lit share one budget
    var rangeScale: Double
    var emissionScale: Double
    var glowScale: Double
}

func srgb(_ v: [Double], _ s: Double = 1) -> NSColor {
    NSColor(srgbRed: CGFloat(v[0] * s), green: CGFloat(v[1] * s), blue: CGFloat(v[2] * s), alpha: 1)
}
func hex(_ h: UInt32) -> NSColor {
    NSColor(srgbRed: CGFloat((h >> 16) & 0xFF) / 255, green: CGFloat((h >> 8) & 0xFF) / 255, blue: CGFloat(h & 0xFF) / 255, alpha: 1)
}
func gradient(_ top: UInt32, _ bottom: UInt32) -> CGImage {
    let w = 16, h = 512
    let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0,
                        space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
    let g = CGGradient(colorsSpace: CGColorSpace(name: CGColorSpace.sRGB)!, colors: [hex(top).cgColor, hex(bottom).cgColor] as CFArray, locations: [0, 1])!
    ctx.drawLinearGradient(g, start: CGPoint(x: 0, y: h), end: .zero, options: [])
    return ctx.makeImage()!
}

let a = CommandLine.arguments
guard a.count == 7 else { print("usage: rooms room_id off|allon|under part rig.json out.png"); exit(2) }
let roomsDir = URL(fileURLWithPath: a[1]), roomId = a[2], mode = a[3], part = a[4]
let rig = try JSONDecoder().decode(Rig.self, from: Data(contentsOf: URL(fileURLWithPath: a[5])))
let catalog = try JSONSerialization.jsonObject(with: Data(contentsOf: roomsDir.appendingPathComponent("catalog.json"))) as! [String: Any]
let row = (catalog["rooms"] as! [[String: Any]]).first { $0["id"] as? String == roomId }!
let appearance = try JSONSerialization.jsonObject(with: Data(contentsOf: roomsDir.appendingPathComponent(row["appearance"] as! String))) as! [String: Any]
let devices = try JSONSerialization.jsonObject(with: Data(contentsOf: roomsDir.appendingPathComponent(row["devices"] as! String))) as! [String: Any]
let scene = try SCNScene(url: roomsDir.appendingPathComponent(row["model"] as! String), options: [.animationImportPolicy: SCNSceneSource.AnimationImportPolicy.doNotPlay])

var doomed: [SCNNode] = []
var nodes: [String: SCNNode] = [:]
scene.rootNode.enumerateHierarchy { n, _ in
    guard let name = n.name, !name.isEmpty, name != "mesh" else { return }
    let l = name.lowercased()
    if l.hasPrefix("exterior_") || l.hasSuffix("_glimpse") { doomed.append(n) }
    if l.hasPrefix("pet_cat_pose_") && !l.contains("_head") && !l.contains("_tail") && !l.hasSuffix("sleep") { n.isHidden = true }
    if nodes[name] == nil { nodes[name] = n }
}
doomed.forEach { $0.removeFromParentNode() }
func resolve(_ name: String) -> SCNNode? { nodes[name] ?? nodes[name.replacingOccurrences(of: ".", with: "_")] }
func mesh(_ name: String) -> SCNNode? {
    guard let n = resolve(name) else { return nil }
    return n.geometry != nil ? n : n.childNodes.first { $0.geometry != nil }
}

// Which devices are lit, by mode.
var litDevices = Set<String>()
for d in devices["devices"] as! [[String: Any]] {
    let name = d["name"] as! String
    if mode == "allon" { litDevices.insert(name) }
    if mode == "under" && name == "under_cabinet_lights" { litDevices.insert(name) }
}
var litNodes = Set<String>(), litLights: [String] = []
for d in devices["devices"] as! [[String: Any]] where litDevices.contains(d["name"] as! String) {
    for k in ["mesh_nodes", "glow_decals"] { (d[k] as? [String])?.forEach { litNodes.insert($0) } }
    ((d["on_state"] as? [String: Any])?["shows"] as? [String])?.forEach { litNodes.insert($0) }
    (d["light_nodes"] as? [String])?.forEach { litLights.append($0) }
}

// Emission and glow, per the app's rest(_:level:).
for (name, raw) in appearance["nodes"] as! [String: [String: Any]] {
    guard let node = mesh(name), let geo = node.geometry else { continue }
    let copy = geo.copy() as! SCNGeometry
    copy.materials = geo.materials.map { $0.copy() as! SCNMaterial }
    node.geometry = copy
    let kind = raw["kind"] as? String ?? ""
    let on = litNodes.contains(name) || litNodes.contains(name.replacingOccurrences(of: "_", with: "."))
    let strength = raw["strength"] as? Double ?? 0
    let em = raw["emissive_srgb"] as? [Double] ?? [0, 0, 0]
    switch kind {
    case "glow":
        node.isHidden = !on
        for m in copy.materials {
            m.lightingModel = .constant; m.isDoubleSided = true; m.writesToDepthBuffer = false
            m.blendMode = .add; m.transparencyMode = .aOne
            m.multiply.contents = srgb(raw["base_srgb"] as? [Double] ?? [1, 1, 1])
            m.transparency = CGFloat(raw["opacity"] as? Double ?? 1)
        }
        node.opacity = on ? CGFloat(rig.glowScale) : 0
    case "flame", "cone":
        node.isHidden = !on
        for m in copy.materials { m.lightingModel = .physicallyBased; m.emission.contents = srgb(em); m.emission.intensity = on ? CGFloat(strength * rig.emissionScale) : 0 }
        node.opacity = on ? CGFloat(raw["opacity"] as? Double ?? 1) : 0
    case "screen":
        for m in copy.materials {
            m.lightingModel = .physicallyBased
            if on { m.emission.contents = m.diffuse.contents; m.emission.intensity = CGFloat(max(strength, 1) * rig.emissionScale) }
            else { m.diffuse.contents = NSColor(white: 0.024, alpha: 1); m.emission.contents = NSColor.black; m.emission.intensity = 0 }
        }
    default:
        for m in copy.materials { m.lightingModel = .physicallyBased; m.emission.contents = srgb(em); m.emission.intensity = on ? CGFloat(strength * rig.emissionScale) : 0 }
    }
}

// Device lights.
let lights = appearance["lights"] as! [String: [String: Any]]
let share = litLights.count > rig.shareAbove ? Double(rig.shareAbove) / Double(litLights.count) : 1
for name in litLights {
    guard let spec = lights[name] ?? lights[name.replacingOccurrences(of: ".", with: "_")], let parent = resolve(name) else { continue }
    let l = SCNLight()
    l.type = spec["type"] as? String == "spot" ? .spot : .omni
    l.color = srgb(spec["color_srgb"] as! [Double])
    l.intensity = CGFloat((spec["intensity"] as! Double) * rig.lumensPerCandela * share)
    l.attenuationStartDistance = 0
    let range = spec["range"] as? Double ?? 6
    l.attenuationEndDistance = CGFloat((range > 0 ? range : 6) * rig.rangeScale)
    l.attenuationFalloffExponent = 2
    if l.type == .spot { l.spotInnerAngle = CGFloat(spec["inner_deg"] as? Double ?? 18); l.spotOuterAngle = CGFloat(spec["outer_deg"] as? Double ?? 43) }
    let n = SCNNode(); n.light = l; parent.addChildNode(n)
}

// Studio rig, per addStudio + DollhouseDaylight.
let looks: [String: (UInt32, UInt32, UInt32, Double, UInt32, UInt32)] = [
    "morning": (0xFFE4C4, 0xFFD0A6, 0xFFD8B0, 34, 0xFFE6C8, 0xC4A07A),
    "midday": (0xFFF3E3, 0xFFF1DD, 0xF4F0E6, 22, 0xFFF3E3, 0x5B4636),
    "dusk": (0xFFD2A8, 0xFFC08A, 0xFFBE7A, 40, 0xFFC89A, 0x8A5A3A),
    "night": (0xE7ECF6, 0xD5DDEC, 0x9BB6E0, 46, 0x1C2433, 0x0E1218),
]
let look = looks[part]!
scene.background.contents = NSColor.clear
scene.lightingEnvironment.contents = gradient(look.4, look.5)
scene.lightingEnvironment.intensity = CGFloat(rig.envIntensity)
let views = row["views"] as! [String: Any]
let shot = views["dollhouse"] as! [String: Any]
let pos = (shot["position"] as! [Double]).map { Float($0) }, tgt = (shot["target"] as! [Double]).map { Float($0) }
let fov = shot["fov"] as! Double
let amb = SCNNode(); amb.light = SCNLight(); amb.light!.type = .ambient; amb.light!.color = hex(look.0); amb.light!.intensity = CGFloat(rig.ambient)
scene.rootNode.addChildNode(amb)
let key = SCNNode(); key.light = SCNLight(); key.light!.type = .directional; key.light!.color = hex(look.1); key.light!.intensity = CGFloat(rig.key)
key.light!.castsShadow = true; key.light!.shadowMode = .deferred; key.light!.shadowSampleCount = 16; key.light!.shadowRadius = 8
key.light!.shadowMapSize = CGSize(width: 2048, height: 2048); key.light!.shadowColor = NSColor(white: 0, alpha: CGFloat(rig.keyShadowAlpha))
key.light!.zNear = 0.5; key.light!.zFar = 80
key.light!.orthographicScale = Double(max(8, abs(pos[0] - tgt[0])))
key.position = SCNVector3(CGFloat(tgt[0] - 5), CGFloat(tgt[1] + 10), CGFloat(tgt[2] + 4))
key.look(at: SCNVector3(CGFloat(tgt[0]), CGFloat(tgt[1]), CGFloat(tgt[2])))
scene.rootNode.addChildNode(key)
let win = SCNNode(); win.light = SCNLight(); win.light!.type = .directional; win.light!.color = hex(look.2); win.light!.intensity = CGFloat(look.3 * rig.windowScale)
var origin: SCNVector3?
scene.rootNode.enumerateHierarchy { c, _ in
    guard origin == nil, let n = c.name?.lowercased() else { return }
    if (n.contains("window") && n.contains("glass")) || n == "skylight_diffuser" { origin = c.worldPosition }
}
win.position = origin ?? SCNVector3(CGFloat(tgt[0] + 4), CGFloat(tgt[1] + 3), CGFloat(tgt[2] + 2))
win.look(at: SCNVector3(CGFloat(tgt[0]), CGFloat(tgt[1]), CGFloat(tgt[2])))
scene.rootNode.addChildNode(win)

// Fitted camera, per fitted(_:).
var lo = SCNVector3(1e9, 1e9, 1e9), hi = SCNVector3(-1e9, -1e9, -1e9)
scene.rootNode.enumerateHierarchy { n, _ in
    guard n.geometry != nil, !n.isHidden else { return }
    let (p, q) = n.boundingBox
    for x in [p.x, q.x] { for y in [p.y, q.y] { for z in [p.z, q.z] {
        let w = n.convertPosition(SCNVector3(x, y, z), to: nil)
        lo = SCNVector3(min(lo.x, w.x), min(lo.y, w.y), min(lo.z, w.z)); hi = SCNVector3(max(hi.x, w.x), max(hi.y, w.y), max(hi.z, w.z))
    } } }
}
let center = SCNVector3((lo.x + hi.x) / 2, (lo.y + hi.y) / 2, (lo.z + hi.z) / 2)
let radius = sqrt(pow(hi.x - lo.x, 2) + pow(hi.y - lo.y, 2) + pow(hi.z - lo.z, 2)) / 2
var dir = SIMD3<Double>(Double(pos[0] - tgt[0]), Double(pos[1] - tgt[1]), Double(pos[2] - tgt[2]))
dir /= (dir * dir).sum().squareRoot()
let dist = Double(radius) / sin(fov * .pi / 360) * 0.92
let cam = SCNNode(); cam.camera = SCNCamera()
cam.camera!.zNear = 0.05; cam.camera!.zFar = 100; cam.camera!.fieldOfView = CGFloat(fov)
cam.camera!.wantsHDR = true; cam.camera!.wantsExposureAdaptation = false
cam.camera!.exposureOffset = CGFloat(rig.exposure)
cam.camera!.whitePoint = CGFloat(rig.whitePoint)
cam.camera!.bloomIntensity = CGFloat(rig.bloomIntensity); cam.camera!.bloomThreshold = CGFloat(rig.bloomThreshold); cam.camera!.bloomBlurRadius = 12
cam.position = SCNVector3(center.x + CGFloat(dir.x * dist), center.y + CGFloat(dir.y * dist), center.z + CGFloat(dir.z * dist))
cam.look(at: center)
scene.rootNode.addChildNode(cam)

let renderer = SCNRenderer(device: MTLCreateSystemDefaultDevice(), options: nil)
renderer.scene = scene
renderer.pointOfView = cam
renderer.autoenablesDefaultLighting = false
let size = CGSize(width: 900, height: 700)
_ = renderer.snapshot(atTime: 0, with: size, antialiasingMode: .multisampling4X)
let image = renderer.snapshot(atTime: 0.1, with: size, antialiasingMode: .multisampling4X)
var rect = CGRect(origin: .zero, size: image.size)
let cg = image.cgImage(forProposedRect: &rect, context: nil, hints: nil)!
let w = cg.width, h = cg.height
var px = [UInt8](repeating: 0, count: w * h * 4)
let ctx = CGContext(data: &px, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w * 4,
                    space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
ctx.draw(cg, in: CGRect(x: 0, y: 0, width: w, height: h))
var count = 0, sum = 0.0, white = 0, clip = 0
for i in stride(from: 0, to: px.count, by: 4) where px[i + 3] > 200 {
    let r = Double(px[i]), g = Double(px[i + 1]), b = Double(px[i + 2])
    let y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    count += 1; sum += y
    if y >= 235 { white += 1 }
    if max(r, g, b) >= 254 { clip += 1 }
}
// Over the app's dark background, for looking at.
let out = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0, space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
out.setFillColor(NSColor(srgbRed: 0.11, green: 0.11, blue: 0.12, alpha: 1).cgColor); out.fill(CGRect(x: 0, y: 0, width: w, height: h))
out.draw(cg, in: CGRect(x: 0, y: 0, width: w, height: h))
let rep = NSBitmapImageRep(cgImage: out.makeImage()!)
try rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: a[6]))
let n = Double(max(count, 1))
print(String(format: "%@ %@ %@ mean=%.1f nearwhite=%.1f%% clipped=%.1f%% px=%d", roomId, mode, part, sum / n, Double(white) / n * 100, Double(clip) / n * 100, count))
