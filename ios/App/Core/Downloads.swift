import Foundation
import Observation
import UIKit

/// Films and episodes saved on this phone to watch with no connection (a plane, the car).
///
/// 1. The hub offers the original file (fastest; streamed straight from Plex or rewrapped to MP4)
///    or a version Plex converts to 720p or 480p.
/// 2. Once the hub has it ready, iOS downloads it in the background, over Wi-Fi unless the person
///    allowed cellular. Downloads carry on when the app is closed and resume after a dropped
///    connection.
/// 3. It plays in the app. Where it was stopped goes back to Plex when home answers again.
///
/// No approval step: anyone may download what they're allowed to watch (the hub checks; children
/// get rated titles only). Children have a per-device size limit.
@MainActor
@Observable
final class Downloads {
    static let shared = Downloads()

    /// Children's titles on one device may not add up to more than this.
    static let childLimit: Int64 = 20 * 1_000_000_000

    struct Option: Codable, Hashable, Identifiable {
        var quality: String
        var label: String
        var detail: String
        var bytes: Int64?
        var instant: Bool
        var id: String { quality }
    }
    struct OptionsReply: Codable { var item: MediaItem; var options: [Option] }

    struct Job: Codable {
        var id: String
        var status: String
        var progress: Double?
        var bytes: Int64?
        var error: String?
        var url: String?
    }

    struct Title: Codable, Identifiable, Hashable {
        enum State: String, Codable { case preparing, downloading, ready, failed }
        var id: String { "\(itemId)-\(quality)" }
        var itemId: String
        var quality: String
        var title: String
        var kind: String?
        var rating: String?
        var summary: String?
        var year: Int?
        var posterPath: String?
        var personId: String?
        var state: State = .preparing
        /// 0-1: the hub preparing it while `.preparing`, the phone downloading it while `.downloading`.
        var progress: Double = 0
        var bytes: Int64?
        var error: String?
        var jobId: String?
        var createdAt = Date()
        var finishedAt: Date?
        var durationS: Double?
        var positionS: Double = 0
        var watchedAt: Date?
        /// A grown-up sent it from the hub (`SendsSync`): it arrives and leaves by itself, and kids can't delete it.
        var sendId: String?
        var sentBy: String?
        var season: Int?
        var episode: Int?
        var show: String?

        var sent: Bool { sendId != nil }

        var fileName: String { "\(itemId)-\(quality).mp4" }
        var posterName: String { "\(itemId).poster" }
        var qualityLabel: String { ["original": "Original", "high": "Good", "small": "Small"][quality] ?? quality }
    }

    private(set) var titles: [Title] = []
    private var pollers: [String: Task<Void, Never>] = [:]
    @ObservationIgnored private lazy var delegate = DownloadDelegate()
    @ObservationIgnored private lazy var session: URLSession = {
        let c = URLSessionConfiguration.background(withIdentifier: Self.sessionID)
        c.sessionSendsLaunchEvents = true
        c.isDiscretionary = false
        c.allowsCellularAccess = true          // decided per request
        return URLSession(configuration: c, delegate: delegate, delegateQueue: nil)
    }()
    static let sessionID = "me.dlewis.thomascreek.downloads"
    /// Set by the app delegate when iOS wakes the app to finish background downloads.
    @ObservationIgnored var backgroundDone: (() -> Void)?

    init() { load() }

    // MARK: what this person sees

    /// This person's titles, newest first.
    var mine: [Title] {
        let me = HomeStore.shared.me?.person.id
        return titles.filter { $0.personId == nil || $0.personId == me }.sorted { $0.createdAt > $1.createdAt }
    }

    func title(for itemID: String) -> Title? { mine.first { $0.itemId == itemID } }

    var bytesUsed: Int64 { mine.reduce(0) { $0 + ($1.state == .ready ? fileSize($1) : 0) } }

    func fileURL(_ t: Title) -> URL { dir.appendingPathComponent(t.fileName) }
    func posterURL(_ t: Title) -> URL { dir.appendingPathComponent(t.posterName) }

    private var dir: URL { OfflineCache.dir(.kept, OfflineCache.Category.downloads.rawValue) }

    private func fileSize(_ t: Title) -> Int64 {
        Int64((try? fileURL(t).resourceValues(forKeys: [.fileSizeKey]))?.fileSize ?? 0)
    }

    // MARK: start

    func options(for item: MediaItem) async throws -> [Option] {
        let r: OptionsReply = try await HubClient.shared.get("/v1/media/\(item.id)/download")
        return r.options
    }

    /// Start one. Throws with a plain reason when it can't (too big for a child's limit, not allowed).
    func start(_ item: MediaItem, _ option: Option) async throws {
        let me = HomeStore.shared.me
        if me?.role == .child, let b = option.bytes, bytesUsed + b > Self.childLimit {
            throw HubClient.HubError.server(0, "That won't fit. Remove something you've watched first.")
        }
        if let old = title(for: item.id) { remove(old) }
        AppSettings.downloadQuality = option.quality
        var t = Title(itemId: item.id, quality: option.quality, title: item.title, kind: item.kind, rating: item.rating,
                      summary: item.summary, year: item.year, posterPath: item.posterUrl, personId: me?.person.id)
        t.bytes = option.bytes
        let job: Job = try await HubClient.shared.post("/v1/media/\(item.id)/download", ["quality": option.quality])
        t.jobId = job.id
        upsert(t)
        savePoster(t)
        apply(job, to: t.id)
    }

    private func apply(_ job: Job, to id: String) {
        guard var t = titles.first(where: { $0.id == id }) else { return }
        switch job.status {
        case "ready":
            guard let u = job.url else { return }
            t.bytes = job.bytes ?? t.bytes
            if t.state != .downloading { t.state = .downloading; t.progress = 0; upsert(t); Task { await download(t, path: u) } }
        case "failed":
            t.state = .failed; t.error = job.error ?? "The hub couldn't prepare it."; upsert(t)
        default:
            t.state = .preparing; t.progress = job.progress ?? t.progress; upsert(t)
            poll(t.id)
        }
    }

    /// Ask the hub how preparing is going, every few seconds, until it's ready.
    private func poll(_ id: String) {
        guard pollers[id] == nil else { return }
        pollers[id] = Task { [weak self] in
            defer { Task { @MainActor in self?.pollers[id] = nil } }
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(3))
                guard let self, let t = self.titles.first(where: { $0.id == id }), t.state == .preparing, let job = t.jobId else { return }
                do {
                    let j: Job = try await HubClient.shared.get("/v1/media/downloads/\(job)")
                    self.apply(j, to: id)
                    if j.status != "preparing" { return }
                } catch HubClient.HubError.server(404, _) {
                    // The hub restarted or the job expired: ask again.
                    if let item = self.item(t) { _ = try? await self.restart(t, item) }
                    return
                } catch { continue }
            }
        }
    }

    private func item(_ t: Title) -> MediaItem? {
        MediaItem(id: t.itemId, title: t.title, year: t.year, kind: t.kind, posterUrl: t.posterPath, rating: t.rating, summary: t.summary)
    }

    private func restart(_ t: Title, _ item: MediaItem) async throws {
        let job: Job = try await HubClient.shared.post("/v1/media/\(item.id)/download", ["quality": t.quality])
        var n = t; n.jobId = job.id; n.state = .preparing; upsert(n)
        apply(job, to: n.id)
    }

    private func download(_ t: Title, path: String) async {
        guard let url = await HubClient.shared.resolve(path) else { return }
        var req = URLRequest(url: url)
        req.allowsCellularAccess = AppSettings.downloadOnCellular
        req.allowsExpensiveNetworkAccess = AppSettings.downloadOnCellular
        let task = session.downloadTask(with: req)
        task.taskDescription = t.id
        task.countOfBytesClientExpectsToReceive = t.bytes ?? NSURLSessionTransferSizeUnknown
        task.resume()
    }

    private func savePoster(_ t: Title) {
        guard let p = t.posterPath else { return }
        Task {
            if let d = OfflineCache.loadImage(path: p) { OfflineCache.write(d, to: posterURL(t)); return }
            guard let req = try? await HubClient.shared.authorizedRequest(p),
                  let reply = try? await HubClient.session.data(for: req) else { return }
            OfflineCache.write(reply.0, to: posterURL(t))
        }
    }

    /// A title a grown-up sent to this person's devices. Same flow as `start`, without asking or limits.
    func startSent(_ s: SendsSync.Item) async throws {
        var t = Title(itemId: s.itemId, quality: s.quality, title: s.title ?? "Untitled", kind: s.kind, rating: s.rating,
                      summary: s.summary, year: s.year, posterPath: s.posterUrl, personId: HomeStore.shared.me?.person.id)
        t.sendId = s.sendId; t.sentBy = s.from; t.season = s.season; t.episode = s.episode; t.show = s.show
        t.durationS = s.durationS.map(Double.init)
        let job: Job = try await HubClient.shared.post("/v1/media/\(s.itemId)/download", ["quality": s.quality])
        t.jobId = job.id
        upsert(t)
        savePoster(t)
        apply(job, to: t.id)
    }

    /// Mark a title this person downloaded themselves as also sent (so it stays while the send wants it).
    func adopt(_ t: Title, send: SendsSync.Item) {
        guard var n = titles.first(where: { $0.id == t.id }), n.sendId != send.sendId else { return }
        n.sendId = send.sendId; n.sentBy = send.from
        upsert(n)
    }

    // MARK: background session callbacks (from DownloadDelegate)

    func progressed(_ id: String, _ fraction: Double) {
        guard var t = titles.first(where: { $0.id == id }), t.state == .downloading else { return }
        if fraction - t.progress >= 0.01 || fraction >= 1 { t.progress = fraction; upsert(t, persist: false) }
    }

    func finished(_ id: String, file: URL?, error: String?) {
        guard var t = titles.first(where: { $0.id == id }) else { return }
        if let file {
            let dest = fileURL(t)
            try? FileManager.default.removeItem(at: dest)
            OfflineCache.ensure(dest.deletingLastPathComponent())
            do {
                try FileManager.default.moveItem(at: file, to: dest)
                try? (dest as NSURL).setResourceValue(URLFileProtection.completeUntilFirstUserAuthentication, forKey: .fileProtectionKey)
                t.state = .ready; t.progress = 1; t.finishedAt = .now; t.error = nil
                t.bytes = fileSize(t)
                upsert(t)
                if t.sent { SendsSync.shared.arrived(t) } else { HomeStore.shared.show("Downloaded", t.title) }
            } catch {
                t.state = .failed; t.error = "There wasn't room to save it."; upsert(t)
            }
        } else if let error {
            // Not finished: leave it to resume() unless the link expired for good.
            t.state = .failed; t.error = error; upsert(t)
        }
    }

    // MARK: lifecycle

    /// On launch and when home comes back: pick up anything unfinished.
    func resume() {
        _ = session  // reconnect to downloads iOS kept going while the app was closed
        session.getAllTasks { tasks in
            let running = Set(tasks.filter { $0.state == .running || $0.state == .suspended }.compactMap(\.taskDescription))
            Task { @MainActor in
                for t in self.titles {
                    switch t.state {
                    case .preparing: self.poll(t.id)
                    case .downloading where !running.contains(t.id):
                        if let job = t.jobId, let j: Job = try? await HubClient.shared.get("/v1/media/downloads/\(job)") {
                            var n = t; n.state = .preparing; self.upsert(n); self.apply(j, to: t.id)
                        } else if let item = self.item(t) { try? await self.restart(t, item) }
                    default: break
                    }
                }
                self.removeOldWatched()
            }
        }
    }

    func retry(_ t: Title) {
        guard let item = item(t) else { return }
        Task { do { try await restart(t, item) } catch { HomeStore.shared.show(error.localizedDescription, isError: true) } }
    }

    func remove(_ t: Title) {
        session.getAllTasks { tasks in tasks.filter { $0.taskDescription == t.id }.forEach { $0.cancel() } }
        pollers[t.id]?.cancel(); pollers[t.id] = nil
        try? FileManager.default.removeItem(at: fileURL(t))
        if !titles.contains(where: { $0.itemId == t.itemId && $0.id != t.id }) { try? FileManager.default.removeItem(at: posterURL(t)) }
        titles.removeAll { $0.id == t.id }
        save()
    }

    /// The phone changed hands or was removed from the house.
    func forgetAll() {
        for t in titles { remove(t) }
        titles = []; save()
        Outbox.shared.reset()
    }

    private func removeOldWatched() {
        guard AppSettings.removeWatchedDownloads else { return }
        // Sent titles come and go with the hub's lineup instead.
        for t in titles where !t.sent && t.watchedAt.map({ Date.now.timeIntervalSince($0) > 7 * 86400 }) == true { remove(t) }
    }

    // MARK: playing

    /// Where the player stopped. Saved on the phone and passed to Plex (now, or when home is back).
    func played(_ t: Title, at seconds: Double, duration: Double?) {
        guard var n = titles.first(where: { $0.id == t.id }) else { return }
        n.positionS = seconds
        if let duration, duration > 0 { n.durationS = duration }
        let watched = (n.durationS ?? 0) > 0 && seconds >= (n.durationS ?? 0) * 0.95
        if watched { n.watchedAt = n.watchedAt ?? .now; n.positionS = 0 }
        upsert(n)
        Outbox.shared.enqueue("/v1/media/\(t.itemId)/progress",
                              ["offset_s": seconds, "duration_s": n.durationS ?? 0, "watched": watched], label: nil)
        if HomeStore.shared.reachable { Task { await Outbox.shared.flush() } }
    }

    // MARK: storage

    private var stateFile: URL { OfflineCache.root.appendingPathComponent("downloads.json") }

    private func upsert(_ t: Title, persist: Bool = true) {
        if let i = titles.firstIndex(where: { $0.id == t.id }) { titles[i] = t } else { titles.append(t) }
        if persist { save(); SendsSync.shared.reportSoon() }
    }

    private func load() {
        titles = (try? Data(contentsOf: stateFile)).flatMap { try? JSONDecoder().decode([Title].self, from: $0) } ?? []
    }

    private func save() {
        if let d = try? JSONEncoder().encode(titles) { OfflineCache.write(d, to: stateFile) }
    }
}

/// The background session's delegate: checks the hub's pinned certificate and hands results to
/// `Downloads` on the main actor. The downloaded file must be moved before this returns.
final class DownloadDelegate: NSObject, URLSessionDownloadDelegate, @unchecked Sendable {
    func urlSession(_ session: URLSession, didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        let (d, c) = HubPinning.evaluate(challenge)
        completionHandler(d, c)
    }

    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didWriteData bytesWritten: Int64,
                    totalBytesWritten: Int64, totalBytesExpectedToWrite: Int64) {
        guard let id = downloadTask.taskDescription else { return }
        let expected = totalBytesExpectedToWrite > 0 ? totalBytesExpectedToWrite : downloadTask.countOfBytesClientExpectsToReceive
        guard expected > 0 else { return }
        let f = min(1, Double(totalBytesWritten) / Double(expected))
        Task { @MainActor in Downloads.shared.progressed(id, f) }
    }

    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didFinishDownloadingTo location: URL) {
        guard let id = downloadTask.taskDescription else { return }
        let code = (downloadTask.response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(code) else {
            Task { @MainActor in Downloads.shared.finished(id, file: nil, error: "Home said no (\(code)). Try again.") }
            return
        }
        // Move it out of the temporary spot now; iOS deletes it when this returns.
        let hold = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".mp4")
        do { try FileManager.default.moveItem(at: location, to: hold) } catch { return }
        Task { @MainActor in Downloads.shared.finished(id, file: hold, error: nil) }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        guard let error, let id = task.taskDescription else { return }
        let ns = error as NSError
        if ns.code == NSURLErrorCancelled { return }
        // Lost connection: resume() picks it up again when home is back.
        let msg = ns.code == NSURLErrorNotConnectedToInternet || ns.code == NSURLErrorNetworkConnectionLost
            ? nil : "The download stopped. Tap to try again."
        Task { @MainActor in
            if let msg { Downloads.shared.finished(id, file: nil, error: msg) }
        }
    }

    func urlSessionDidFinishEvents(forBackgroundURLSession session: URLSession) {
        Task { @MainActor in Downloads.shared.backgroundDone?(); Downloads.shared.backgroundDone = nil }
    }
}
