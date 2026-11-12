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
            if let data = try? await HubClient.shared.raw("GET", path), !data.isEmpty,
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
            return try UNNotificationAttachment(identifier: "picture", url: url, options: nil)
        } catch { return nil }
    }
}
