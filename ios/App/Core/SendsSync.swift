import Foundation
import BackgroundTasks
import Observation
import UIKit
import UserNotifications

/// Films, shows and YouTube a grown-up sent to this person's devices (usually a kid's iPad).
///
/// The hub keeps a *lineup*: exactly what this person's devices should hold (`GET /v1/sends/mine`).
/// This device downloads what it's missing through the normal `Downloads` flow (the hub has already
/// prepared the files, so iOS only has to fetch them), deletes sent titles that dropped out (a watched
/// episode when the next one comes, an old channel video when a new one arrives, a send a grown-up
/// removed), and tells the hub what it holds so grown-ups can see it (`POST /v1/sends/inventory`).
///
/// It runs when the app opens, when the hub sends a silent push, and in iOS background refresh.
/// Apple rations silent pushes and doesn't deliver them to an app that was swiped away, so the other
/// two make sure a send arrives at the latest the next time the app is opened.
@MainActor
@Observable
final class SendsSync {
    static let shared = SendsSync()
    static let refreshTask = "me.dlewis.thomascreek.sends"

    struct Item: Codable, Hashable, Identifiable {
        var itemId: String
        var title: String?
        var kind: String?
        var posterUrl: String?
        var rating: String?
        var summary: String?
        var year: Int?
        var show: String?
        var season: Int?
        var episode: Int?
        var durationS: Int?
        var ready: Bool
        var sendId: String
        var quality: String
        var from: String?
        var watched: Bool?
        var id: String { itemId }
    }
    struct Lineup: Codable { var items: [Item]; var active: Bool }

    /// What the hub last said this person should have.
    private(set) var lineup: [Item] = []
    private(set) var lastSync: Date?
    @ObservationIgnored private var running = false
    @ObservationIgnored private var reportTask: Task<Void, Never>?

    // MARK: syncing

    /// Fetch the lineup, start what's missing, remove what's no longer wanted, report back.
    @discardableResult
    func sync() async -> Bool {
        guard !running, HomeStore.shared.me?.role != .pending, AppSettings.deviceID != nil else { return false }
        running = true
        defer { running = false }
        // Not the cached GET: an old saved copy must never decide what gets deleted.
        guard let l: Lineup = try? await HubClient.shared.call("GET", "/v1/sends/mine") else { return false }
        lineup = l.items
        lastSync = .now
        let downloads = Downloads.shared
        let wanted = Set(l.items.map(\.itemId))
        var changed = false
        for item in l.items where item.ready {
            if let have = downloads.title(for: item.itemId) {
                if have.state == .failed, have.sent { downloads.retry(have) }
                else if !have.sent { downloads.adopt(have, send: item) }
                continue
            }
            do { try await downloads.startSent(item); changed = true }
            catch { continue }  // the hub will still list it next time
        }
        for t in downloads.mine where t.sent && !wanted.contains(t.itemId) {
            downloads.remove(t)
            changed = true
        }
        await report()
        return changed
    }

    /// A sent title finished downloading: a quiet note on the device ("New from Mom").
    func arrived(_ t: Downloads.Title) {
        let c = UNMutableNotificationContent()
        c.title = t.sentBy.map { "New from \($0)" } ?? "New to watch"
        c.body = "\(t.title) is ready to watch."
        c.interruptionLevel = .passive
        c.threadIdentifier = "sends"
        c.userInfo = ["ref": "thomascreek://downloads"]
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: "send-\(t.id)", content: c, trigger: nil),
                                               withCompletionHandler: nil)
        if UIApplication.shared.applicationState == .active { HomeStore.shared.show("New to watch", t.title) }
    }

    // MARK: reporting what this device holds

    /// Debounced: downloads change state often (progress), the hub only needs the gist.
    func reportSoon() {
        reportTask?.cancel()
        reportTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(4))
            guard !Task.isCancelled else { return }
            await self?.report()
        }
    }

    func report() async {
        guard HomeStore.shared.reachable, AppSettings.deviceID != nil else { return }
        let downloads = Downloads.shared
        let items: [[String: Any]] = downloads.mine.map { t in
            var d: [String: Any] = ["item_id": t.itemId, "title": t.title, "quality": t.quality, "state": t.state.rawValue,
                                    "progress": t.progress, "watched": t.watchedAt != nil, "sent": t.sent]
            if let b = t.bytes { d["bytes"] = b }
            if let e = t.error { d["error"] = String(e.prefix(190)) }
            return d
        }
        var body: [String: Any] = ["items": items]
        let attrs = try? URL(fileURLWithPath: NSHomeDirectory()).resourceValues(
            forKeys: [.volumeAvailableCapacityForImportantUsageKey, .volumeTotalCapacityKey])
        if let free = attrs?.volumeAvailableCapacityForImportantUsage { body["free_bytes"] = free }
        if let total = attrs?.volumeTotalCapacity { body["total_bytes"] = total }
        try? await HubClient.shared.send("/v1/sends/inventory", body)
    }

    // MARK: waking up in the background

    /// Called from `application(_:didFinishLaunchingWithOptions:)`; must happen before launch finishes.
    nonisolated static func registerBackgroundRefresh() {
        BGTaskScheduler.shared.register(forTaskWithIdentifier: refreshTask, using: nil) { task in
            guard let task = task as? BGAppRefreshTask else { task.setTaskCompleted(success: false); return }
            scheduleRefresh()
            let work = Task { @MainActor in
                let ok = await SendsSync.shared.sync()
                task.setTaskCompleted(success: ok)
            }
            task.expirationHandler = { work.cancel() }
        }
    }

    /// Ask iOS for a background check in about an hour (it decides when; usually while charging on Wi-Fi).
    nonisolated static func scheduleRefresh() {
        let r = BGAppRefreshTaskRequest(identifier: refreshTask)
        r.earliestBeginDate = Date(timeIntervalSinceNow: 3600)
        try? BGTaskScheduler.shared.submit(r)
    }

    /// The hub's silent push (`cat: sends`).
    func handlePush() async -> UIBackgroundFetchResult {
        let changed = await sync()
        return changed ? .newData : .noData
    }
}
