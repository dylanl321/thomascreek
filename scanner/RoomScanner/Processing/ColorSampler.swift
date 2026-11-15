import Foundation
import RoomPlan
import simd

/// Picks a color for every wall, floor, door, window, opening and object: project a few points
/// on it into the photos that see it best and take the per-channel median of 5x5 patches.
///
/// A photo only counts for a point when it actually sees that point: a wall or floor must face the
/// camera from the room's side (so a shared wall never takes the other room's color), and nothing
/// RoomPlan knows about (furniture, other walls) may sit between the camera and the point (so a wall
/// behind the couch doesn't come out couch-colored).
enum ColorSampler {
    static let method = "median sRGB of 5x5 patches around the center and +/-30% points, from the 3 most central "
        + "photos per point that face the surface from the room side and aren't blocked by furniture or walls"

    struct Target {
        let id: String
        let kind: String
        let category: String?
        let transform: simd_float4x4
        let dimensions: simd_float3
        /// Surfaces: the wall or floor this belongs to (excluded from its own occlusion test).
        let parentID: String?
        /// Surfaces: a point inside the room, to tell the room side of the surface from the back.
        let roomCenter: simd_float3
    }

    /// An oriented box something could hide behind.
    struct Occluder {
        let id: String
        let inverse: simd_float4x4
        let half: simd_float3
    }

    static func sample(rooms: [CapturedRoom], photos: [PixelSample]) -> [RoomColors] {
        let occluders = rooms.flatMap(occluders(for:))
        return rooms.enumerated().map { index, room in
            RoomColors(roomIndex: index, entries: targets(for: room).map { color(for: $0, photos: photos, occluders: occluders) })
        }
    }

    static func roomCenter(_ room: CapturedRoom) -> simd_float3 {
        let surfaces = room.floors.isEmpty ? room.walls : room.floors
        guard !surfaces.isEmpty else { return .zero }
        var sum = simd_float3.zero
        for s in surfaces { sum += simd_make_float3(s.transform.columns.3) }
        var c = sum / Float(surfaces.count)
        if let floor = room.floors.first { c.y = floor.transform.columns.3.y + 1.0 }
        return c
    }

    static func targets(for room: CapturedRoom) -> [Target] {
        let center = roomCenter(room)
        var list: [Target] = []
        func add(_ surfaces: [CapturedRoom.Surface], _ kind: String) {
            for s in surfaces {
                list.append(Target(id: s.identifier.uuidString, kind: kind, category: nil,
                                   transform: s.transform, dimensions: s.dimensions,
                                   parentID: s.parentIdentifier?.uuidString, roomCenter: center))
            }
        }
        add(room.walls, "wall")
        add(room.floors, "floor")
        add(room.doors, "door")
        add(room.windows, "window")
        add(room.openings, "opening")
        for o in room.objects {
            list.append(Target(id: o.identifier.uuidString, kind: "object", category: categoryName(o.category),
                               transform: o.transform, dimensions: o.dimensions, parentID: nil, roomCenter: center))
        }
        return list
    }

    static func occluders(for room: CapturedRoom) -> [Occluder] {
        var list: [Occluder] = []
        for o in room.objects {
            list.append(Occluder(id: o.identifier.uuidString, inverse: o.transform.inverse, half: o.dimensions / 2))
        }
        for w in room.walls {
            // Walls are planes in RoomPlan; give them a little thickness so a ray can't slip through.
            var half = w.dimensions / 2
            half.z = max(half.z, 0.05)
            list.append(Occluder(id: w.identifier.uuidString, inverse: w.transform.inverse, half: half))
        }
        return list
    }

    static func categoryName(_ category: CapturedRoom.Object.Category) -> String {
        String(describing: category)
    }

    static func samplePoints(_ t: Target) -> [simd_float3] {
        let m = t.transform
        let center = simd_make_float3(m.columns.3)
        let axes = [simd_make_float3(m.columns.0), simd_make_float3(m.columns.1), simd_make_float3(m.columns.2)]
        let lengths = [t.dimensions.x, t.dimensions.y, t.dimensions.z]
        var points = [center]
        for (axis, length) in zip(axes, lengths) where length > 0.05 {
            points.append(center + axis * length * 0.3)
            points.append(center - axis * length * 0.3)
        }
        if t.kind == "object" { points.append(center + axes[1] * t.dimensions.y * 0.45) }  // top face
        return points
    }

    /// True when the camera looks at the surface's room-side face (or the target isn't a surface).
    static func facesCamera(_ t: Target, point: simd_float3, camera: simd_float3) -> Bool {
        guard t.kind != "object" else { return true }
        let normal = simd_normalize(simd_make_float3(t.transform.columns.2))
        let roomSide = simd_dot(normal, t.roomCenter - point)
        let cameraSide = simd_dot(normal, camera - point)
        let distance = simd_length(camera - point)
        // Same side as the room, and not a grazing view (less than ~6 degrees off the surface).
        return roomSide * cameraSide > 0 && abs(cameraSide) > 0.1 * distance
    }

    /// True when a box other than the target (and its parent wall) sits between camera and point.
    static func blocked(_ t: Target, point: simd_float3, camera: simd_float3, occluders: [Occluder]) -> Bool {
        let length = simd_length(point - camera)
        guard length > 0.1 else { return false }
        let limit = length - 0.08    // ignore hits right at the point itself
        for o in occluders where o.id != t.id && o.id != t.parentID {
            let origin = simd_make_float3(o.inverse * simd_float4(camera, 1))
            let target = simd_make_float3(o.inverse * simd_float4(point, 1))
            if let hit = rayBox(origin: origin, direction: (target - origin) / length, half: o.half), hit > 0.05, hit < limit {
                return true
            }
        }
        return false
    }

    /// Distance along a unit ray to an axis-aligned box centred on the origin, or nil.
    static func rayBox(origin: simd_float3, direction: simd_float3, half: simd_float3) -> Float? {
        var tMin: Float = -.greatestFiniteMagnitude
        var tMax: Float = .greatestFiniteMagnitude
        for i in 0..<3 {
            let o = origin[i], d = direction[i], h = half[i]
            if abs(d) < 1e-6 {
                if o < -h || o > h { return nil }
                continue
            }
            var t0 = (-h - o) / d
            var t1 = (h - o) / d
            if t0 > t1 { swap(&t0, &t1) }
            tMin = max(tMin, t0)
            tMax = min(tMax, t1)
            if tMin > tMax { return nil }
        }
        if tMax < 0 { return nil }
        return tMin >= 0 ? tMin : tMax
    }

    static func color(for target: Target, photos: [PixelSample], occluders: [Occluder]) -> ColorEntry {
        var r: [UInt8] = [], g: [UInt8] = [], b: [UInt8] = []
        for point in samplePoints(target) {
            var hits: [(score: Float, photo: PixelSample, u: Float, v: Float)] = []
            for photo in photos {
                let camera = simd_make_float3(photo.transform.columns.3)
                guard facesCamera(target, point: point, camera: camera),
                      let p = photo.project(point),
                      !blocked(target, point: point, camera: camera, occluders: occluders) else { continue }
                let dx = p.u - Float(photo.width) / 2, dy = p.v - Float(photo.height) / 2
                let maxR = hypot(Float(photo.width) / 2, Float(photo.height) / 2)
                let centrality = 1 - hypot(dx, dy) / maxR
                hits.append((centrality / (1 + 0.25 * p.depth), photo, p.u, p.v))
            }
            for hit in hits.sorted(by: { $0.score > $1.score }).prefix(3) {
                hit.photo.collect(u: hit.u, v: hit.v, into: &r, &g, &b)
            }
        }
        let hex = r.isEmpty ? nil : String(format: "#%02X%02X%02X", median(r), median(g), median(b))
        return ColorEntry(id: target.id, kind: target.kind, category: target.category, hex: hex, sampleCount: r.count)
    }

    private static func median(_ values: [UInt8]) -> UInt8 {
        values.sorted()[values.count / 2]
    }
}
