import Foundation
import RoomPlan
import simd

/// Picks a color for every wall, floor, door, window, opening and object: project a few points
/// on it into the photos that see it best and take the per-channel median of 5x5 patches.
enum ColorSampler {
    static let method = "median sRGB of 5x5 patches around the center and +/-30% points, from the 3 most central photos per point"

    struct Target {
        let id: String
        let kind: String
        let category: String?
        let transform: simd_float4x4
        let dimensions: simd_float3
    }

    static func sample(rooms: [CapturedRoom], photos: [PixelSample]) -> [RoomColors] {
        rooms.enumerated().map { index, room in
            RoomColors(roomIndex: index, entries: targets(for: room).map { color(for: $0, photos: photos) })
        }
    }

    static func targets(for room: CapturedRoom) -> [Target] {
        var list: [Target] = []
        func add(_ surfaces: [CapturedRoom.Surface], _ kind: String) {
            for s in surfaces {
                list.append(Target(id: s.identifier.uuidString, kind: kind, category: nil,
                                   transform: s.transform, dimensions: s.dimensions))
            }
        }
        add(room.walls, "wall")
        add(room.floors, "floor")
        add(room.doors, "door")
        add(room.windows, "window")
        add(room.openings, "opening")
        for o in room.objects {
            list.append(Target(id: o.identifier.uuidString, kind: "object", category: categoryName(o.category),
                               transform: o.transform, dimensions: o.dimensions))
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

    static func color(for target: Target, photos: [PixelSample]) -> ColorEntry {
        var r: [UInt8] = [], g: [UInt8] = [], b: [UInt8] = []
        for point in samplePoints(target) {
            var hits: [(score: Float, photo: PixelSample, u: Float, v: Float)] = []
            for photo in photos {
                guard let p = photo.project(point) else { continue }
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
