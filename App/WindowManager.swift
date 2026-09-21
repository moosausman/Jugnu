import AppKit
import SwiftUI

/// Owns Jugnu's two windows -- the chat panel and Settings -- and the app's
/// activation policy.
///
/// Jugnu lives in the menu bar. A menu-bar-only (accessory) app can't own
/// the menu bar, though: while its window is in front, macOS keeps showing
/// the previous app's menus. So while a Jugnu window is open the app
/// becomes a regular app (its own menus, a Dock icon, ⌘-Tab), and when the
/// last one closes it goes back to living only in the menu bar. The
/// "Show in Dock while open" setting turns that off.
@MainActor
final class WindowManager: NSObject, NSWindowDelegate {
    static let shared = WindowManager()

    private var chatWindow: NSWindow?
    private var settingsWindow: NSWindow?

    private static let chatAutosaveName = "JugnuChatPanel"

    private override init() {
        super.init()
        let center = NotificationCenter.default
        center.addObserver(forName: NSApplication.didHideNotification, object: nil, queue: .main) { _ in
            Task { @MainActor in WindowManager.shared.updateActivationPolicy() }
        }
        center.addObserver(forName: NSApplication.didUnhideNotification, object: nil, queue: .main) { _ in
            Task { @MainActor in WindowManager.shared.updateActivationPolicy() }
        }
    }

    // MARK: - Chat

    var isChatFrontmost: Bool {
        guard let chatWindow else { return false }
        return NSApp.isActive && chatWindow.isVisible && chatWindow.isKeyWindow
    }

    /// Shows the chat panel. From the menu bar icon it drops down under the
    /// icon, like a menu; from anywhere else it opens where it was left.
    func showChat(below button: NSStatusBarButton? = nil) {
        let window = chatWindow ?? makeChatWindow()
        if let button { position(window, below: button) }
        present(window)
        NotificationCenter.default.post(name: .jugnuFocusInput, object: nil)
    }

    func hideChat() {
        chatWindow?.orderOut(nil)
        afterWindowHidden()
    }

    func toggleChat(below button: NSStatusBarButton? = nil) {
        isChatFrontmost ? hideChat() : showChat(below: button)
    }

    func showActivity() {
        showChat()
        NotificationCenter.default.post(name: .jugnuShowActivity, object: nil)
    }

    func toggleActivity() {
        showChat()
        NotificationCenter.default.post(name: .jugnuToggleActivity, object: nil)
    }

    private func makeChatWindow() -> NSWindow {
        let window = ChatWindow(
            contentRect: NSRect(origin: .zero, size: Theme.WindowSize.initial),
            styleMask: [.titled, .closable, .resizable, .fullSizeContentView],
            backing: .buffered, defer: false)
        window.title = "Jugnu"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.isMovableByWindowBackground = true
        window.isOpaque = false
        window.backgroundColor = .clear
        window.hasShadow = true
        window.appearance = NSAppearance(named: .darkAqua)
        // A quick-access panel: stays above other apps and follows you
        // between Spaces. .floating is still far below the menu bar and
        // the rest of the system UI.
        window.level = .floating
        window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        window.contentMinSize = Theme.WindowSize.min
        window.contentMaxSize = Theme.WindowSize.max
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.standardWindowButton(.miniaturizeButton)?.isHidden = true
        window.standardWindowButton(.zoomButton)?.isHidden = true

        let host = NSHostingView(rootView: ContentView(appState: AppState.shared))
        host.sizingOptions = [.minSize]
        window.contentView = host

        let hasSavedFrame = UserDefaults.standard.string(forKey: "NSWindow Frame \(Self.chatAutosaveName)") != nil
        window.setFrameAutosaveName(Self.chatAutosaveName)
        if !hasSavedFrame {
            window.setContentSize(Theme.WindowSize.initial)
            window.center()
            if let button = MenuBarController.shared.statusButton { position(window, below: button) }
        }
        chatWindow = window
        return window
    }

    private func position(_ window: NSWindow, below button: NSStatusBarButton) {
        guard let itemWindow = button.window, let screen = itemWindow.screen ?? NSScreen.main else { return }
        let item = itemWindow.frame
        // Right after launch the icon may not be placed in the menu bar yet;
        // its frame then isn't at the top of any screen.
        guard item.width > 0, abs(item.maxY - screen.frame.maxY) < 60,
              item.midX > screen.frame.minX, item.midX < screen.frame.maxX else {
            if !window.isVisible { window.center() }
            return
        }
        let visible = screen.visibleFrame
        let size = window.frame.size
        let top = min(item.minY, visible.maxY) - 6
        let x = min(max(item.midX - size.width / 2, visible.minX + 8), visible.maxX - size.width - 8)
        let y = max(top - size.height, visible.minY + 8)
        window.setFrameOrigin(NSPoint(x: x, y: y))
    }

    // MARK: - Settings

    func showSettings() {
        let window = settingsWindow ?? makeSettingsWindow()
        present(window)
    }

    private func makeSettingsWindow() -> NSWindow {
        let window = NSWindow(
            contentRect: NSRect(origin: .zero, size: Theme.WindowSize.settings),
            styleMask: [.titled, .closable, .fullSizeContentView],
            backing: .buffered, defer: false)
        window.title = "Jugnu Settings"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.isMovableByWindowBackground = true
        window.appearance = NSAppearance(named: .darkAqua)
        window.backgroundColor = NSColor(Theme.windowBackground)
        // Same level as the chat panel, so it never opens hidden behind it.
        window.level = .floating
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.standardWindowButton(.miniaturizeButton)?.isHidden = true
        window.standardWindowButton(.zoomButton)?.isHidden = true
        let host = NSHostingView(rootView: SettingsView(appState: AppState.shared))
        host.sizingOptions = []
        window.contentView = host
        window.setContentSize(Theme.WindowSize.settings)
        window.center()
        settingsWindow = window
        return window
    }

    // MARK: - Presenting, hiding, activation policy

    private func present(_ window: NSWindow) {
        if NSApp.isHidden { NSApp.unhide(nil) }
        window.makeKeyAndOrderFront(nil)
        updateActivationPolicy()
        NSApp.activate(ignoringOtherApps: true)
    }

    private func afterWindowHidden() {
        updateActivationPolicy()
        // With nothing of Jugnu's left on screen, hand focus back to
        // whatever you were using before.
        if NSApp.isActive && !anyWindowVisible { NSApp.hide(nil) }
    }

    private var anyWindowVisible: Bool {
        chatWindow?.isVisible == true || settingsWindow?.isVisible == true
    }

    func updateActivationPolicy() {
        let wantsRegular = SettingsStore.shared.showInDockWhenOpen && anyWindowVisible && !NSApp.isHidden
        let target: NSApplication.ActivationPolicy = wantsRegular ? .regular : .accessory
        guard NSApp.activationPolicy() != target else { return }
        NSApp.setActivationPolicy(target)
        if target == .regular && NSApp.isActive {
            // The menu bar only switches to a newly-regular app on its next
            // activation, so step aside for a moment and come straight back.
            NSRunningApplication.runningApplications(withBundleIdentifier: "com.apple.dock").first?.activate()
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) {
                NSApp.activate(ignoringOtherApps: true)
            }
        }
    }

    // MARK: - NSWindowDelegate

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        // The chat panel hides instead of closing; the menu bar icon, the
        // Dock icon or ⌘-Tab bring it straight back with everything intact.
        if sender === chatWindow {
            hideChat()
            return false
        }
        return true
    }

    func windowWillClose(_ notification: Notification) {
        guard (notification.object as? NSWindow) === settingsWindow else { return }
        DispatchQueue.main.async { self.afterWindowHidden() }
    }

    func windowDidBecomeKey(_ notification: Notification) {
        if (notification.object as? NSWindow) === chatWindow {
            NotificationCenter.default.post(name: .jugnuFocusInput, object: nil)
        }
    }
}

extension Notification.Name {
    static let jugnuToggleActivity = Notification.Name("jugnuToggleActivity")
    static let jugnuShowActivity = Notification.Name("jugnuShowActivity")
}

/// The chat panel's window. Escape hides it, like a menu.
private final class ChatWindow: NSWindow {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }

    override func cancelOperation(_ sender: Any?) {
        WindowManager.shared.hideChat()
    }
}
