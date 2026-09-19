import AppKit

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var didInstallMenuBar = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Start the MacBridge helper if it isn't already up. Roughly 130 of
        // Jugnu's 141 tools are HTTP calls to it, so without it running the
        // assistant can talk but can't *do* anything -- every file, mail,
        // calendar, shell, keyboard and WhatsApp tool fails at the first call.
        //
        // MacBridgeLauncher has existed since early on and was never called
        // from anywhere, so this auto-launch has in fact never run: the
        // helper always had to be started by hand (usually as a side effect
        // of MacBridge/build.sh, which ends in `open`). Doing it here, before
        // the window resolves, gives the server a head start on the first
        // turn -- and its own permission prompts, on first run, arrive while
        // the user is still at the keyboard rather than mid-task.
        MacBridgeLauncher.launchIfNeeded()

        // WindowAccessor (drop `.background(WindowAccessor())` into
        // ContentView's root view) posts this the instant SwiftUI attaches
        // the WindowGroup's view to a real NSWindow -- more reliable than
        // polling NSApp.windows, so it wins the race whenever it's wired in.
        NotificationCenter.default.addObserver(
            self, selector: #selector(handleWindowResolved),
            name: .jugnuMainWindowResolved, object: nil
        )

        // Fallback: SwiftUI can finish setting up the window just after
        // this fires rather than strictly before it, and WindowAccessor
        // might not be wired in -- keep polling briefly instead of giving
        // up after a single run-loop hop.
        installMenuBar(attempt: 0)
    }

    @objc private func handleWindowResolved(_ note: Notification) {
        guard !didInstallMenuBar, let window = note.object as? NSWindow else { return }
        didInstallMenuBar = true
        MenuBarController.shared.install(window: window)
    }

    private func installMenuBar(attempt: Int) {
        guard !didInstallMenuBar else { return }

        if let window = Self.resolveContentWindow() {
            didInstallMenuBar = true
            MenuBarController.shared.install(window: window)
            return
        }

        guard attempt < 40 else {
            print("[AppDelegate] no window found after retrying -- MenuBarController not installed")
            return
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { [weak self] in
            self?.installMenuBar(attempt: attempt + 1)
        }
    }

    /// `NSApp.windows.first` isn't guaranteed to be the real chat window --
    /// `NSApp.windows` can include other AppKit-owned windows too, and
    /// silently grabbing the wrong one leaves the menu bar item controlling
    /// a window nobody is looking at (this is the most likely reason
    /// left-click ever felt unreliable). Filter for something that
    /// actually looks like Jugnu's titled, full-size chat window.
    private static func resolveContentWindow() -> NSWindow? {
        let candidates = NSApp.windows.filter {
            $0.styleMask.contains(.titled) && $0.frame.width > 200 && $0.frame.height > 200
        }
        return candidates.first ?? NSApp.windows.first
    }

    /// Flipped from the previous `true`: MenuBarController redirects the
    /// window's close button to a hide rather than a real close (see its
    /// windowWillClose), so leaving this as `true` would still quit the
    /// whole app the instant the window disappeared -- exactly what
    /// living in the menu bar is supposed to avoid.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }
}
