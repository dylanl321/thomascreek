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
@MainActor
final class PresenceMonitor: NSObject, CLLocationManagerDelegate {
    static let shared = PresenceMonitor()
    static let settingKey = "app.family.arrivals"
    static let regionID = "thomascreek.home"
    private let manager = CLLocationManager()
    private var pendingSetup = false

    override init() {
        super.init()
        manager.delegate = self
    }

    /// Call at launch: region events relaunch the app in the background and arrive here.
    func start() {
        guard UserDefaults.standard.bool(forKey: Self.settingKey) else { return }
        if manager.monitoredRegions.isEmpty { setUpRegion() }
    }

    func enable() {
        UserDefaults.standard.set(true, forKey: Self.settingKey)
        pendingSetup = true
        if manager.authorizationStatus == .notDetermined { manager.requestAlwaysAuthorization() }
        else if manager.authorizationStatus == .authorizedWhenInUse { manager.requestAlwaysAuthorization(); setUpRegion() }
        else { setUpRegion() }
    }

    func disable() {
        UserDefaults.standard.set(false, forKey: Self.settingKey)
        for r in manager.monitoredRegions { manager.stopMonitoring(for: r) }
    }

    var isOn: Bool { UserDefaults.standard.bool(forKey: Self.settingKey) && !manager.monitoredRegions.isEmpty }
    var needsAlways: Bool { manager.authorizationStatus != .authorizedAlways }

    /// The house's location comes from the hub (Home Assistant's own); a phone at home can also use where it is.
    private func setUpRegion() {
        Task {
            struct Loc: Decodable { var location: Coord?; struct Coord: Decodable { var lat: Double; var lon: Double } }
            if let l: Loc = try? await HubClient.shared.get("/v1/me/welcome"), let c = l.location {
                monitor(CLLocationCoordinate2D(latitude: c.lat, longitude: c.lon))
            } else if HomeStore.shared.me?.atHome == true {
                manager.requestLocation()
            }
        }
    }

    private func monitor(_ c: CLLocationCoordinate2D) {
        for r in manager.monitoredRegions { manager.stopMonitoring(for: r) }
        let region = CLCircularRegion(center: c, radius: 150, identifier: Self.regionID)
        region.notifyOnEntry = true
        region.notifyOnExit = true
        manager.startMonitoring(for: region)
        pendingSetup = false
    }

    nonisolated func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        Task { @MainActor in if self.pendingSetup { self.setUpRegion() } }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard let c = locations.last?.coordinate else { return }
        Task { @MainActor in if self.pendingSetup { self.monitor(c) } }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {}

    nonisolated func locationManager(_ manager: CLLocationManager, didEnterRegion region: CLRegion) {
        Task { @MainActor in await self.report("arrive") }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didExitRegion region: CLRegion) {
        Task { @MainActor in await self.report("leave") }
    }

    private func report(_ event: String) async {
        // The app may only have a few seconds in the background.
        let task = UIApplication.shared.beginBackgroundTask(withName: "presence")
        defer { UIApplication.shared.endBackgroundTask(task) }
        if let r: PresenceReply = try? await HubClient.shared.post("/v1/presence", ["event": event]),
           let w = r.welcome, let msg = w.message {
            HomeStore.shared.show("Welcome home", msg)
        }
        AppSettings.refreshSurfaces()
    }
}
