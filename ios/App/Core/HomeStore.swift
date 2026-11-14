import SwiftUI
import Observation
import TVPhone

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
    /// Thomas Creek TVs, so Now playing can offer Move to… without another round trip.
    var houseTVs: [TVPlayer] = []
    var tvsLoadedAt: Date?
    /// Someone at the door, for as long as the hub said the card should stay up.
    var doorAlert: DoorNotice?
    /// Kids' TV winding down or stopped. A parent can add time from the card.
    var tvBedtime: TVBedtimeNotice?
    var lastUpdate: Date?
    var reachable = true
    var unreachableSince: Date?
    var streamConnected = false
    var toast: Toast?
    var askParent: AskParent?
    /// Who Dylan is looking at the app as ("View as"), nil normally.
    private(set) var previewing: Person?

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
            NotificationCenter.default.addObserver(forName: .hubBackHome, object: nil, queue: .main) { _ in
                // Back on the home address: move the live stream off the published one too.
                Task { @MainActor in HomeStore.shared.reopenStream() }
            }
            NotificationCenter.default.addObserver(forName: .hubReachable, object: nil, queue: .main) { _ in
                Task { @MainActor in
                    let s = HomeStore.shared
                    if !s.reachable { await s.loadHome() }   // fresh Home, and the strip goes with it
                }
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
        TVPairingCenter.shared.start()
    }

    func enrolled(status: String) async {
        phase = status == "active" ? .ready : .pending
        await refreshAll()
        openStream()
        TVPairingCenter.shared.start()
        PushRegistration.shared.refresh()
    }

    func becameActive() {
        Task { await start(); await SendsSync.shared.sync() }
        PushRegistration.shared.refresh()
        LiveActivities.shared.sync()
    }

    func wentToBackground() {
        TVPairingCenter.shared.stop()
        SendsSync.scheduleRefresh()
        stream?.stop(); stream = nil; streamConnected = false
        poller?.cancel(); poller = nil
        // A preview ends when the app is put away, so it can never be left on by accident.
        if previewing != nil { Task { await stopPreview() } }
    }

    func refreshAll() async {
        await loadMe()
        await SettingsStore.shared.load()
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
            if HubClient.viewAs != nil {
                // Previewing someone else: show their answer, but keep this phone's own copies and downloads.
                me = m; markReachable(); return
            }
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
                SpotlightIndex.clear()
                phase = .findingHome
            }
        } catch HubClient.HubError.cancelled {
        } catch {
            markUnreachable()
        }
    }

    func loadHome() async {
        do {
            let (h, savedAt) = try await HubClient.shared.fetch("/v1/home", as: HomeSnapshot.self)
            home = h; cacheHome()
            if savedAt == nil { markReachable(); if previewing == nil { SpotlightIndex.update(h, me: me) } } else { markUnreachable() }
            await loadHouseTVs()
        } catch HubClient.HubError.cancelled {
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
        if wasOffline { Downloads.shared.resume(); Task { await SendsSync.shared.sync() } }
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

    func reopenStream() {
        guard stream != nil else { return }
        stream?.stop(); stream = nil
        openStream()
    }

    private func apply(_ e: HubEvent) {
        lastUpdate = .now
        switch e.type {
        case "device":
            guard let incoming = e.data.flatMap({ Self.decode(DeviceItem.self, $0) }), var h = home else { return }
            let d = reconcile(incoming)
            if let i = h.devices?.firstIndex(where: { $0.id == d.id }) { h.devices?[i] = d } else { h.devices?.append(d) }
            home = h
            refreshSmart(after: 2)  // a door locked or a light went off: the cards may change
        case "status":
            guard let s = e.data.flatMap({ Self.decode(HomeSnapshot.HouseStatus.self, $0) }), var h = home else { return }
            h.status = s; home = h
        case "now_playing":
            guard let n = e.data.flatMap({ Self.decode([NowPlaying].self, $0) }), var h = home else { return }
            h.nowPlaying = n; home = h
        case "printer":
            // Live printer status: refresh the Home summary and hand the full status to an open Workshop.
            guard let st = e.data.flatMap({ Self.decode(PrinterStatus.self, $0) }), var h = home else { return }
            let sum = PrinterSummary(id: st.id, name: st.name, model: st.model, state: st.state, online: st.online,
                                     setUp: st.setUp, job: st.job, color: st.activeTray?.color)
            if let i = h.printers?.firstIndex(where: { $0.id == st.id }) { h.printers?[i] = sum; home = h }
            NotificationCenter.default.post(name: .printerStatus, object: st)
        case "tv":
            guard case .object(let o)? = e.data else { return }
            if let watch = Self.decode(TVWatch.self, .object(o)) { noteTV(watch) }
            if let cmd = o["command"]?.object {
                let name = o["name"]?.string ?? "The TV"
                let ok = cmd["ok"]?.bool ?? false
                show(ok ? "Playing on \(name)" : (cmd["error"]?.string ?? "\(name) didn't start it"), isError: !ok)
            }
            return
        case "watch":
            // "Watch this on that TV" finished in the background (Plex opened, title started, tracks picked).
            guard case .object(let o)? = e.data else { return }
            let ok = o["ok"]?.bool ?? false
            show(o["message"]?.string ?? (ok ? "Playing" : "Couldn't start it"), o["tracks"]?.string, isError: !ok)
        case "tv_pair":
            // One event per new TV. The banner uses it at once; the list adds the room's name.
            guard let event = e.data.flatMap({ Self.decode(TVPairEvent.self, $0) }) else { return }
            TVPairingCenter.shared.note(event)
            return
        case "hello":
            return
        case "bedtime":
            guard case .object(let o)? = e.data, let state = o["state"]?.string else { return }
            let left = o["minutes_left"]?.double ?? o["minutesLeft"]?.double
            tvBedtime = TVBedtimeNotice(state: state, profile: o["profile"]?.string ?? "kids",
                                        minutesLeft: left.map { Int($0) })
            if state == "warning" { UINotificationFeedbackGenerator().notificationOccurred(.warning) }
            return
        case "home_alert":
            guard let alert = e.data.flatMap({ Self.decode(DoorNotice.self, $0) }) else { return }
            doorAlert = alert
            if alert.kind == "doorbell" { UINotificationFeedbackGenerator().notificationOccurred(.warning) }
            DoorLive.show(alert)
            let token = alert.id
            Task {
                try? await Task.sleep(for: .seconds(max(8, alert.ttlS)))
                if doorAlert?.id == token { doorAlert = nil }
            }
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
        case "smart":
            refreshSmart(after: 0.5)
            return
        case "action_result":
            // The hub accepted a tap and answered at once; this is how it finished.
            guard case .object(let o)? = e.data, o["ok"]?.bool == false, let rid = o["request_id"]?.string else { return }
            failPending(requestID: rid, message: o["message"]?.string ?? "That didn't go through.")
            return
        case "settings":
            if case .object(let o)? = e.data, let v = o["version"]?.double {
                SettingsStore.shared.hubVersion(Int(v), from: o["device_id"]?.string)
            }
            return
        default:
            Task { await loadHome() }
        }
        cacheHome()
    }

    /// The saved copy the app and widgets open with. Never written while previewing someone else.
    private func cacheHome() {
        if previewing == nil { AppSettings.cachedHome = home }
    }

    // MARK: View as

    /// Dylan sees the app exactly as this person does. The hub answers as them and refuses anything that
    /// would change the house; their favorites and Home layout can be set up.
    func startPreview(_ person: Person) async {
        guard me?.role == .admin, previewing == nil else { return }
        try? await HubClient.shared.send("/v1/me/view-as", ["person_id": person.id])
        stream?.stop(); stream = nil
        HubClient.viewAs = person.id
        previewing = person
        Router.shared.reset()
        await refreshAll()
        openStream()
        TVPairingCenter.shared.start()
    }

    func stopPreview() async {
        guard let who = previewing else { return }
        stream?.stop(); stream = nil
        HubClient.viewAs = nil
        previewing = nil
        me = AppSettings.cachedMe; home = AppSettings.cachedHome
        SettingsStore.shared.reset()
        Router.shared.reset()
        try? await HubClient.shared.send("/v1/me/view-as", ["person_id": NSNull()])
        _ = who
        await refreshAll()
        openStream()
        TVPairingCenter.shared.start()
    }

    // MARK: smart cards

    private var smartTask: Task<Void, Never>?

    /// Fetch just the "Right now" cards, coalescing bursts of events into one request.
    func refreshSmart(after delay: Double = 0) {
        smartTask?.cancel()
        smartTask = Task { [weak self] in
            if delay > 0 { try? await Task.sleep(for: .seconds(delay)) }
            guard !Task.isCancelled else { return }
            guard let list: SmartList = try? await HubClient.shared.get("/v1/smart") else { return }
            guard let self, var h = self.home else { return }
            h.smart = list.cards
            if let w = list.weather { h.weather = w }
            self.home = h
            self.cacheHome()
        }
    }

    /// Hide a card here at once, then tell the hub ("Not now", or handled after a button worked).
    func dismissSmart(_ card: SmartCard, handled: Bool) {
        if var h = home { h.smart?.removeAll { $0.id == card.id }; home = h }
        Task {
            struct Reply: Decodable { var muted: Bool?; var ruleTitle: String? }
            if let r: Reply = try? await HubClient.shared.post("/v1/smart/dismiss", ["id": card.id, "rule": card.rule, "handled": handled]),
               r.muted == true {
                show("Won't suggest \(r.ruleTitle ?? "that") again", "Turn it back on in Profile → Suggestions")
            }
        }
    }

    /// A card's button.
    func use(_ b: SmartCard.Button, on card: SmartCard, router: Router) {
        switch b.kind {
        case "open":
            if let u = b.url.flatMap(URL.init(string:)) { router.follow(u) }
        case "done":
            if let d = b.done { show(d) }
            dismissSmart(card, handled: true)
        case "call":
            guard let path = b.path else { return }
            Task {
                do {
                    try await HubClient.shared.send(path)
                    if let d = b.done { show(d) }
                    dismissSmart(card, handled: true)
                } catch { show(error.localizedDescription, isError: true) }
            }
        default:
            guard let action = b.action else { return }
            let params = (b.params?.hubBody as? [String: Any]) ?? [:]
            let confirm = b.secure == true ? "Confirm: \(b.title)" : nil
            Task {
                do {
                    let r = try await HubClient.shared.action(action, target: b.target, params: params, confirm: confirm)
                    if r.dryRun == true { show("Practice mode", r.message ?? "Nothing was changed") }
                    else { show(b.done ?? r.message ?? "Done") }
                    dismissSmart(card, handled: true)
                    await loadHome()
                } catch HubClient.HubError.cancelled {
                } catch HubClient.HubError.forbidden(_, let message) {
                    show(message ?? "That isn't allowed.", isError: true)
                } catch {
                    show(error.localizedDescription, isError: true)
                }
            }
        }
    }

    private static func decode<T: Decodable>(_ t: T.Type, _ v: JSONValue) -> T? {
        // Nested object keys inside a JSONValue can still be snake_case (camera_name, thumb_url), so
        // decode with the hub decoder. Keys that are already camelCase pass through it unchanged.
        guard let data = try? JSONEncoder().encode(v) else { return nil }
        return try? HubClient.decoder.decode(T.self, from: data)
    }

    // MARK: actions

    // MARK: taps that feel instant
    //
    // A tap changes the screen at once and remembers what it asked for. Events from the hub that still show the
    // old state (the light hasn't reported back yet) don't flip the switch back; the change is kept until the
    // device confirms it (a light tap of haptics) or `pendingWindow` passes. A failure puts back the state from
    // before the tap, without reloading the whole house.

    struct Pending {
        var requestID: String
        var expected: [String: JSONValue]
        var before: DeviceItem
        var until: Date
        /// The last state the hub reported while waiting, put back if the change never shows up.
        var lastReal: DeviceItem?
    }
    private var pending: [String: Pending] = [:]
    static let pendingWindow: TimeInterval = 10

    /// Keep a tap's change over a stale event; settle it when the device shows it.
    private func reconcile(_ d: DeviceItem) -> DeviceItem {
        guard var p = pending[d.id] else { return d }
        if p.until < .now { pending[d.id] = nil; return d }
        if Self.shows(d, p.expected) {
            pending[d.id] = nil
            UIImpactFeedbackGenerator(style: .soft).impactOccurred(intensity: 0.6)
            return d
        }
        p.lastReal = d; pending[d.id] = p
        var kept = d
        for (k, v) in p.expected { kept.state[k] = v }
        return kept
    }

    /// Whether the device's reported state has what the tap asked for. Brightness is a percentage the hub rounds,
    /// so within 2 counts; colours are compared by the light being on (bulbs report their own nearest colour).
    static func shows(_ d: DeviceItem, _ expected: [String: JSONValue]) -> Bool {
        for (k, v) in expected {
            switch (k, v, d.state[k]) {
            case ("color", _, _): continue
            case (_, .number(let a), .number(let b)?): if abs(a - b) > 2 { return false }
            case ("on", .bool(let on), _): if d.isOn != on { return false }
            default: if d.state[k] != v { return false }
            }
        }
        return true
    }

    private func setDevice(_ d: DeviceItem) {
        guard var h = home, let i = h.devices?.firstIndex(where: { $0.id == d.id }) else { return }
        h.devices![i] = d; home = h
    }

    /// Apply a tap's change on screen and remember it. Returns false when the device isn't on screen.
    @discardableResult
    private func begin(_ target: String, requestID: String, _ optimistic: (inout DeviceItem) -> Void) -> Bool {
        guard let before = home?.device(target) else { return false }
        var after = before
        optimistic(&after)
        var expected: [String: JSONValue] = [:]
        for (k, v) in after.state where before.state[k] != v { expected[k] = v }
        if let on = after.state["on"] { expected["on"] = on }
        // A second tap on the same device keeps the first one's "before", so a failure goes all the way back.
        let original = pending[target].map(\.before) ?? before
        pending[target] = Pending(requestID: requestID, expected: expected, before: original,
                                  until: .now + Self.pendingWindow)
        setDevice(after)
        Task { [weak self] in
            try? await Task.sleep(for: .seconds(Self.pendingWindow + 0.5))
            self?.expire(target, requestID: requestID)
        }
        return true
    }

    /// The change never showed up: show what the device last reported, or ask the hub once if it said nothing.
    private func expire(_ target: String, requestID: String) {
        guard let p = pending[target], p.requestID == requestID else { return }
        pending[target] = nil
        if let real = p.lastReal { setDevice(real) } else { Task { await loadHome() } }
    }

    /// Put the devices of a failed request back as they were.
    private func revert(requestID: String) {
        for (id, p) in pending where p.requestID == requestID {
            pending[id] = nil
            setDevice(p.lastReal ?? p.before)
        }
    }

    private func failPending(requestID: String, message: String) {
        guard pending.values.contains(where: { $0.requestID == requestID }) else { return }
        revert(requestID: requestID)
        UINotificationFeedbackGenerator().notificationOccurred(.error)
        show(message, isError: true)
    }

    /// Run an action, update the device optimistically, and report the result in a toast.
    /// `confirm` is the Face ID reason for Secure-tier actions.
    func perform(_ action: String, target: String? = nil, params: [String: Any] = [:], confirm: String? = nil,
                 optimistic: ((inout DeviceItem) -> Void)? = nil, done: String? = nil) {
        let rid = HubClient.newRequestID()
        var tracked = false
        if let target, let optimistic { tracked = begin(target, requestID: rid, optimistic) }
        Task {
            do {
                let r = try await HubClient.shared.action(action, target: target, params: params, confirm: confirm, requestID: rid)
                finish(r, action: action, tracked: tracked, requestID: rid, done: done)
            } catch {
                failed(error, tracked: tracked, requestID: rid)
            }
        }
    }

    /// The same action for several devices in one request: a preset, or a lamp made of several bulbs. An older
    /// hub without the batch endpoint gets them one at a time.
    func performBatch(_ action: String, items: [(target: String, params: [String: Any], optimistic: (inout DeviceItem) -> Void)],
                      confirm: String? = nil) {
        guard !items.isEmpty else { return }
        if items.count == 1, let one = items.first {
            perform(action, target: one.target, params: one.params, confirm: confirm, optimistic: one.optimistic)
            return
        }
        let rid = HubClient.newRequestID()
        var tracked = false
        for it in items { tracked = begin(it.target, requestID: rid, it.optimistic) || tracked }
        Task {
            do {
                let r = try await HubClient.shared.actionBatch(action, items: items.map { ($0.target, $0.params) },
                                                               confirm: confirm, requestID: rid)
                finish(r, action: action, tracked: tracked, requestID: rid, done: nil)
            } catch HubClient.HubError.server(404, _) {
                revert(requestID: rid)
                for it in items { perform(action, target: it.target, params: it.params, confirm: confirm, optimistic: it.optimistic) }
            } catch {
                failed(error, tracked: tracked, requestID: rid)
            }
        }
    }

    private func finish(_ r: ActionReply, action: String, tracked: Bool, requestID: String, done: String?) {
        if r.dryRun == true {
            revert(requestID: requestID)
            show("Practice mode", r.message ?? "Nothing was changed")
            Task { await loadHome() }
            return
        }
        // A tracked tap speaks through the switch and a haptic; only a real message gets a toast.
        if let msg = done ?? r.message, !(tracked && (msg == "Done" || r.accepted == true)) { show(msg) }
        if action == "mode.run" || action == "scene.run" { Task { await loadHome() } }
    }

    private func failed(_ error: Error, tracked: Bool, requestID: String) {
        switch error {
        case HubClient.HubError.cancelled:
            revert(requestID: requestID)
            if !tracked { Task { await loadHome() } }
        case HubClient.HubError.noAnswer:
            // It may still happen: keep the change on screen until the device confirms or the window passes.
            show("Still waiting on home", "It may change in a moment.")
        case HubClient.HubError.forbidden(let ask, let message):
            revert(requestID: requestID)
            if !tracked { Task { await loadHome() } }
            if ask { askParent = AskParent(what: message ?? "Can I change this?") }
            else { show(message ?? "That isn't allowed.", isError: true) }
        default:
            revert(requestID: requestID)
            if !tracked { Task { await loadHome() } }
            UINotificationFeedbackGenerator().notificationOccurred(.error)
            show(error.localizedDescription, isError: true)
        }
    }

    /// Not in English? The hub picks English audio, or the original audio with English subtitles, and switches
    /// the TV now if it's playing there.
    func fixLanguage(_ item: MediaItem, on tv: String?) {
        Task {
            do {
                var body: [String: Any] = [:]
                if let tv { body["target_device_id"] = tv }
                let r: LanguageFix = try await HubClient.shared.post("/v1/media/\(item.id)/language", body)
                show(r.message, [r.audio, r.subtitles.map { "Subtitles: \($0)" }].compactMap { $0 }.joined(separator: " · "),
                     isError: r.ok == false)
            } catch { show(error.localizedDescription, isError: true) }
        }
    }

    func extendBedtime(minutes: Int) async {
        tvBedtime = TVBedtimeNotice(state: "unlocked", profile: tvBedtime?.profile ?? "kids", minutesLeft: minutes)
        do {
            let _: TVBedtimeRules = try await HubClient.shared.post("/v1/tv/bedtime/extend",
                                                                    ["profile": "kids", "minutes": minutes])
            show("Added \(minutes) minutes")
        } catch {
            show(error.localizedDescription)
        }
    }

    func loadHouseTVs() async {
        guard let list: TVDeviceList = try? await HubClient.shared.get("/v1/tv/devices") else { return }
        houseTVs = list.devices
        tvsLoadedAt = .now
        NowPlayingActivity.sync(list.devices)
    }

    private func noteTV(_ watch: TVWatch) {
        if let i = houseTVs.firstIndex(where: { $0.id == watch.id }) {
            houseTVs[i].playing = watch.playing
            houseTVs[i].online = true
            if let name = watch.name { houseTVs[i].name = name }
        } else if watch.playing != nil {
            houseTVs.append(TVPlayer(id: watch.id, name: watch.name ?? "TV", room: watch.room,
                                     mediaEntity: watch.mediaEntity, online: true, playing: watch.playing))
        }
        NowPlayingActivity.sync(houseTVs)
    }

    /// Start the show on another TV from the same second. The card updates before the hub answers.
    func moveTV(from: String, to: TVPlayer, title: String) {
        let before = houseTVs
        let source = houseTVs.first { $0.id == from || $0.mediaEntity == from }
        if source?.playing != nil {
            if let i = houseTVs.firstIndex(where: { $0.id == from || $0.mediaEntity == from }) {
                let carried = houseTVs[i].playing
                houseTVs[i].playing = nil
                if let j = houseTVs.firstIndex(where: { $0.id == to.id }) { houseTVs[j].playing = carried }
            }
            show("Moving \(title) to \(to.name)")
        }
        Task {
            do {
                let payload = try TVHandoffBody(fromDevice: from, toDevice: to.id).json()
                let reply: HouseHandoffReply = try await HubClient.shared.post(TVRemotePath.handoff, payload)
                if reply.stayed {
                    houseTVs = before
                    show(reply.message ?? "It's still where it was")
                }
            } catch HubClient.HubError.unreachable {
                houseTVs = before
                show("Can't reach the house right now.")
            } catch HubClient.HubError.cancelled {
                houseTVs = before
            } catch {
                houseTVs = before
                show(calm(error))
            }
        }
    }

    /// One remote button. Returns a sentence when it did not go through. Nil when the hub took it.
    func press(_ body: TVRemoteBody) async -> String? {
        do {
            let reply: TVRemoteReply = try await HubClient.shared.post(TVRemotePath.remote, try body.json())
            return reply.calmLine
        } catch HubClient.HubError.unreachable {
            return "Can't reach the house right now."
        } catch HubClient.HubError.cancelled {
            return nil
        } catch {
            return calm(error)
        }
    }

    private func calm(_ error: Error) -> String {
        if let hub = error as? HubClient.HubError, let line = hub.errorDescription, !line.isEmpty,
           !line.contains("NSURL"), !line.contains("Error Domain") {
            return line
        }
        return "That didn't go through."
    }

    /// Hand a title to a TV ("play on").
    func play(_ item: MediaItem, on tv: DeviceItem) {
        Task {
            do {
                let r: ActionReply = try await HubClient.shared.post("/v1/media/play", ["item_id": item.id, "target_device_id": tv.id])
                // A TV with a Plex app answers right away ("Opening Plex on …"); the hub reports when it's playing.
                show(r.dryRun == true ? "Practice mode" : (r.message ?? "Playing on \(tv.name)"), r.dryRun == true ? r.message : item.title)
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
    /// Starred rooms in this person's order (the hub seeds them from the house list until they pick).
    var favoriteRooms: [Room] {
        let stars = SettingsStore.shared.favorites(.room)
        if stars.isEmpty && SettingsStore.shared.current.version == 0 { return (home?.rooms ?? []).filter { $0.favorite == true } }
        return stars.compactMap { home?.room($0.id) }
    }
    func lightsOn(in room: Room) -> Int { home?.devices(in: room).filter { $0.kind == .light && $0.isOn }.count ?? 0 }
    var myRoom: Room? { home?.room(me?.child?.roomId) ?? home?.rooms?.first { $0.id == me?.person.area } }
    var holidaySeason: Bool {
        if let s = home?.seasonal?.holidayLights { return s }
        let m = Calendar.current.component(.month, from: .now)
        return m == 11 || m == 12 || m == 1
    }
}
