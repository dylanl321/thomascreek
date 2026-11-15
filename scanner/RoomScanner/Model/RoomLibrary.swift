import Foundation

/// One saved scan, as listed on the home screen.
struct ScanSummary: Identifiable, Hashable {
    var id: String
    var folder: URL
    var name: String
    var description: String
    var createdAt: Date
    var roomCount: Int
    var photoCount: Int
    var thumbnailURL: URL?
    var zipURL: URL?
}

/// Scans live in Documents/Rooms/<id>/ (unzipped) and Documents/Exports/<id>.zip.
/// Documents is shared with the Files app (UIFileSharingEnabled + LSSupportsOpeningDocumentsInPlace).
final class RoomLibrary: ObservableObject {
    @Published private(set) var scans: [ScanSummary] = []

    let documents: URL
    var roomsDir: URL { documents.appendingPathComponent("Rooms", isDirectory: true) }
    var exportsDir: URL { documents.appendingPathComponent("Exports", isDirectory: true) }

    init() {
        documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        try? FileManager.default.createDirectory(at: roomsDir, withIntermediateDirectories: true)
        try? FileManager.default.createDirectory(at: exportsDir, withIntermediateDirectories: true)
        reload()
    }

    func reload() {
        let fm = FileManager.default
        let folders = (try? fm.contentsOfDirectory(at: roomsDir, includingPropertiesForKeys: nil)) ?? []
        scans = folders.compactMap(summary(for:)).sorted { $0.createdAt > $1.createdAt }
    }

    func summary(for folder: URL) -> ScanSummary? {
        let url = folder.appendingPathComponent(PackageFormat.manifestFile)
        guard let data = try? Data(contentsOf: url),
              let manifest = try? PackageFormat.decoder.decode(Manifest.self, from: data) else { return nil }
        let id = folder.lastPathComponent
        let thumb = manifest.thumbnailFile.map { folder.appendingPathComponent($0) }
        let zip = exportsDir.appendingPathComponent("\(id).zip")
        return ScanSummary(id: id, folder: folder, name: manifest.roomName, description: manifest.description,
                           createdAt: manifest.createdAt, roomCount: manifest.rooms.count,
                           photoCount: manifest.photoCount, thumbnailURL: thumb,
                           zipURL: FileManager.default.fileExists(atPath: zip.path) ? zip : nil)
    }

    func delete(_ scan: ScanSummary) {
        try? FileManager.default.removeItem(at: scan.folder)
        if let zip = scan.zipURL { try? FileManager.default.removeItem(at: zip) }
        reload()
    }

    /// "Guest bath, through the door on the left wall of the bedroom" -> "Guest Bath".
    static func roomName(from text: String) -> String {
        var name = text.trimmingCharacters(in: .whitespacesAndNewlines)
        for separator in [",", ".", ";", ":", " - ", " — ", "\n"] {
            if let r = name.range(of: separator) { name = String(name[..<r.lowerBound]) }
        }
        for word in [" through ", " off ", " next to ", " behind ", " connect", " by ", " via ", " from ", " past ", " across ", " beside "] {
            if let r = name.range(of: word, options: .caseInsensitive) { name = String(name[..<r.lowerBound]) }
        }
        let prefixes = ["the next room is the ", "next room is the ", "next room is ", "this room is the ",
                        "this is the ", "this is ", "now the ", "the "]
        for prefix in prefixes where name.lowercased().hasPrefix(prefix) {
            name = String(name.dropFirst(prefix.count))
            break
        }
        name = name.trimmingCharacters(in: .whitespacesAndNewlines)
        if name.isEmpty { return "Room" }
        return String(titleCased(name).prefix(40))
    }

    /// "tv room" -> "Tv Room" with `capitalized`; this keeps words already typed in capitals ("TV Room").
    static func titleCased(_ text: String) -> String {
        text.split(separator: " ", omittingEmptySubsequences: true).map { word -> String in
            if word.contains(where: { $0.isUppercase }) { return String(word) }
            return word.prefix(1).uppercased() + word.dropFirst()
        }.joined(separator: " ")
    }

    static func folderName(for name: String, date: Date) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyyMMdd-HHmmss"
        let slug = name.lowercased().map { $0.isLetter || $0.isNumber ? $0 : "-" }
        let compact = String(slug).split(separator: "-").joined(separator: "-")
        return "\(f.string(from: date))-\(compact.isEmpty ? "room" : compact)"
    }
}
