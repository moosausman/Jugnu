import AppKit
import UserNotifications

/// Thin wrapper around local (non-push) notifications for nudges,
/// digests, and finished replies. Call `requestAuthorization()` once at
/// launch -- `notify(...)` is safe to call any time after that; if the
/// person hasn't granted permission yet, UNUserNotificationCenter just
/// silently drops the request instead of failing.
final class NotificationManager: NSObject, UNUserNotificationCenterDelegate {
    static let shared = NotificationManager()

    private override init() {
        super.init()
        UNUserNotificationCenter.current().delegate = self
    }

    func requestAuthorization() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, error in
            if let error {
                print("Jugnu: notification authorization failed -- \(error)")
            }
        }
    }

    func notify(title: String, body: String) {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = .default

        let request = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request)
    }

    /// macOS suppresses banners from the frontmost app by default, which
    /// would make this a no-op on the one path that matters -- AppState
    /// already gates `notify()` behind `!NSApp.isActive`, so anything
    /// that gets here should actually show.
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .sound])
    }

    /// Tapping the banner should bring Jugnu to the front, not just
    /// dismiss it. MenuBarController listens for this notification name
    /// to actually raise the window.
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        NSApp.activate(ignoringOtherApps: true)
        NotificationCenter.default.post(name: .jugnuNotificationTapped, object: nil)
        completionHandler()
    }
}

extension Notification.Name {
    static let jugnuNotificationTapped = Notification.Name("jugnuNotificationTapped")
}
