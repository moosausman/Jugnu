import AppKit

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationWillFinishLaunching(_ notification: Notification) {
        // Menu bar first; WindowManager promotes the app to a regular one
        // (menus, Dock icon) while a window is open.
        NSApp.setActivationPolicy(.accessory)
        // The panel doesn't go full screen, so don't offer it in the View menu.
        UserDefaults.standard.set(false, forKey: "NSFullScreenMenuItemEverywhere")
    }

    @MainActor
    func applicationDidFinishLaunching(_ notification: Notification) {
        // Nearly every tool is an HTTP call to MacBridge, so start it early:
        // its first-run permission prompts then arrive while you're at the keyboard.
        MacBridgeLauncher.launchIfNeeded()
        AppState.shared.launchBackend()
        MenuBarController.shared.install()
        HotKeys.shared.install()
        WindowManager.shared.showChat()
    }

    /// Dock icon clicked (or the app reopened) with the panel hidden.
    @MainActor
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        WindowManager.shared.showChat()
        return false
    }

    /// Closing the panel only hides it; Jugnu keeps running in the menu bar.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }
}
