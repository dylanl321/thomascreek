import Foundation
#if canImport(WidgetKit)
import WidgetKit
#endif

/// Settings shared by the app, the widgets, Control Center and Siri (App Group defaults).
/// Nothing secret lives here: the hub holds every service password and token.
enum AppSettings {
    static let groupID = Bundle.main.object(forInfoDictionaryKey: "AppGroup") as? String ?? ""
    static let defaults = UserDefaults(suiteName: groupID) ?? .standard

    /// The hub at home, found with Bonjour on first launch, e.g. https://<hub-ip>:8443.
    static var hubLAN: String {
        get { defaults.string(forKey: "hubLAN") ?? "" }
        set { defaults.set(clean(newValue), forKey: "hubLAN") }
    }
    /// The hub published through the reverse proxy, if Dylan enables it (the hub reports it in /v1/me).
    static var hubRemote: String {
        get { defaults.string(forKey: "hubRemote") ?? "" }
        set { defaults.set(clean(newValue), forKey: "hubRemote") }
    }
    /// Hex SHA-256 of the hub certificate's public key, from its Bonjour record at enrollment.
    static var hubPin: String {
        get { defaults.string(forKey: "hubPin") ?? "" }
        set { defaults.set(newValue, forKey: "hubPin") }
    }
    /// Set when the hub address was typed in (Bonjour didn't answer): the first certificate seen from
    /// this host is pinned, then the flag clears. Only private LAN addresses qualify.
    static var trustFirstCertFrom: String {
        get { defaults.string(forKey: "trustFirstCertFrom") ?? "" }
        set { defaults.set(newValue, forKey: "trustFirstCertFrom") }
    }
    /// The hub's id for this phone, set by enrollment.
    static var deviceID: String? {
        get { defaults.string(forKey: "hubDeviceID") }
        set { defaults.set(newValue, forKey: "hubDeviceID") }
    }
    /// A name for this phone, used in "Dylan's iPhone opened the garage" notices.
    static var deviceName: String {
        get { defaults.string(forKey: "deviceName") ?? "iPhone" }
        set { defaults.set(newValue, forKey: "deviceName") }
    }
    static var autoLiveActivity: Bool {
        get { defaults.object(forKey: "autoLiveActivity") as? Bool ?? true }
        set { defaults.set(newValue, forKey: "autoLiveActivity") }
    }
    static var prefs: [String: Bool] {
        get { defaults.dictionary(forKey: "prefs") as? [String: Bool] ?? [:] }
        set { defaults.set(newValue, forKey: "prefs") }
    }

    // MARK: offline

    /// Trips an adult switched on (or off) for offline, overriding "kept while it's close".
    static var offlineTripsOn: Set<Int> {
        get { Set(defaults.array(forKey: "offlineTripsOn") as? [Int] ?? []) }
        set { defaults.set(Array(newValue), forKey: "offlineTripsOn") }
    }
    static var offlineTripsOff: Set<Int> {
        get { Set(defaults.array(forKey: "offlineTripsOff") as? [Int] ?? []) }
        set { defaults.set(Array(newValue), forKey: "offlineTripsOff") }
    }
    /// Films download over Wi-Fi only unless this is on.
    static var downloadOnCellular: Bool {
        get { defaults.bool(forKey: "downloadOnCellular") }
        set { defaults.set(newValue, forKey: "downloadOnCellular") }
    }
    /// Remove a downloaded title a week after it was watched to the end.
    static var removeWatchedDownloads: Bool {
        get { defaults.object(forKey: "removeWatchedDownloads") as? Bool ?? true }
        set { defaults.set(newValue, forKey: "removeWatchedDownloads") }
    }
    /// Away-from-home VPN: "off" (default; only connects when asked), "away" (connects off home Wi-Fi) or "always".
    static var vpnMode: String {
        get { defaults.string(forKey: "vpnMode") ?? "off" }
        set { defaults.set(newValue, forKey: "vpnMode") }
    }
    /// Quality picked last time, offered first next time.
    static var downloadQuality: String {
        get { defaults.string(forKey: "downloadQuality") ?? "original" }
        set { defaults.set(newValue, forKey: "downloadQuality") }
    }

    // MARK: caches so the app and widgets open with something on screen

    static var cachedMe: Me? {
        get { load("me") }
        set { save(newValue, "me") }
    }
    static var cachedHome: HomeSnapshot? {
        get { load("home") }
        set { save(newValue, "home") }
    }
    static var cachedStatus: ShowStatus? {
        get { load("status") }
        set { save(newValue, "status") }
    }
    static var cachedChores: ChoreBoard? {
        get { load("chores") }
        set { save(newValue, "chores") }
    }

    private static func clean(_ s: String) -> String {
        var t = s.trimmingCharacters(in: .whitespacesAndNewlines)
        while t.hasSuffix("/") { t.removeLast() }
        return t
    }
    private static func load<T: Decodable>(_ key: String) -> T? {
        defaults.data(forKey: key).flatMap { try? HubClient.decoder.decode(T.self, from: $0) }
    }
    private static func save<T: Encodable>(_ value: T?, _ key: String) {
        defaults.set(value.flatMap { try? HubClient.encoder.encode($0) }, forKey: key)
    }

    /// Reload widgets and Control Center controls after a change.
    static func refreshSurfaces() {
        #if canImport(WidgetKit)
        WidgetCenter.shared.reloadAllTimelines()
        ControlCenter.shared.reloadAllControls()
        #endif
    }
}
