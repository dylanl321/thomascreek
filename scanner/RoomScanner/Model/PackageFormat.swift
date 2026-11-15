import Foundation
import UIKit

/// Everything written into one scan package. Bump `currentSchemaVersion` when the layout changes.
enum PackageFormat {
    static let currentSchemaVersion = 1
    static let manifestFile = "manifest.json"
    static let photosFile = "photos.json"
    static let colorsFile = "colors.json"
    static let worldMapFile = "worldmap.arworldmap"
    static let structureFile = "structure.json"
    static let structureUSDZFile = "structure.usdz"
    static let thumbnailFile = "thumbnail.png"
    static let photosFolder = "photos"

    /// Room 1 uses the plain names; extra rooms from "Next room" get a numeric suffix.
    static func capturedRoomFile(index: Int) -> String { index == 0 ? "capturedroom.json" : "capturedroom_\(index + 1).json" }
    static func roomUSDZFile(index: Int) -> String { index == 0 ? "room.usdz" : "room_\(index + 1).usdz" }

    static var encoder: JSONEncoder {
        let e = JSONEncoder()
        e.outputFormatting = [.prettyPrinted, .sortedKeys]
        e.dateEncodingStrategy = .iso8601
        return e
    }

    static var decoder: JSONDecoder {
        let d = JSONDecoder()
        d.dateDecodingStrategy = .iso8601
        return d
    }
}

struct DeviceInfo: Codable, Hashable {
    var model: String
    var identifier: String
    var systemName: String
    var systemVersion: String

    @MainActor static var current: DeviceInfo {
        var info = utsname()
        uname(&info)
        let identifier = withUnsafeBytes(of: &info.machine) { raw in
            String(decoding: raw.prefix { $0 != 0 }, as: UTF8.self)
        }
        let device = UIDevice.current
        return DeviceInfo(model: device.model, identifier: identifier,
                          systemName: device.systemName, systemVersion: device.systemVersion)
    }
}

struct RoomEntry: Codable, Hashable {
    var index: Int
    var name: String
    var description: String
    var capturedRoomFile: String
    var usdzFile: String?
}

struct Manifest: Codable {
    var schemaVersion: Int
    var app: String
    var appVersion: String
    var id: String
    var roomName: String
    var description: String
    var createdAt: Date
    var device: DeviceInfo
    var units: String
    var coordinateSystem: String
    var rooms: [RoomEntry]
    var structureFile: String?
    var structureUSDZFile: String?
    var photosFile: String
    var photoCount: Int
    var colorsFile: String
    var worldMapFile: String?
    var thumbnailFile: String?
    /// Filled in by a future LLM review pass (see Review/RoomReviewer.swift).
    var reviewFile: String?
    var files: [String]
}

struct PhotoRecord: Codable, Hashable {
    var file: String
    var timestamp: Double
    var roomIndex: Int
    var imageWidth: Int
    var imageHeight: Int
    /// Camera-to-world 4x4, column-major (ARKit `camera.transform`).
    var transform: [Float]
    /// 3x3 pinhole intrinsics in pixels of the full image, column-major (ARKit `camera.intrinsics`).
    var intrinsics: [Float]
    var fx: Float
    var fy: Float
    var cx: Float
    var cy: Float
}

struct PhotosFile: Codable {
    var convention: String
    var photos: [PhotoRecord]
}

struct ColorEntry: Codable, Hashable, Identifiable {
    var id: String
    var kind: String
    var category: String?
    var hex: String?
    var sampleCount: Int
}

struct RoomColors: Codable, Hashable {
    var roomIndex: Int
    var entries: [ColorEntry]
}

struct ColorsFile: Codable {
    var method: String
    var colorSpace: String
    var rooms: [RoomColors]

    var byIdentifier: [String: String] {
        var map: [String: String] = [:]
        for room in rooms { for e in room.entries { if let hex = e.hex { map[e.id] = hex } } }
        return map
    }
}
