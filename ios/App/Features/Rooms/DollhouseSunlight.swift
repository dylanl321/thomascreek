import Foundation
import SceneKit

/// Real sunlight through a room's windows. Optional per room (`"sun"` in `Rooms/catalog.json`).
///
/// The sun's direction comes from the house's latitude and longitude and the clock (`SolarPosition`), turned into the
/// model's frame with `bearing_x`, the compass bearing of the model's +X axis (the way its window wall faces outward).
/// One shadow-casting directional light plays the sun. Glass lets it through; walls, frames and muntins block it, so
/// patches of light land on the floor, the furniture and the counters. The dollhouse is a cutaway (no ceiling, front
/// walls cut low), so an invisible shell closes the room for the sun's shadows only: it is never drawn and lights
/// nothing. Clouds and rain fade the sun. Night, or a sun on the far side of the house, turns it off.
///
/// SceneKit and Foundation only, so `ios/scripts/dollhouse_snapshot.swift` can render it offscreen on a Mac.
struct DollhouseSunSpec: Decodable, Hashable {
    /// Compass bearing (degrees clockwise from north) of the model's +X axis.
    var bearingX: Double
    /// Window and glass-door groups. Inside each, nodes named `*glass*` or `*pane*` let the sun through.
    var windows: [String]
    /// The room's floors. Their footprint (with the windows) sizes the invisible shell.
    var floors: [String]
    /// Real walls that hold the windows. Their height is the shell's ceiling.
    var walls: [String]
}

/// What the weather says about the sun. Latitude and longitude are the house's, rounded by the hub.
struct DollhouseSunWeather: Hashable {
    var latitude: Double
    var longitude: Double
    /// Cloud cover, 0 to 100. Nil is treated as clear.
    var clouds: Double?
    /// The hub's SF Symbol for the current weather; rain, snow, fog and storms dim the sun further.
    var symbol: String?
}

enum SolarPosition {
    /// NOAA's solar position equations. Azimuth is degrees clockwise from north, elevation degrees above the
    /// horizon (no refraction). Checked against astral to 0.01 degrees.
    static func at(_ date: Date, latitude: Double, longitude: Double) -> (azimuth: Double, elevation: Double) {
        let rad = Double.pi / 180
        let seconds = date.timeIntervalSince1970
        let jd = seconds / 86400 + 2440587.5
        let t = (jd - 2451545) / 36525
        let l0 = (280.46646 + t * (36000.76983 + t * 0.0003032)).truncatingRemainder(dividingBy: 360)
        let m = 357.52911 + t * (35999.05029 - 0.0001537 * t)
        let e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
        let c = sin(m * rad) * (1.914602 - t * (0.004817 + 0.000014 * t))
            + sin(2 * m * rad) * (0.019993 - 0.000101 * t) + sin(3 * m * rad) * 0.000289
        let omega = 125.04 - 1934.136 * t
        let lambda = l0 + c - 0.00569 - 0.00478 * sin(omega * rad)
        let eps0 = 23 + (26 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60) / 60
        let eps = eps0 + 0.00256 * cos(omega * rad)
        let decl = asin(sin(eps * rad) * sin(lambda * rad))
        let y = pow(tan(eps * rad / 2), 2)
        let eot = 4 / rad * (y * sin(2 * l0 * rad) - 2 * e * sin(m * rad) + 4 * e * y * sin(m * rad) * cos(2 * l0 * rad)
            - 0.5 * y * y * sin(4 * l0 * rad) - 1.25 * e * e * sin(2 * m * rad))
        var minutes = seconds.truncatingRemainder(dividingBy: 86400) / 60
        if minutes < 0 { minutes += 1440 }
        var solarTime = (minutes + eot + 4 * longitude).truncatingRemainder(dividingBy: 1440)
        if solarTime < 0 { solarTime += 1440 }
        let hourAngle = (solarTime / 4 - 180) * rad
        let lat = latitude * rad
        let cosZenith = sin(lat) * sin(decl) + cos(lat) * cos(decl) * cos(hourAngle)
        let elevation = 90 - acos(max(-1, min(1, cosZenith))) / rad
        var azimuth = atan2(sin(hourAngle), cos(hourAngle) * sin(lat) - tan(decl) * cos(lat)) / rad + 180
        azimuth = azimuth.truncatingRemainder(dividingBy: 360)
        return (azimuth, elevation)
    }

    /// Unit vector toward the sun in a model whose +X axis points at compass bearing `bearingX` (Y up).
    static func toSun(azimuth: Double, elevation: Double, bearingX: Double) -> SIMD3<Double> {
        let rad = Double.pi / 180
        let rel = (azimuth - bearingX) * rad
        let h = elevation * rad
        return SIMD3(cos(h) * cos(rel), sin(h), cos(h) * sin(rel))
    }

    /// 0...1: how much direct sun gets through. Low sun fades in over the first 6 degrees; clouds and wet or
    /// foggy weather take most of it away.
    static func strength(elevation: Double, clouds: Double?, symbol: String?) -> Double {
        let rise = smoothstep(0.5, 6, elevation)
        let cover = max(0, min(100, clouds ?? 0)) / 100
        var sky = 1 - 0.88 * pow(cover, 1.3)
        let s = symbol ?? ""
        if ["rain", "drizzle", "snow", "sleet", "hail", "bolt", "fog"].contains(where: s.contains) { sky *= 0.2 }
        return max(0, min(1, rise * sky))
    }

    /// Warm near the horizon, near-white high up. sRGB 0...1.
    static func color(elevation: Double) -> (r: Double, g: Double, b: Double) {
        let t = smoothstep(2, 32, elevation)
        let low = (1.0, 0.66, 0.38), high = (1.0, 0.95, 0.86)
        return (low.0 + (high.0 - low.0) * t, low.1 + (high.1 - low.1) * t, low.2 + (high.2 - low.2) * t)
    }

    static func smoothstep(_ a: Double, _ b: Double, _ x: Double) -> Double {
        let t = max(0, min(1, (x - a) / (b - a)))
        return t * t * (3 - 2 * t)
    }
}

final class DollhouseSunlight {
    /// The shell's category. Cameras leave it out, so it is never drawn; lights other than the sun leave it out too.
    static let shellCategory = 1 << 5
    /// Lux-like SceneKit intensity at full sun. The studio key is 420; sun patches read clearly brighter.
    static let peak: Double = 3500

    let light: SCNNode
    private let spec: DollhouseSunSpec
    private let center: SIMD3<Float>
    private let radius: Float

    /// Nil when the room's model has none of the listed floors.
    init?(scene: SCNScene, spec: DollhouseSunSpec) {
        self.spec = spec
        let root = scene.rootNode
        func named(_ name: String) -> SCNNode? { root.childNode(withName: name, recursively: true) }

        let floors = spec.floors.compactMap(named).compactMap(Self.worldBox)
        guard var box = floors.first else { return nil }
        for b in floors.dropFirst() { box = box.union(b) }
        let windows = spec.windows.compactMap(named)
        var openings = box
        for w in windows {
            if let b = Self.worldBox(w) { openings = openings.union(b) }
            // Glass must not block the sun.
            w.enumerateHierarchy { node, _ in
                let n = node.name?.lowercased() ?? ""
                if n.contains("glass") || n.contains("pane") {
                    node.castsShadow = false
                    node.enumerateHierarchy { child, _ in child.castsShadow = false }
                }
            }
        }
        let wallTop = spec.walls.compactMap(named).compactMap(Self.worldBox).map(\.max.y).max() ?? (box.max.y + 2.7)
        let floorY = box.max.y

        // Which sides hold windows: those are left to the real walls.
        let windowBoxes = windows.compactMap(Self.worldBox)
        func open(_ side: Side) -> Bool {
            windowBoxes.contains { w in
                // A window sits in a wall: thin across it, near that side of the floor.
                let acrossX = (w.max.x - w.min.x) < (w.max.z - w.min.z)
                switch side {
                case .minX: return acrossX && w.min.x < box.min.x + 0.5
                case .maxX: return acrossX && w.max.x > box.max.x - 0.5
                case .minZ: return !acrossX && w.min.z < box.min.z + 0.5
                case .maxZ: return !acrossX && w.max.z > box.max.z - 0.5
                }
            }
        }
        let shell = SCNNode()
        shell.name = "dollhouse-sun-shell"
        let pad: Float = 0.3, thick: Float = 0.1
        let x0 = min(box.min.x, openings.min.x) - pad, x1 = max(box.max.x, openings.max.x) + pad
        let z0 = min(box.min.z, openings.min.z) - pad, z1 = max(box.max.z, openings.max.z) + pad
        let y0 = floorY - 0.4, y1 = wallTop + 0.02
        func slab(_ ax0: Float, _ ax1: Float, _ ay0: Float, _ ay1: Float, _ az0: Float, _ az1: Float) {
            let geo = SCNBox(width: CGFloat(ax1 - ax0), height: CGFloat(ay1 - ay0), length: CGFloat(az1 - az0), chamferRadius: 0)
            let mat = SCNMaterial()
            mat.lightingModel = .constant
            // Cameras skip the shell's category, so it is never drawn. It must write depth, or SceneKit leaves
            // it out of the sun's shadow map too.
            mat.colorBufferWriteMask = []
            geo.materials = [mat]
            let node = SCNNode(geometry: geo)
            node.simdPosition = SIMD3((ax0 + ax1) / 2, (ay0 + ay1) / 2, (az0 + az1) / 2)
            node.categoryBitMask = Self.shellCategory
            node.castsShadow = true
            shell.addChildNode(node)
        }
        slab(x0, x1, y1, y1 + thick, z0, z1)                       // ceiling
        if !open(.minX) { slab(x0 - thick, x0, y0, y1, z0, z1) }
        if !open(.maxX) { slab(x1, x1 + thick, y0, y1, z0, z1) }
        if !open(.minZ) { slab(x0, x1, y0, y1, z0 - thick, z0) }
        if !open(.maxZ) { slab(x0, x1, y0, y1, z1, z1 + thick) }
        root.addChildNode(shell)

        center = SIMD3((x0 + x1) / 2, floorY + (y1 - floorY) / 3, (z0 + z1) / 2)
        let dx = x1 - x0, dz = z1 - z0, dy = y1 - y0
        radius = sqrt(dx * dx + dz * dz + dy * dy) / 2

        let sun = SCNLight()
        sun.type = .directional
        sun.intensity = 0
        sun.castsShadow = true
        // Forward, not deferred: a deferred shadow darkens the whole frame (studio light included) wherever the
        // sun is blocked, which turned every room black. Forward only takes away the sun's own light.
        sun.shadowMode = .forward
        sun.shadowSampleCount = 16
        sun.shadowRadius = 1.5
        sun.shadowMapSize = CGSize(width: 2048, height: 2048)
        sun.shadowColor = CGColor(gray: 0, alpha: 1)
        sun.shadowBias = 2
        sun.orthographicScale = CGFloat(radius * 1.05)
        sun.zNear = 1
        sun.zFar = CGFloat(radius * 2 + 40)
        let node = SCNNode()
        node.name = "dollhouse-sun"
        node.light = sun
        node.isHidden = true
        root.addChildNode(node)
        light = node
    }

    /// Points the sun and sets its strength and colour. Nil weather (no house location yet) leaves it off.
    func update(weather: DollhouseSunWeather?, at date: Date = Date(), animated: Bool) {
        guard let weather else { setOff(animated: animated); return }
        let pos = SolarPosition.at(date, latitude: weather.latitude, longitude: weather.longitude)
        let strength = SolarPosition.strength(elevation: pos.elevation, clouds: weather.clouds, symbol: weather.symbol)
        guard strength > 0.02 else { setOff(animated: animated); return }
        let dir = SolarPosition.toSun(azimuth: pos.azimuth, elevation: pos.elevation, bearingX: spec.bearingX)
        let distance = Double(radius) + 20
        let rgb = SolarPosition.color(elevation: pos.elevation)

        SCNTransaction.begin()
        SCNTransaction.animationDuration = animated ? 1.5 : 0
        light.isHidden = false
        light.simdPosition = center + SIMD3<Float>(dir * distance)
        light.simdLook(at: center, up: SIMD3(0, 1, 0), localFront: SIMD3(0, 0, -1))
        light.light?.color = CGColor(srgbRed: rgb.r, green: rgb.g, blue: rgb.b, alpha: 1)
        light.light?.intensity = CGFloat(Self.peak * strength)
        SCNTransaction.commit()
    }

    private func setOff(animated: Bool) {
        SCNTransaction.begin()
        SCNTransaction.animationDuration = animated ? 1.5 : 0
        SCNTransaction.completionBlock = { [weak self] in
            if (self?.light.light?.intensity ?? 0) < 1 { self?.light.isHidden = true }
        }
        light.light?.intensity = 0
        SCNTransaction.commit()
    }

    private enum Side { case minX, maxX, minZ, maxZ }

    struct Box {
        var min: SIMD3<Float>
        var max: SIMD3<Float>
        func union(_ o: Box) -> Box { Box(min: simd_min(min, o.min), max: simd_max(max, o.max)) }
    }

    /// World-space bounds of every geometry under `node`.
    static func worldBox(_ node: SCNNode) -> Box? {
        var out: Box?
        node.enumerateHierarchy { n, _ in
            guard n.geometry != nil else { return }
            let (lo, hi) = n.boundingBox
            let m = n.simdWorldTransform
            for i in 0..<8 {
                let p = SIMD4<Float>(i & 1 == 0 ? Float(lo.x) : Float(hi.x), i & 2 == 0 ? Float(lo.y) : Float(hi.y),
                                     i & 4 == 0 ? Float(lo.z) : Float(hi.z), 1)
                let w = m * p
                let v = SIMD3(w.x, w.y, w.z)
                out = out.map { Box(min: simd_min($0.min, v), max: simd_max($0.max, v)) } ?? Box(min: v, max: v)
            }
        }
        return out
    }
}
