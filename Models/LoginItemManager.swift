import ServiceManagement

/// Thin wrapper around SMAppService for a "Launch at Login" menu item.
/// SMAppService.mainApp (macOS 13+) is the modern replacement for the old
/// SMLoginItemSetEnabled/LSSharedFileList APIs and, unlike those, doesn't
/// need a separate helper app target -- it registers Jugnu's own app
/// bundle to launch at login.
enum LoginItemManager {
    static var isEnabled: Bool {
        guard #available(macOS 13.0, *) else { return false }
        return SMAppService.mainApp.status == .enabled
    }

    static func toggle() {
        guard #available(macOS 13.0, *) else {
            print("[LoginItemManager] Launch at Login needs macOS 13 or later.")
            return
        }
        do {
            if SMAppService.mainApp.status == .enabled {
                try SMAppService.mainApp.unregister()
            } else {
                try SMAppService.mainApp.register()
            }
        } catch {
            print("[LoginItemManager] failed to toggle launch-at-login: \(error)")
        }
    }
}
