import SwiftUI
import CoreLocation

// MARK: - "Last one out?"

/// What's still open as you leave, each with its fix, plus "Fix everything". Opened from the
/// "You're the last one out" push, from Home and from Siri.
struct DepartureView: View {
    @Environment(HomeStore.self) private var store
    @State private var dep: Departure?
    @State private var working: Set<String> = []
    @State private var error: String?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                header
                if let dep {
                    if !dep.othersHome.isEmpty {
                        Label("\(dep.othersHome.joined(separator: ", ")) \(dep.othersHome.count == 1 ? "is" : "are") still home",
                              systemImage: "person.2.fill").font(.subheadline).foregroundStyle(Theme.dim).card()
                    }
                    if dep.open.isEmpty {
                        allClear
                    } else {
                        VStack(spacing: 10) { ForEach(dep.open) { row($0) } }
                        PrimaryButton(title: "Fix everything", symbol: "checkmark.shield.fill") { Task { await fixAll(dep.open) } }
                            .disabled(!working.isEmpty)
                    }
                } else if let error {
                    EmptyCard(symbol: "wifi.exclamationmark", title: "Couldn't check the house", detail: error)
                } else { LoadingCard() }
            }
            .screen()
        }
        .background(Backdrop())
        .navigationTitle("Leaving")
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
        .refreshable { await load() }
    }

    private var header: some View {
        HStack(spacing: 14) {
            Text(dep?.allClear == true ? "👋" : "🔑").font(.system(size: 44))
            VStack(alignment: .leading, spacing: 2) {
                Text(dep?.lastOneOut == false ? "Heading out?" : "Last one out?").font(.system(.title2, design: .rounded, weight: .bold))
                Text(dep?.line ?? "Checking doors, the garage, ovens and lights…").font(.subheadline).foregroundStyle(Theme.dim)
            }
        }
        .padding(.top, 10)
    }

    private var allClear: some View {
        VStack(spacing: 10) {
            Image(systemName: "checkmark.seal.fill").font(.system(size: 54)).foregroundStyle(.green)
                .symbolEffect(.bounce, value: dep?.allClear)
            Text("All locked up").font(.title3.weight(.semibold))
            Text("Doors locked, garage closed, ovens and lights off.").font(.subheadline).foregroundStyle(Theme.dim)
        }
        .frame(maxWidth: .infinity).padding(.vertical, 24).card()
    }

    private func row(_ t: Departure.OpenThing) -> some View {
        HStack(spacing: 12) {
            IconBadge(symbol: symbol(t.kind), color: Theme.warn, size: 40)
            Text(t.title).font(.subheadline.weight(.semibold))
            Spacer()
            if working.contains(t.id) { ProgressView() } else {
                Button(t.fix) { Task { await fix(t) } }.buttonStyle(.borderedProminent).tint(Theme.accent).controlSize(.small)
            }
        }
        .card()
    }

    private func symbol(_ kind: String) -> String {
        switch kind { case "lock": "lock.open.fill"; case "garage": "door.garage.open"; case "oven": "oven.fill"; default: "lightbulb.fill" }
    }

    private func load() async {
        do { dep = try await HubClient.shared.get("/v1/departure"); error = nil }
        catch { self.error = error.localizedDescription }
    }

    private func fix(_ t: Departure.OpenThing) async {
        working.insert(t.id)
        defer { working.remove(t.id) }
        do {
            let params = (t.params?.hubBody as? [String: Any]) ?? [:]
            _ = try await HubClient.shared.action(t.action, target: t.target, params: params,
                                                  confirm: t.secure == true ? "Confirm: \(t.title)" : nil)
            UINotificationFeedbackGenerator().notificationOccurred(.success)
        } catch { store.show(error.localizedDescription, isError: true) }
        await load()
    }

    private func fixAll(_ items: [Departure.OpenThing]) async {
        for t in items { await fix(t) }
        store.show(dep?.allClear == true ? "All locked up" : "Done what I could")
    }
}

// MARK: - Arriving and leaving (geofence)

/// Watches a circle round the house and tells the hub when this person arrives or leaves.
/// The hub decides whether they were the last one out (and pushes the check) or runs their welcome mode.
///
/// Uses `CLMonitor` (iOS 17+): the old `CLCircularRegion` monitoring is refused by iOS 27 ("unsupported
/// fence type"). Only "inside" or "outside" the circle ever leaves the phone, never where it is.
/// A crossing relaunches the app in the background; `start()` at launch picks the event up.
@MainActor
final class PresenceMonitor: NSObject, CLLocationManagerDelegate {
    static let shared = PresenceMonitor()
    static let settingKey = "app.family.arrivals"
    static let lastStateKey = "app.family.circleState"   // "inside" / "outside": tells a crossing from a refresh
    static let monitorName = "thomascreek.presence"
    static let conditionID = "thomascreek.home"
    static let radius: CLLocationDistance = 150
    private let manager = CLLocationManager()   // only for the "Always" permission and a one-off fix at home
    private var monitor: CLMonitor?
    private var listening: Task<Void, Never>?
    private var pendingSetup = false

    override init() {
        super.init()
        manager.delegate = self
    }

    /// Call at launch (also a background relaunch for a crossing) and when the app comes to the front.
    func start() {
        guard UserDefaults.standard.bool(forKey: Self.settingKey) else { return }
        // Builds before this used CLCircularRegion; iOS no longer runs those, so drop them.
        for r in manager.monitoredRegions { manager.stopMonitoring(for: r) }
        Task {
            let m = await self.openMonitor()
            if await m.identifiers.contains(Self.conditionID) {
                await self.refresh(m)
            } else {
                self.setUpCircle()
            }
        }
    }

    func enable() {
        UserDefaults.standard.set(true, forKey: Self.settingKey)
        pendingSetup = true
        switch manager.authorizationStatus {
        case .notDetermined: manager.requestAlwaysAuthorization()
        case .authorizedWhenInUse: manager.requestAlwaysAuthorization(); setUpCircle()
        default: setUpCircle()
        }
    }

    func disable() {
        UserDefaults.standard.set(false, forKey: Self.settingKey)
        UserDefaults.standard.removeObject(forKey: Self.lastStateKey)
        for r in manager.monitoredRegions { manager.stopMonitoring(for: r) }
        listening?.cancel()
        listening = nil
        Task {
            if let m = self.monitor { await m.remove(Self.conditionID) }
            // The hub forgets this phone's last arrive/leave; Wi-Fi decides who's home again.
            let _: Empty? = await hubQuiet("presence off", { try await HubClient.shared.post("/v1/presence", ["event": "off"]) })
        }
    }

    var isOn: Bool { UserDefaults.standard.bool(forKey: Self.settingKey) }
    var needsAlways: Bool { manager.authorizationStatus != .authorizedAlways }

    private func openMonitor() async -> CLMonitor {
        if let m = monitor { return m }
        let m = await CLMonitor(Self.monitorName)
        monitor = m
        listening = Task { [weak self] in
            do {
                for try await event in await m.events where event.identifier == Self.conditionID {
                    await self?.handle(event.state)
                }
            } catch {
                // The stream ends on an error; the next start() opens it again.
            }
        }
        return m
    }

    /// The house's location comes from the hub (Home Assistant's own); a phone at home can also use where it is.
    private func setUpCircle() {
        Task {
            struct Loc: Decodable { var location: Coord?; struct Coord: Decodable { var lat: Double; var lon: Double } }
            if let l: Loc = await hubQuiet("home location", { try await HubClient.shared.get("/v1/me/welcome") }), let c = l.location {
                await self.watch(CLLocationCoordinate2D(latitude: c.lat, longitude: c.lon))
            } else if HomeStore.shared.me?.atHome == true {
                self.manager.requestLocation()
            }
        }
    }

    private func watch(_ c: CLLocationCoordinate2D) async {
        let m = await openMonitor()
        await m.remove(Self.conditionID)
        UserDefaults.standard.removeObject(forKey: Self.lastStateKey)   // the first event is a refresh, not a crossing
        await m.add(CLMonitor.CircularGeographicCondition(center: c, radius: Self.radius), identifier: Self.conditionID)
        pendingSetup = false
    }

    /// Coming back to the app: tell the hub where the phone is now, without welcome or last-one-out.
    private func refresh(_ m: CLMonitor) async {
        guard let state = await m.record(for: Self.conditionID)?.lastEvent.state else { return }
        await handle(state, refreshOnly: true)
    }

    /// One state from iOS. A change from a known state is a crossing (arrive/leave); anything else only
    /// refreshes (inside/outside). iOS can repeat an event on a background launch, so repeats are dropped.
    private func handle(_ state: CLMonitor.Event.State, refreshOnly: Bool = false) async {
        let now: String
        switch state {
        case .satisfied: now = "inside"
        case .unsatisfied: now = "outside"
        default: return
        }
        let before = UserDefaults.standard.string(forKey: Self.lastStateKey)
        if before == now && !refreshOnly { return }
        UserDefaults.standard.set(now, forKey: Self.lastStateKey)
        if before == nil || before == now || refreshOnly {
            let _: Empty? = await hubQuiet("presence", { try await HubClient.shared.post("/v1/presence", ["event": now]) })
        } else {
            await report(now == "inside" ? "arrive" : "leave")
        }
    }

    nonisolated func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        Task { @MainActor in if self.pendingSetup { self.setUpCircle() } }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard let c = locations.last?.coordinate else { return }
        Task { @MainActor in if self.pendingSetup { await self.watch(c) } }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {}

    private func report(_ event: String) async {
        // The app may only have a few seconds in the background.
        let task = UIApplication.shared.beginBackgroundTask(withName: "presence")
        defer { UIApplication.shared.endBackgroundTask(task) }
        if let r: PresenceReply = await hubQuiet("presence", { try await HubClient.shared.post("/v1/presence", ["event": event]) }),
           let w = r.welcome, let msg = w.message {
            HomeStore.shared.show("Welcome home", msg)
        }
        AppSettings.refreshSurfaces()
    }
}
