import Metal
import RoomPlan
import SceneKit
import UIKit
import simd

/// A quick cartoony dollhouse built straight from RoomPlan: thick walls (cut down on the camera
/// side), floors, doors and windows as panels, objects as rounded boxes in their sampled colors.
enum DollhouseScene {
    struct Built {
        let scene: SCNScene
        let camera: SCNNode
        let center: SCNVector3
    }

    /// The default camera looks from this side; walls facing it are cut down so you can see in.
    static let viewDirection = simd_normalize(simd_float3(0.55, 0, 1))
    static let cutHeight: Float = 0.35
    static let wallThickness: CGFloat = 0.12

    static func build(rooms: [CapturedRoom], colors: [String: String]) -> Built {
        let scene = SCNScene()
        scene.background.contents = UIColor(red: 0.93, green: 0.95, blue: 0.98, alpha: 1)
        let root = scene.rootNode

        let walls = rooms.flatMap(\.walls)
        let objects = rooms.flatMap(\.objects)

        // Align the base to the first wall so it isn't skewed in world space.
        var yaw: Float = 0
        if let first = walls.first {
            let ax = simd_make_float3(first.transform.columns.0)
            yaw = atan2(-ax.z, ax.x)
        }
        var points: [simd_float3] = []
        for w in walls {
            let c = position(w.transform)
            let half = simd_make_float3(w.transform.columns.0) * w.dimensions.x / 2
            let up = simd_float3(0, w.dimensions.y / 2, 0)
            points += [c + half - up, c - half + up]
        }
        for o in objects {
            let c = position(o.transform)
            let up = simd_float3(0, o.dimensions.y / 2, 0)
            points += [c - up, c + up]
        }
        if points.isEmpty { points = [simd_float3(-1, 0, -1), simd_float3(1, 2.4, 1)] }

        var minL = simd_float3(repeating: .greatestFiniteMagnitude)
        var maxL = simd_float3(repeating: -.greatestFiniteMagnitude)
        for p in points {
            let l = toLocal(p, yaw: yaw)
            minL = simd_min(minL, l)
            maxL = simd_max(maxL, l)
        }
        let centerLocal = (minL + maxL) / 2
        let center = toWorld(centerLocal, yaw: yaw)
        let floorY = minL.y
        let size = maxL - minL
        let radius = max(size.x, size.z, 2)

        // Base slab.
        let slab = SCNBox(width: CGFloat(size.x + 0.6), height: 0.16, length: CGFloat(size.z + 0.6), chamferRadius: 0.06)
        slab.materials = [material(UIColor(red: 0.97, green: 0.94, blue: 0.88, alpha: 1))]
        let slabNode = SCNNode(geometry: slab)
        slabNode.simdPosition = simd_float3(center.x, floorY - 0.1, center.z)
        slabNode.eulerAngles.y = yaw
        root.addChildNode(slabNode)

        func isCameraSide(_ p: simd_float3) -> Bool {
            var outward = p - center
            outward.y = 0
            guard simd_length(outward) > 0.05 else { return false }
            return simd_dot(simd_normalize(outward), viewDirection) > 0.3
        }

        for room in rooms {
            for floor in room.floors {
                let floorColor = color(colors[floor.identifier.uuidString], fallback: "#C8A27A")
                // L-shaped and other non-rectangular floors come with their outline; draw that shape.
                let node = polygonFloor(floor, color: floorColor)
                    ?? panel(floor.transform, floor.dimensions, thickness: 0.05, color: floorColor)
                root.addChildNode(node)
            }
            for wall in room.walls {
                root.addChildNode(wallNode(wall, cut: isCameraSide(position(wall.transform)),
                                           color: color(colors[wall.identifier.uuidString], fallback: "#EDE6DA")))
            }
            for door in room.doors where !isCameraSide(position(door.transform)) {
                root.addChildNode(panel(door.transform, door.dimensions, thickness: 0.16,
                                        color: color(colors[door.identifier.uuidString], fallback: "#8B5E3C")))
            }
            for window in room.windows where !isCameraSide(position(window.transform)) {
                let node = panel(window.transform, window.dimensions, thickness: 0.15,
                                 color: UIColor(red: 0.62, green: 0.82, blue: 0.97, alpha: 1))
                node.childNodes.first?.geometry?.firstMaterial?.transparency = 0.55
                root.addChildNode(node)
            }
            for object in room.objects {
                root.addChildNode(objectNode(object, color: color(colors[object.identifier.uuidString],
                                                                  fallback: fallbackHex(object.category))))
            }
        }

        // Soft lighting.
        let ambient = SCNNode()
        ambient.light = SCNLight()
        ambient.light?.type = .ambient
        ambient.light?.intensity = 550
        ambient.light?.color = UIColor(red: 1, green: 0.97, blue: 0.92, alpha: 1)
        root.addChildNode(ambient)

        let sun = SCNNode()
        sun.light = SCNLight()
        sun.light?.type = .directional
        sun.light?.intensity = 750
        sun.light?.castsShadow = true
        sun.light?.shadowMode = .deferred
        sun.light?.shadowColor = UIColor(white: 0, alpha: 0.22)
        sun.light?.shadowRadius = 6
        sun.light?.shadowSampleCount = 8
        sun.light?.orthographicScale = CGFloat(radius * 1.2)
        sun.simdPosition = center + simd_float3(0, 6, 0)
        sun.eulerAngles = SCNVector3(-1.05, 0.5, 0)
        root.addChildNode(sun)

        // Camera above the cut-away side.
        let camera = SCNNode()
        camera.camera = SCNCamera()
        camera.camera?.fieldOfView = 40
        camera.camera?.zNear = 0.05
        camera.camera?.zFar = 200
        camera.simdPosition = center + viewDirection * radius * 1.35 + simd_float3(0, radius * 1.15, 0)
        camera.simdLook(at: center)
        root.addChildNode(camera)

        return Built(scene: scene, camera: camera, center: SCNVector3(center.x, center.y, center.z))
    }

    /// Offscreen render for the home-screen list.
    static func thumbnail(rooms: [CapturedRoom], colors: [String: String]) -> UIImage? {
        let built = build(rooms: rooms, colors: colors)
        let renderer = SCNRenderer(device: MTLCreateSystemDefaultDevice(), options: nil)
        renderer.scene = built.scene
        renderer.pointOfView = built.camera
        return renderer.snapshot(atTime: 0, with: CGSize(width: 640, height: 480), antialiasingMode: .multisampling4X)
    }

    // MARK: Pieces

    static func wallNode(_ wall: CapturedRoom.Surface, cut: Bool, color: UIColor) -> SCNNode {
        let fullHeight = wall.dimensions.y
        let height = cut ? min(cutHeight, fullHeight) : fullHeight
        let holder = SCNNode()
        holder.simdTransform = wall.transform
        let box = SCNBox(width: CGFloat(wall.dimensions.x) + wallThickness, height: CGFloat(height),
                         length: wallThickness, chamferRadius: 0.015)
        box.materials = [material(color)]
        let body = SCNNode(geometry: box)
        body.simdPosition = simd_float3(0, -(fullHeight - height) / 2, 0)
        holder.addChildNode(body)
        // Darker cap on top, like a cut section.
        let cap = SCNBox(width: box.width + 0.01, height: 0.03, length: wallThickness + 0.01, chamferRadius: 0.01)
        cap.materials = [material(UIColor(red: 0.35, green: 0.33, blue: 0.36, alpha: 1))]
        let capNode = SCNNode(geometry: cap)
        capNode.simdPosition = simd_float3(0, -fullHeight / 2 + height, 0)
        holder.addChildNode(capNode)
        return holder
    }

    /// A floor drawn from RoomPlan's polygon corners (floor-local XY plane, local Z up), or nil.
    static func polygonFloor(_ floor: CapturedRoom.Surface, color: UIColor) -> SCNNode? {
        let corners = floor.polygonCorners
        guard corners.count >= 3 else { return nil }
        let path = UIBezierPath()
        path.move(to: CGPoint(x: CGFloat(corners[0].x), y: CGFloat(corners[0].y)))
        for c in corners.dropFirst() { path.addLine(to: CGPoint(x: CGFloat(c.x), y: CGFloat(c.y))) }
        path.close()
        path.flatness = 0.01
        let shape = SCNShape(path: path, extrusionDepth: 0.05)
        shape.materials = [material(color)]
        let holder = SCNNode()
        holder.simdTransform = floor.transform
        let body = SCNNode(geometry: shape)
        body.simdPosition = simd_float3(0, 0, -0.025)   // top face at floor level
        holder.addChildNode(body)
        return holder
    }

    static func panel(_ transform: simd_float4x4, _ dims: simd_float3, thickness: Float, color: UIColor) -> SCNNode {
        let holder = SCNNode()
        holder.simdTransform = transform
        let d = simd_float3(dims.x < 0.01 ? thickness : dims.x,
                            dims.y < 0.01 ? thickness : dims.y,
                            dims.z < 0.01 ? thickness : dims.z)
        let box = SCNBox(width: CGFloat(d.x), height: CGFloat(d.y), length: CGFloat(d.z), chamferRadius: 0.01)
        box.materials = [material(color)]
        holder.addChildNode(SCNNode(geometry: box))
        return holder
    }

    static func objectNode(_ object: CapturedRoom.Object, color: UIColor) -> SCNNode {
        let d = object.dimensions
        let chamfer = CGFloat(min(min(d.x, d.y, d.z) * 0.15, 0.08))
        let box = SCNBox(width: CGFloat(d.x), height: CGFloat(d.y), length: CGFloat(d.z), chamferRadius: chamfer)
        box.materials = [material(color)]
        let node = SCNNode(geometry: box)
        node.simdTransform = object.transform
        return node
    }

    static func material(_ color: UIColor) -> SCNMaterial {
        let m = SCNMaterial()
        m.lightingModel = .blinn
        m.diffuse.contents = color
        m.specular.contents = UIColor(white: 0.12, alpha: 1)
        m.shininess = 0.2
        return m
    }

    /// Sampled photo colors, softened a little toward white for the toy look.
    static func color(_ hex: String?, fallback: String) -> UIColor {
        let base = UIColor(hex: hex ?? fallback) ?? UIColor(hex: fallback) ?? .lightGray
        var r: CGFloat = 0, g: CGFloat = 0, b: CGFloat = 0, a: CGFloat = 0
        base.getRed(&r, green: &g, blue: &b, alpha: &a)
        let t: CGFloat = 0.12
        return UIColor(red: r + (1 - r) * t, green: g + (1 - g) * t, blue: b + (1 - b) * t, alpha: 1)
    }

    static func fallbackHex(_ category: CapturedRoom.Object.Category) -> String {
        switch category {
        case .sofa, .chair: return "#7C9CBF"
        case .bed: return "#E8D7C3"
        case .table: return "#A47551"
        case .storage: return "#B9A38B"
        case .television: return "#2F2F35"
        case .fireplace: return "#9C8F84"
        case .refrigerator, .stove, .oven, .dishwasher, .washerDryer: return "#D9DDE1"
        case .sink, .toilet, .bathtub: return "#F4F6F8"
        case .stairs: return "#B08D6A"
        @unknown default: return "#C9C2B8"
        }
    }

    static func position(_ m: simd_float4x4) -> simd_float3 { simd_make_float3(m.columns.3) }

    static func toLocal(_ p: simd_float3, yaw: Float) -> simd_float3 {
        simd_float3(p.x * cos(yaw) - p.z * sin(yaw), p.y, p.x * sin(yaw) + p.z * cos(yaw))
    }

    static func toWorld(_ l: simd_float3, yaw: Float) -> simd_float3 {
        simd_float3(l.x * cos(yaw) + l.z * sin(yaw), l.y, -l.x * sin(yaw) + l.z * cos(yaw))
    }
}

extension UIColor {
    convenience init?(hex: String) {
        var s = hex.trimmingCharacters(in: .whitespaces)
        if s.hasPrefix("#") { s.removeFirst() }
        guard s.count == 6, let v = UInt32(s, radix: 16) else { return nil }
        self.init(red: CGFloat((v >> 16) & 0xFF) / 255, green: CGFloat((v >> 8) & 0xFF) / 255,
                  blue: CGFloat(v & 0xFF) / 255, alpha: 1)
    }
}
