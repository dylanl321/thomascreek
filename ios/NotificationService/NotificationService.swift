import UserNotifications

/// Adds the camera picture to an alert. The hub sends only a path (`image`); the picture is fetched
/// from the hub with this phone's own sign-in, so nothing private passes through Apple.
final class NotificationService: UNNotificationServiceExtension {
    private var handler: ((UNNotificationContent) -> Void)?
    private var best: UNMutableNotificationContent?
    private var task: Task<Void, Never>?

    override func didReceive(_ request: UNNotificationRequest, withContentHandler contentHandler: @escaping (UNNotificationContent) -> Void) {
        handler = contentHandler
        let content = (request.content.mutableCopy() as? UNMutableNotificationContent) ?? UNMutableNotificationContent()
        best = content
        guard let path = content.userInfo["image"] as? String, path.hasPrefix("/v1/") else {
            contentHandler(content); return
        }
        task = Task {
            // A repeat of the same alert replaces the one on screen (apns-collapse-id). The new
            // payload has to win, including its picture: ignore the cached download, and give the
            // attachment a new id so Notification Center doesn't keep the previous snapshot.
            if let data = try? await HubClient.shared.raw("GET", path, fresh: true), !data.isEmpty,
               let attachment = Self.attachment(data) {
                content.attachments = [attachment]
            }
            finish()
        }
    }

    override func serviceExtensionTimeWillExpire() {
        task?.cancel()
        finish()
    }

    private func finish() {
        guard let h = handler, let c = best else { return }
        handler = nil
        h(c)
    }

    private static func attachment(_ data: Data) -> UNNotificationAttachment? {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".jpg")
        do {
            try data.write(to: url)
            return try UNNotificationAttachment(identifier: UUID().uuidString, url: url, options: nil)
        } catch { return nil }
    }
}
