import SwiftUI
import Observation

/// Everything the screens show: who this phone belongs to, the live house snapshot and the inbox.
/// Live changes arrive over the hub's event stream; the last snapshot is cached for a fast, honest start.
@MainActor
@Observable
final class HomeStore {
    static let shared = HomeStore()

    enum Phase: Equatable {
        case launching
        case findingHome          // not enrolled yet, looking for the hub on home Wi-Fi
        case enrolling
        case pending              // enrolled, waiting for Dylan to say whose phone this is
        case ready
        case failed(String)
    }

    var phase: Phase = .launching
    var me: Me? = AppSettings.cachedMe
    var home: HomeSnapshot? = AppSettings.cachedHome
    var inbox: [InboxItem] = []
    /// Newest Frigate detections across all cameras (Home and the Cameras section).
    var recentEvents: [CameraEvent]?
    var recentEventsError: String?
    var lastUpdate: Date?
    var reachable = true
    var unreachableSince: Date?
    var streamConnected = false
    var toast: Toast?
    var askParent: AskParent?

    struct Toast: Equatable, Identifiable { let id = UUID(); var title: String; var detail: String? = nil; var isError = false }
    struct AskParent: Identifiable { let id = UUID(); var what: String; var kind: String = "other" }

    /// True when the data on screen is current.
    var isLive: Bool { reachable && streamConnected }
    var role: Role { me?.role ?? .pending }
    var inboxCount: Int { inbox.filter { $0.acked != true && ($0.bucket == .actNow || $0.bucket == .decision) }.count }

    private var stream: EventStream?
    private var offlineStarted = false
    private var poller: Task<Void, Never>?

    // MARK: lifecycle

    func start() async {
        Task { await AppUpdate.shared.check() }
        if AppSettings.deviceID == nil {
            phase = .findingHome
            await Enrollment.shared.run()
            return
        }
        if !offlineStarted {
            offlineStarted = true
            NotificationCenter.default.addObserver(forName: .hubUnreachable, object: nil, queue: .main) { _ in
                Task { @MainActor in HomeStore.shared.markUnreachable() }
            }
            Downloads.shared.resume()
            Task.detached(priority: .background) { OfflineCache.trim() }
        }
        if me != nil && phase == .launching { phase = me?.role == .pending ? .pending : .ready }
        #if targetEnvironment(simulator)
        if phase == .pending || me?.role == .pending { await Enrollment.shared.upgradeTestDevice() }
        #endif
        await refreshAll()
        openStream()
    }

    func enrolled(status: String) async {
        phase = status == "active" ? .ready : .pending
        await refreshAll()
        openStream()
        PushRegistration.shared.refresh()
    }

    func becameActive() {
        Task { await start() }
        PushRegistration.shared.refresh()
        LiveActivities.shared.sync()
    }

    func wentToBackground() {
        stream?.stop(); stream = nil; streamConnected = false
        poller?.cancel(); poller = nil
    }

    func refreshAll() async {
        await loadMe()
        await loadHome()
        await loadInbox()
        await loadRecentEvents()
    }

    func loadRecentEvents() async {
        guard me?.can(.cameras) == true else { recentEvents = nil; return }
        do {
            let e: CameraEvents = try await HubClient.shared.get("/v1/cameras/events", ["limit": "20"])
            recentEvents = e.items; recentEventsError = nil
        } catch {
            recentEventsError = error.localizedDescription
            if recentEvents == nil { recentEvents = [] }
        }
    }

    func loadMe() async {
        do {
            let (m, savedAt) = try await HubClient.shared.fetch("/v1/me", as: Me.self)
            if savedAt != nil { markUnreachable(); return }
            if !m.person.id.isEmpty, m.person.id != OfflineCache.owner, OfflineCache.owner != "device" {
                // This phone now belongs to someone else: their downloads and saved copies go.
                Downloads.shared.forgetAll()
            }
            OfflineCache.setOwner(m.person.id)
            me = m; AppSettings.cachedMe = m
            if let r = m.remoteUrl { AppSettings.hubRemote = r }
            AppSettings.deviceName = m.device?.name ?? AppSettings.deviceName
            phase = m.role == .pending ? .pending : .ready
            markReachable()
            if m.role != .pending { await VPN.shared.ensureInstalled() }
        } catch HubClient.HubError.notEnrolled {
            phase = .findingHome
        } catch HubClient.HubError.forbidden {
            // A revoked phone has had its id cleared by HubClient; start over on home Wi-Fi.
            if AppSettings.deviceID == nil {
                me = nil; AppSettings.cachedMe = nil; AppSettings.cachedHome = nil; home = nil
                Downloads.shared.forgetAll()
                phase = .findingHome
            }
        } catch {
            markUnreachable()
        }
    }

    func loadHome() async {
        do {
            let (h, savedAt) = try await HubClient.shared.fetch("/v1/home", as: HomeSnapshot.self)
            home = h; AppSettings.cachedHome = h
            if savedAt == nil { markReachable() } else { markUnreachable() }
        } catch { markUnreachable() }
    }

    func loadInbox() async {
        if let l: InboxList = try? await HubClient.shared.get("/v1/inbox") { inbox = l.items.sorted { $0.t > $1.t } }
    }

    private func markReachable() {
        let wasOffline = !reachable
        reachable = true; unreachableSince = nil; lastUpdate = .now
        // Back home: send what was done offline and carry on with downloads.
        if wasOffline || Outbox.shared.pending > 0 { Task { await Outbox.shared.flush() } }
        if wasOffline { Downloads.shared.resume() }
    }
    func markUnreachable() {
        if reachable { unreachableSince = .now }
        reachable = false
    }

    // MARK: live updates

    private func openStream() {
        guard stream == nil, phase == .ready || phase == .pending else { return }
        let s = EventStream { [weak self] event in self?.apply(event) } onState: { [weak self] connected in
            self?.streamConnected = connected
            if connected { self?.markReachable() }
        }
        stream = s
        s.start()
        // A slow safety poll in case the stream is quiet or blocked by a proxy.
        poller?.cancel()
        poller = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(60))
                await self?.loadHome()
            }
        }
    }

    private func apply(_ e: HubEvent) {
        lastUpdate = .now
        switch e.type {
        case "device":
            guard let d = e.data.flatMap({ Self.decode(DeviceItem.self, $0) }), var h = home else { return }
            if let i = h.devices?.firstIndex(where: { $0.id == d.id }) { h.devices?[i] = d } else { h.devices?.append(d) }
            home = h
        case "status":
            guard let s = e.data.flatMap({ Self.decode(HomeSnapshot.HouseStatus.self, $0) }), var h = home else { return }
            h.status = s; home = h
        case "now_playing":
            guard let n = e.data.flatMap({ Self.decode([NowPlaying].self, $0) }), var h = home else { return }
            h.nowPlaying = n; home = h
        case "hello":
            return
        case "camera_event":
            guard let ev = e.data.flatMap({ Self.decode(CameraEvent.self, $0) }) else { return }
            var list = recentEvents ?? []
            list.removeAll { $0.id == ev.id }
            list.insert(ev, at: 0)
            recentEvents = Array(list.prefix(40))
            return
        case "inbox":
            Task { await loadInbox() }
        case "snapshot":
            if case .object(let o)? = e.data, o["reason"]?.string == "role_changed" {
                // A new role means a new token and a different set of tabs.
                Task { await HubClient.shared.signOut(); await refreshAll() }
            } else { Task { await loadHome() } }
        case "me":
            Task { await loadMe() }
        case "app_update":
            Task { await AppUpdate.shared.check(force: true) }
            return
        default:
            Task { await loadHome() }
        }
        AppSettings.cachedHome = home
    }

    private static func decode<T: Decodable>(_ t: T.Type, _ v: JSONValue) -> T? {
        // Keys were already camel-cased when the event was decoded, so re-encode without conversion.
        guard let data = try? JSONEncoder().encode(v) else { return nil }
        return try? JSONDecoder().decode(T.self, from: data)
    }

    // MARK: actions

    /// Run an action, update the device optimistically, and report the result in a toast.
    /// `confirm` is the Face ID reason for Secure-tier actions.
    func perform(_ action: String, target: String? = nil, params: [String: Any] = [:], confirm: String? = nil,
                 optimistic: ((inout DeviceItem) -> Void)? = nil, done: String? = nil) {
        if let target, let optimistic, var h = home, let i = h.devices?.firstIndex(where: { $0.id == target }) {
            optimistic(&h.devices![i]); home = h
        }
        Task {
            do {
                let r = try await HubClient.shared.action(action, target: target, params: params, confirm: confirm)
                if r.dryRun == true { show("Practice mode", r.message ?? "Nothing was changed"); await loadHome() }
                else if let msg = r.message ?? done { show(msg) }
                if action == "mode.run" || action == "scene.run" { await loadHome() }
            } catch HubClient.HubError.cancelled {
                await loadHome()
            } catch HubClient.HubError.forbidden(let ask, let message) {
                await loadHome()
                if ask { askParent = AskParent(what: message ?? "Can I change this?") }
                else { show(message ?? "That isn't allowed.", isError: true) }
            } catch {
                await loadHome()
                show(error.localizedDescription, isError: true)
            }
        }
    }

    /// Hand a title to a TV ("play on").
    func play(_ item: MediaItem, on tv: DeviceItem) {
        Task {
            do {
                let r: ActionReply = try await HubClient.shared.post("/v1/media/play", ["item_id": item.id, "target_device_id": tv.id])
                show(r.dryRun == true ? "Practice mode" : "Playing on \(tv.name)", r.dryRun == true ? r.message : item.title)
            } catch { show(error.localizedDescription, isError: true) }
        }
    }

    func show(_ title: String, _ detail: String? = nil, isError: Bool = false) {
        let t = Toast(title: title, detail: detail, isError: isError)
        toast = t
        Task { try? await Task.sleep(for: .seconds(2.6)); if toast?.id == t.id { toast = nil } }
    }

    // MARK: convenience for screens

    func rooms(on floor: Floor) -> [Room] { floor.roomIds.compactMap { home?.room($0) } }
    var favoriteRooms: [Room] { (home?.rooms ?? []).filter { $0.favorite == true } }
    func lightsOn(in room: Room) -> Int { home?.devices(in: room).filter { $0.kind == .light && $0.isOn }.count ?? 0 }
    var myRoom: Room? { home?.room(me?.child?.roomId) ?? home?.rooms?.first { $0.id == me?.person.area } }
    var holidaySeason: Bool {
        if let s = home?.seasonal?.holidayLights { return s }
        let m = Calendar.current.component(.month, from: .now)
        return m == 11 || m == 12 || m == 1
    }
}
