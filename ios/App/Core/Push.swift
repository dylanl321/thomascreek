import UIKit
import UserNotifications

final class AppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    func application(_ application: UIApplication, didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        NotificationActions.register()
        Task { @MainActor in LiveActivities.shared.observe() }   // also runs when iOS wakes the app for Live Activity tokens
        application.registerForRemoteNotifications()
        return true
    }

    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        let hex = deviceToken.map { String(format: "%02x", $0) }.joined()
        Task { @MainActor in
            PushRegistration.shared.token = hex
            PushRegistration.shared.send()
        }
    }

    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {}

    /// iOS finished (or needs us to finish) film downloads while the app was closed.
    func application(_ application: UIApplication, handleEventsForBackgroundURLSession identifier: String,
                     completionHandler: @escaping () -> Void) {
        guard identifier == Downloads.sessionID else { completionHandler(); return }
        Task { @MainActor in
            Downloads.shared.backgroundDone = completionHandler
            Downloads.shared.resume()
        }
    }

    // Show alerts while the app is open too.
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification) async -> UNNotificationPresentationOptions {
        await HomeStore.shared.loadInbox()
        return [.banner, .sound, .list]
    }

    // Tapped a notification or one of its buttons.
    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse) async {
        let info = response.notification.request.content.userInfo
        let cat = info["cat"] as? String
        let inboxID = info["inbox_id"] as? String
        do {
            switch response.actionIdentifier {
            // Holiday lights (unchanged from the Christmas Lights app)
            case NotificationActions.skip: try await LightsAPI.skipTonight(true)
            case NotificationActions.calm: _ = try await LightsAPI.look(.calm)
            case NotificationActions.next: _ = try await LightsAPI.next()
            case NotificationActions.start: _ = try await LightsAPI.start()
            case NotificationActions.off: _ = try await LightsAPI.look(.off)
            // House: approve or deny a child's request straight from the alert
            case NotificationActions.approve, NotificationActions.deny:
                if let inboxID {
                    let approve = response.actionIdentifier == NotificationActions.approve
                    try await HubClient.shared.send("/v1/inbox/\(inboxID)/act", ["action": approve ? "approve" : "deny"],
                                                    confirm: approve ? "Approve: \(response.notification.request.content.title)" : nil)
                }
            case NotificationActions.view where response.notification.request.content.categoryIdentifier == "NEW_DEVICE":
                await MainActor.run { Router.shared.show(.systems, .people) }
            default:
                await MainActor.run {
                    if let ref = info["ref"] as? String, let url = URL(string: ref) { Router.shared.open(url) }
                    else { Router.shared.openCategory(cat) }
                }
            }
        } catch {
            let c = UNMutableNotificationContent()
            c.title = "Thomas Creek"; c.body = "That didn't go through: \(error.localizedDescription)"
            try? await UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: UUID().uuidString, content: c, trigger: nil))
        }
    }
}

/// Buttons on alerts. Category ids match the hub's push sender (cat.upper()).
enum NotificationActions {
    static let skip = "SKIP_TONIGHT", calm = "CALM_NOW", next = "NEXT_SCENE", start = "START_LOOP", off = "LIGHTS_OFF", open = "OPEN"
    static let approve = "APPROVE", deny = "DENY", view = "VIEW", live = "LIVE_VIEW"

    static func register() {
        let skipA = UNNotificationAction(identifier: skip, title: "Skip tonight", options: [.destructive], icon: .init(systemImageName: "moon.zzz"))
        let calmA = UNNotificationAction(identifier: calm, title: "Calm now", options: [], icon: .init(systemImageName: "moon.fill"))
        let nextA = UNNotificationAction(identifier: next, title: "Next scene", options: [], icon: .init(systemImageName: "forward.fill"))
        let startA = UNNotificationAction(identifier: start, title: "Start the loop", options: [], icon: .init(systemImageName: "play.fill"))
        let offA = UNNotificationAction(identifier: off, title: "Lights off", options: [.destructive], icon: .init(systemImageName: "power"))
        let openA = UNNotificationAction(identifier: open, title: "Open controllers", options: [.foreground], icon: .init(systemImageName: "cpu"))
        // Approving can need Face ID (more internet time is secure tier), so these open the app.
        let approveA = UNNotificationAction(identifier: approve, title: "Approve", options: [.foreground, .authenticationRequired], icon: .init(systemImageName: "checkmark"))
        let denyA = UNNotificationAction(identifier: deny, title: "Not now", options: [.authenticationRequired], icon: .init(systemImageName: "xmark"))
        let chooseA = UNNotificationAction(identifier: view, title: "Choose person", options: [.foreground], icon: .init(systemImageName: "person.crop.circle.badge.plus"))
        // Secure-tier things (garage, locks) open the app, where Face ID confirms the action.
        let viewA = UNNotificationAction(identifier: view, title: "Open", options: [.foreground], icon: .init(systemImageName: "arrow.up.forward.app"))
        let liveA = UNNotificationAction(identifier: live, title: "Live view", options: [.foreground], icon: .init(systemImageName: "video.fill"))
        let cats: [UNNotificationCategory] = [
            .init(identifier: "SHOW", actions: [calmA, skipA, offA], intentIdentifiers: []),
            .init(identifier: "HEALTH", actions: [openA, startA], intentIdentifiers: []),
            .init(identifier: "CONTROL", actions: [calmA, offA], intentIdentifiers: []),
            .init(identifier: "SCENE", actions: [nextA], intentIdentifiers: []),
            .init(identifier: "RENDERS", actions: [], intentIdentifiers: []),
            .init(identifier: "MANAGER", actions: [openA], intentIdentifiers: []),
            .init(identifier: "WEATHER", actions: [skipA, calmA], intentIdentifiers: []),
            .init(identifier: "SUMMARY", actions: [], intentIdentifiers: []),
            .init(identifier: "APPROVAL", actions: [approveA, denyA], intentIdentifiers: []),
            .init(identifier: "REQUEST", actions: [approveA, denyA], intentIdentifiers: []),
            .init(identifier: "NEW_DEVICE", actions: [chooseA], intentIdentifiers: []),
            .init(identifier: "SECURE", actions: [viewA], intentIdentifiers: []),
            .init(identifier: "DEVICE", actions: [viewA], intentIdentifiers: []),
            // The doorbell: open the front door camera (the default tap does the same).
            .init(identifier: "DOORBELL", actions: [liveA], intentIdentifiers: []),
        ]
        UNUserNotificationCenter.current().setNotificationCategories(Set(cats))
    }
}

/// Keeps the hub's record of this phone's push token and alert choices current.
@MainActor
final class PushRegistration {
    static let shared = PushRegistration()
    var token: String?
    var allowed: Bool?
    var categories: [Category] = []

    struct Category: Codable, Hashable, Identifiable { var id: String; var title: String; var detail: String; var `default`: Bool; var group: String? }
    private struct Reply: Decodable { var prefs: [String: Bool]?; var categories: [Category]? }

    func refresh() {
        Task {
            let c = UNUserNotificationCenter.current()
            var s = await c.notificationSettings()
            if s.authorizationStatus == .notDetermined {
                _ = try? await c.requestAuthorization(options: [.alert, .sound, .badge, .providesAppNotificationSettings])
                s = await c.notificationSettings()
            }
            allowed = s.authorizationStatus == .authorized || s.authorizationStatus == .provisional
            UIApplication.shared.registerForRemoteNotifications()
        }
    }

    func send() {
        guard let token, AppSettings.deviceID != nil else { return }
        Task {
            var body: [String: Any] = ["apns_token": token, "env": Self.apsEnvironment]
            if !AppSettings.prefs.isEmpty { body["prefs"] = AppSettings.prefs }
            if let r: Reply = try? await HubClient.shared.post("/v1/devices/push", body) {
                if let p = r.prefs { AppSettings.prefs = p }
                if let c = r.categories { categories = c }
            }
        }
    }

    func setPref(_ cat: String, _ on: Bool) {
        var p = AppSettings.prefs; p[cat] = on; AppSettings.prefs = p
        Task { try? await HubClient.shared.send("/v1/devices/prefs", ["prefs": [cat: on]]) }
    }

    /// "development" builds (run from Xcode) use Apple's sandbox push server; TestFlight uses production.
    nonisolated static var apsEnvironment: String {
        guard let url = Bundle.main.url(forResource: "embedded", withExtension: "mobileprovision"),
              let data = try? Data(contentsOf: url), let text = String(data: data, encoding: .isoLatin1) else { return "production" }
        guard let r = text.range(of: "<key>aps-environment</key>") else { return "production" }
        return text[r.upperBound...].prefix(80).contains("development") ? "sandbox" : "production"
    }
}
