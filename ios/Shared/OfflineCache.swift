import Foundation
import CryptoKit

/// The phone's saved copies of what the hub sent, so every screen opens at once and still works
/// with no connection.
///
/// Two kinds:
/// * **Recent**: every hub reply and picture, kept automatically and trimmed to a size limit.
/// * **Kept**: trips and the 3D house, kept until they're no longer needed. Downloaded films live
///   beside them (see `Downloads`).
///
/// Everything is stored per person, so a phone handed to someone else never shows the last
/// person's data. Files are encrypted while the phone is locked after its first unlock, and left out
/// of iCloud backups.
enum OfflineCache {
    enum Kind: String, CaseIterable { case recent, kept }

    enum Category: String, CaseIterable, Identifiable {
        case downloads, trips, cameras, house, home, pictures
        var id: String { rawValue }
        var title: String {
            switch self {
            case .downloads: "Downloads"
            case .trips: "Trips"
            case .cameras: "Camera pictures"
            case .house: "3D house"
            case .home: "Home, calendar, meals, lists"
            case .pictures: "Posters and photos"
            }
        }
    }

    /// Recent copies are trimmed (oldest first) above this size.
    static let recentLimit: Int64 = 250 * 1024 * 1024
    /// Camera pictures are dropped after a day.
    static let cameraMaxAge: TimeInterval = 24 * 3600

    private static let fm = FileManager.default
    private static let lock = NSLock()

    // MARK: where

    /// Whose copies these are. Set from `/v1/me`; changing it wipes the previous person's data.
    static var owner: String {
        get { AppSettings.defaults.string(forKey: "offlineOwner") ?? "device" }
        set { AppSettings.defaults.set(newValue, forKey: "offlineOwner") }
    }

    static var base: URL {
        let group = fm.containerURL(forSecurityApplicationGroupIdentifier: AppSettings.groupID)
            ?? fm.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        return group.appendingPathComponent("Library/Application Support/Offline", isDirectory: true)
    }

    static var root: URL { base.appendingPathComponent(safe(owner), isDirectory: true) }

    static func dir(_ kind: Kind, _ sub: String? = nil) -> URL {
        var u = root.appendingPathComponent(kind.rawValue, isDirectory: true)
        if let sub { u.appendPathComponent(sub, isDirectory: true) }
        ensure(u)
        return u
    }

    /// Called with the signed-in person's id. A different person means a different phone owner:
    /// forget everything that was saved for the last one.
    static func setOwner(_ id: String) {
        // A phone still waiting for approval has no person yet ("").
        guard !id.isEmpty, id != owner else { return }
        let old = root
        owner = id
        try? fm.removeItem(at: old)
    }

    static func wipeAll() {
        try? fm.removeItem(at: base)
        owner = "device"
    }

    /// Clears the automatic copies; keeps trips, the house model and downloads.
    static func clearRecent() {
        try? fm.removeItem(at: root.appendingPathComponent(Kind.recent.rawValue))
    }

    // MARK: hub replies

    /// Replies worth saving: reads of things a person looks at, not passes, streams or sign-in.
    static func cacheable(_ path: String) -> Bool {
        let skip = ["/v1/auth", "/v1/enroll", "/v1/media/downloads", "/v1/media/file", "/v1/lights/api/live",
                    "/pass", "/stream", "/download", "/v1/vpn", "/v1/web",
                    "/v1/remotes"]  // what's on the network right now: an old copy would offer TVs already added
        return path.hasPrefix("/v1/") && !skip.contains { path.contains($0) }
    }

    static func key(_ path: String, _ query: [String: String]) -> String {
        let q = query.sorted { $0.key < $1.key }.map { "\($0.key)=\($0.value)" }.joined(separator: "&")
        let digest = SHA256.hash(data: Data((path + "?" + q).utf8))
        return digest.prefix(16).map { String(format: "%02x", $0) }.joined()
    }

    static func category(for path: String) -> Category {
        if path.hasPrefix("/v1/trips") { return .trips }
        if path.hasPrefix("/v1/cameras") { return .cameras }
        if path.hasPrefix("/v1/assets/house") { return .house }
        return .home
    }

    static func store(_ data: Data, path: String, query: [String: String] = [:], keep: Bool = false) {
        let name = key(path, query) + ".json"
        let target = dir(keep ? .kept : .recent, category(for: path).rawValue).appendingPathComponent(name)
        write(data, to: target)
        // A reply that is now kept shouldn't also linger in recent, and vice versa.
        let other = dir(keep ? .recent : .kept, category(for: path).rawValue).appendingPathComponent(name)
        if !keep, fm.fileExists(atPath: other.path) {
            // Still kept: refresh the kept copy instead of making a second one.
            write(data, to: other); try? fm.removeItem(at: target)
        } else if keep { try? fm.removeItem(at: other) }
    }

    static func load(path: String, query: [String: String] = [:]) -> (data: Data, savedAt: Date)? {
        let name = key(path, query) + ".json"
        for kind in [Kind.kept, .recent] {
            let u = dir(kind, category(for: path).rawValue).appendingPathComponent(name)
            if let d = try? Data(contentsOf: u) { return (d, modified(u) ?? .distantPast) }
        }
        return nil
    }

    static func savedAt(path: String, query: [String: String] = [:]) -> Date? {
        let name = key(path, query) + ".json"
        for kind in [Kind.kept, .recent] {
            let u = dir(kind, category(for: path).rawValue).appendingPathComponent(name)
            if let m = modified(u) { return m }
        }
        return nil
    }

    /// Move a kept reply back to recent (e.g. a trip that ended over a week ago).
    static func unkeep(path: String, query: [String: String] = [:]) {
        let name = key(path, query) + ".json"
        let cat = category(for: path).rawValue
        let from = dir(.kept, cat).appendingPathComponent(name)
        guard fm.fileExists(atPath: from.path) else { return }
        let to = dir(.recent, cat).appendingPathComponent(name)
        try? fm.removeItem(at: to)
        try? fm.moveItem(at: from, to: to)
    }

    // MARK: files (pictures, map snapshots, the house model)

    static func fileURL(_ name: String, kind: Kind, category: Category) -> URL {
        dir(kind, category.rawValue).appendingPathComponent(safe(name))
    }

    static func storeFile(_ data: Data, name: String, kind: Kind, category: Category) {
        write(data, to: fileURL(name, kind: kind, category: category))
    }

    static func loadFile(_ name: String, kind: Kind, category: Category) -> Data? {
        try? Data(contentsOf: fileURL(name, kind: kind, category: category))
    }

    /// Pictures from the hub (posters, recipe photos, camera event pictures). Camera pictures are
    /// filed separately so they can be dropped after a day.
    static func imageURL(for path: String) -> URL {
        let cat: Category = path.contains("/cameras") ? .cameras : .pictures
        return dir(.recent, cat.rawValue).appendingPathComponent(key(path, [:]) + ".img")
    }

    static func storeImage(_ data: Data, path: String) {
        // Live camera frames carry a per-minute "m=" key; saving them would only fill the phone.
        if path.contains("?m=") || path.contains("&m=") { return }
        write(data, to: imageURL(for: path))
    }

    static func loadImage(path: String) -> Data? {
        let u = imageURL(for: path)
        if path.contains("/cameras"), let m = modified(u), Date.now.timeIntervalSince(m) > cameraMaxAge { return nil }
        return try? Data(contentsOf: u)
    }

    // MARK: housekeeping

    /// Drop old camera pictures, then the oldest recent copies until under the limit.
    static func trim() {
        lock.lock(); defer { lock.unlock() }
        let recent = root.appendingPathComponent(Kind.recent.rawValue)
        var files = allFiles(recent)
        let cams = recent.appendingPathComponent(Category.cameras.rawValue).path
        for f in files where f.url.path.hasPrefix(cams) && Date.now.timeIntervalSince(f.modified) > cameraMaxAge {
            try? fm.removeItem(at: f.url)
        }
        files = allFiles(recent).sorted { $0.modified < $1.modified }
        var total = files.reduce(Int64(0)) { $0 + $1.size }
        for f in files where total > recentLimit {
            try? fm.removeItem(at: f.url); total -= f.size
        }
    }

    struct Usage { var category: Category; var bytes: Int64; var newest: Date? }

    /// Bytes on disk per category (downloads included), for the Storage screen.
    static func usage() -> [Usage] {
        var bytes: [Category: Int64] = [:], newest: [Category: Date] = [:]
        for kind in Kind.allCases {
            for cat in Category.allCases {
                for f in allFiles(root.appendingPathComponent(kind.rawValue).appendingPathComponent(cat.rawValue)) {
                    bytes[cat, default: 0] += f.size
                    if f.modified > (newest[cat] ?? .distantPast) { newest[cat] = f.modified }
                }
            }
        }
        return Category.allCases.map { Usage(category: $0, bytes: bytes[$0] ?? 0, newest: newest[$0]) }
    }

    // MARK: plumbing

    static func write(_ data: Data, to url: URL) {
        ensure(url.deletingLastPathComponent())
        try? data.write(to: url, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
    }

    static func ensure(_ dir: URL) {
        guard !fm.fileExists(atPath: dir.path) else { return }
        try? fm.createDirectory(at: dir, withIntermediateDirectories: true,
                                attributes: [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication])
        var b = base
        var v = URLResourceValues(); v.isExcludedFromBackup = true
        try? b.setResourceValues(v)
    }

    static func modified(_ url: URL) -> Date? {
        (try? url.resourceValues(forKeys: [.contentModificationDateKey]))?.contentModificationDate
    }

    struct FileInfo { var url: URL; var size: Int64; var modified: Date }

    static func allFiles(_ dir: URL) -> [FileInfo] {
        guard let e = fm.enumerator(at: dir, includingPropertiesForKeys: [.fileSizeKey, .contentModificationDateKey, .isRegularFileKey]) else { return [] }
        var out: [FileInfo] = []
        for case let u as URL in e {
            guard let v = try? u.resourceValues(forKeys: [.fileSizeKey, .contentModificationDateKey, .isRegularFileKey]),
                  v.isRegularFile == true else { continue }
            out.append(FileInfo(url: u, size: Int64(v.fileSize ?? 0), modified: v.contentModificationDate ?? .distantPast))
        }
        return out
    }

    private static func safe(_ s: String) -> String {
        String(s.map { $0.isLetter || $0.isNumber || $0 == "-" || $0 == "_" || $0 == "." ? $0 : "_" })
    }
}
