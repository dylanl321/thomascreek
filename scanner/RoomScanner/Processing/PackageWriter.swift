import ARKit
import Foundation
import RoomPlan
import SceneKit
import UIKit

enum PackageError: LocalizedError {
    case noRooms
    var errorDescription: String? { "The scan didn't produce a room." }
}

/// Writes one scan into Documents/Rooms/<id>/ and zips it to Documents/Exports/<id>.zip.
///
///   manifest.json        schema version, names, description, device, units, file list
///   capturedroom.json    RoomPlan CapturedRoom (Codable); capturedroom_2.json... for extra rooms
///   room.usdz            RoomPlan parametric USDZ; room_2.usdz... for extra rooms
///   structure.json/.usdz multi-room CapturedStructure (only with "Next room")
///   photos/*.jpg         auto-captured frames; photos.json has transform + intrinsics per photo
///   colors.json          sampled hex color per wall/floor/door/window/opening/object id
///   worldmap.arworldmap  ARWorldMap for relocalizing future scans
///   thumbnail.png        dollhouse preview render
enum PackageWriter {
    @MainActor
    static func write(_ output: ScanOutput, library: RoomLibrary) async throws -> ScanSummary {
        guard !output.rooms.isEmpty else { throw PackageError.noRooms }
        let createdAt = Date()
        let names = output.descriptions.map(RoomLibrary.roomName(from:))
        let mainName = names.first ?? "Room"
        let id = RoomLibrary.folderName(for: mainName, date: createdAt)
        let folder = library.roomsDir.appendingPathComponent(id, isDirectory: true)
        let zip = library.exportsDir.appendingPathComponent("\(id).zip")
        do {
            return try await writePackage(output, library: library, id: id, folder: folder, zip: zip,
                                          names: names, mainName: mainName, createdAt: createdAt)
        } catch {
            // Don't leave a half-written scan behind: without a manifest it wouldn't even be listed.
            try? FileManager.default.removeItem(at: folder)
            try? FileManager.default.removeItem(at: zip)
            library.reload()
            throw error
        }
    }

    @MainActor
    private static func writePackage(_ output: ScanOutput, library: RoomLibrary, id: String, folder: URL, zip: URL,
                                     names: [String], mainName: String, createdAt: Date) async throws -> ScanSummary {
        let fm = FileManager.default
        try fm.createDirectory(at: folder, withIntermediateDirectories: true)
        let encoder = PackageFormat.encoder

        // Rooms: JSON + parametric USDZ.
        var entries: [RoomEntry] = []
        for (i, room) in output.rooms.enumerated() {
            let jsonName = PackageFormat.capturedRoomFile(index: i)
            try encoder.encode(room).write(to: folder.appendingPathComponent(jsonName))
            let usdzName = PackageFormat.roomUSDZFile(index: i)
            var usdz: String? = usdzName
            do { try room.export(to: folder.appendingPathComponent(usdzName), exportOptions: .parametric) } catch { usdz = nil }
            entries.append(RoomEntry(index: i, name: i < names.count ? names[i] : "Room \(i + 1)",
                                     description: i < output.descriptions.count ? output.descriptions[i] : "",
                                     capturedRoomFile: jsonName, usdzFile: usdz))
        }

        // Multi-room structure (optional).
        var structureFile: String?
        var structureUSDZ: String?
        if output.rooms.count > 1 {
            do {
                let structure = try await StructureBuilder(options: [.beautifyObjects]).capturedStructure(from: output.rooms)
                try encoder.encode(structure).write(to: folder.appendingPathComponent(PackageFormat.structureFile))
                structureFile = PackageFormat.structureFile
                try structure.export(to: folder.appendingPathComponent(PackageFormat.structureUSDZFile))
                structureUSDZ = PackageFormat.structureUSDZFile
            } catch {
                print("StructureBuilder failed: \(error)")
            }
        }

        // Photos.
        let photosDir = folder.appendingPathComponent(PackageFormat.photosFolder, isDirectory: true)
        try fm.createDirectory(at: photosDir, withIntermediateDirectories: true)
        var records: [PhotoRecord] = []
        for photo in output.photos {
            let source = output.photoDirectory.appendingPathComponent((photo.record.file as NSString).lastPathComponent)
            let dest = folder.appendingPathComponent(photo.record.file)
            if (try? fm.moveItem(at: source, to: dest)) != nil { records.append(photo.record) }
        }
        let photosFile = PhotosFile(
            convention: "ARKit: transform is camera-to-world (column-major 4x4, meters); camera looks down -Z with +Y up; "
                + "intrinsics are column-major 3x3 in pixels of the full-size, unrotated (landscape) JPEG.",
            photos: records)
        try encoder.encode(photosFile).write(to: folder.appendingPathComponent(PackageFormat.photosFile))

        // Colors.
        let rooms = output.rooms
        let samples = output.photos.map(\.sample)
        let roomColors = await Task.detached(priority: .userInitiated) {
            ColorSampler.sample(rooms: rooms, photos: samples)
        }.value
        let colors = ColorsFile(method: ColorSampler.method, colorSpace: "sRGB", rooms: roomColors)
        try encoder.encode(colors).write(to: folder.appendingPathComponent(PackageFormat.colorsFile))

        // World map.
        var worldMapFile: String?
        if let map = output.worldMap,
           let data = try? NSKeyedArchiver.archivedData(withRootObject: map, requiringSecureCoding: true) {
            try data.write(to: folder.appendingPathComponent(PackageFormat.worldMapFile))
            worldMapFile = PackageFormat.worldMapFile
        }

        // Thumbnail.
        var thumbnailFile: String?
        if let png = DollhouseScene.thumbnail(rooms: output.rooms, colors: colors.byIdentifier)?.pngData() {
            try png.write(to: folder.appendingPathComponent(PackageFormat.thumbnailFile))
            thumbnailFile = PackageFormat.thumbnailFile
        }

        // Manifest (lists every file, itself included).
        var files = listFiles(in: folder)
        files.append(PackageFormat.manifestFile)
        let manifest = Manifest(
            schemaVersion: PackageFormat.currentSchemaVersion,
            app: "Room Scanner",
            appVersion: appVersion,
            id: id,
            roomName: mainName,
            description: output.descriptions.first ?? "",
            createdAt: createdAt,
            device: DeviceInfo.current,
            units: "meters",
            coordinateSystem: "RoomPlan/ARKit world space: right-handed, +Y up (gravity aligned), origin where the session started",
            rooms: entries,
            structureFile: structureFile,
            structureUSDZFile: structureUSDZ,
            photosFile: PackageFormat.photosFile,
            photoCount: records.count,
            colorsFile: PackageFormat.colorsFile,
            worldMapFile: worldMapFile,
            thumbnailFile: thumbnailFile,
            reviewFile: nil,
            areas: output.areas.isEmpty ? nil : output.areas,
            files: files.sorted())
        try encoder.encode(manifest).write(to: folder.appendingPathComponent(PackageFormat.manifestFile))

        try? fm.removeItem(at: output.photoDirectory.deletingLastPathComponent())

        // Zipping a few dozen photos takes a moment; keep it off the main thread so the spinner moves.
        try await Task.detached(priority: .userInitiated) { try zipFolder(folder, to: zip) }.value
        library.reload()
        guard let summary = library.summary(for: folder) else { throw PackageError.noRooms }
        return summary
    }

    static var appVersion: String {
        let info = Bundle.main.infoDictionary
        let version = info?["CFBundleShortVersionString"] as? String ?? "?"
        let build = info?["CFBundleVersion"] as? String ?? "?"
        return "\(version) (\(build))"
    }

    static func listFiles(in folder: URL) -> [String] {
        guard let e = FileManager.default.enumerator(at: folder, includingPropertiesForKeys: [.isRegularFileKey]) else { return [] }
        let base = folder.standardizedFileURL.path + "/"
        var files: [String] = []
        for case let url as URL in e {
            guard (try? url.resourceValues(forKeys: [.isRegularFileKey]).isRegularFile) == true else { continue }
            files.append(url.standardizedFileURL.path.replacingOccurrences(of: base, with: ""))
        }
        return files
    }

    /// NSFileCoordinator's .forUploading option hands back a zip of a folder.
    static func zipFolder(_ folder: URL, to destination: URL) throws {
        var coordinatorError: NSError?
        var copyError: Error?
        NSFileCoordinator().coordinate(readingItemAt: folder, options: [.forUploading], error: &coordinatorError) { zipURL in
            do {
                if FileManager.default.fileExists(atPath: destination.path) {
                    try FileManager.default.removeItem(at: destination)
                }
                try FileManager.default.copyItem(at: zipURL, to: destination)
            } catch {
                copyError = error
            }
        }
        if let error = coordinatorError ?? copyError { throw error }
    }
}

/// A saved scan loaded back for the preview screen.
struct LoadedScan {
    var rooms: [CapturedRoom]
    var colors: ColorsFile?
    var manifest: Manifest

    static func load(_ scan: ScanSummary) throws -> LoadedScan {
        let decoder = PackageFormat.decoder
        let manifest = try decoder.decode(Manifest.self, from: Data(contentsOf: scan.folder.appendingPathComponent(PackageFormat.manifestFile)))
        let rooms = try manifest.rooms.map {
            try decoder.decode(CapturedRoom.self, from: Data(contentsOf: scan.folder.appendingPathComponent($0.capturedRoomFile)))
        }
        let colors = try? decoder.decode(ColorsFile.self, from: Data(contentsOf: scan.folder.appendingPathComponent(manifest.colorsFile)))
        return LoadedScan(rooms: rooms, colors: colors, manifest: manifest)
    }
}
