import Foundation
import UserNotifications

/// Door and camera alerts leave the Lock Screen and Notification Center by themselves. The hub stamps
/// them with `expires` (Unix seconds, hub/notify.py EXPIRES); this removes delivered ones past it.
/// It runs when the app opens, on the hub's silent "clear" push, and whenever a new alert arrives
/// (the notification extension), so a stale "Someone's at the front door" doesn't stay for hours.
enum NotificationTidy {
    static func sweep() async {
        let center = UNUserNotificationCenter.current()
        let now = Date().timeIntervalSince1970
        let delivered = await center.deliveredNotifications()
        var old: [String] = []
        for n in delivered {
            if let e = (n.request.content.userInfo["expires"] as? NSNumber)?.doubleValue, e <= now {
                old.append(n.request.identifier)
            }
        }
        if !old.isEmpty { center.removeDeliveredNotifications(withIdentifiers: old) }
    }
}
