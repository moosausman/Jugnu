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
///
/// Minimizing the chat panel collapses it into a small firefly orb in a
/// corner of the screen (FireflyOrb) rather than the Dock, unless that's
/// turned off in Settings.
@MainActor
final class WindowManager: NSObject, NSWindowDelegate {
    static let shared = WindowManager()

    private var chatWindow: NSWindow?
    private var settingsWindow: NSWindow?
    private var deskWindow: NSWindow?
    private var orb: FireflyOrb?
    /// The chat panel is minimized into the firefly orb.
    private(set) var isCollapsed = false
    private var transitioning = false

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
        if isCollapsed { expandFromFirefly(); return }
        let window = chatWindow ?? makeChatWindow()
        if let button, !window.isMiniaturized { position(window, below: button) }
        present(window)
        NotificationCenter.default.post(name: .jugnuFocusInput, object: nil)
    }

    func hideChat() {
        chatWindow?.orderOut(nil)
        afterWindowHidden()
    }

    // MARK: - Minimize to the firefly

    /// The yellow button, ⌘M and double-clicking the title bar land here.
    func minimizeChat() {
        guard let chat = chatWindow else { return }
        if SettingsStore.shared.minimizeToFirefly {
            collapseToFirefly()
        } else {
            (chat as? ChatWindow)?.miniaturizeToDock()
        }
    }

    private func collapseToFirefly() {
        guard let chat = chatWindow, chat.isVisible, !isCollapsed, !transitioning else { return }
        let orb = self.orb ?? makeOrb()
        orb.prepare(on: chat.screen, unreadAfter: AppState.shared.messages.count)
        isCollapsed = true
        transitioning = true
        // The panel fades while a stand-in shrinks from its frame into the orb.
        morph(from: chat.frame, to: orb.discFrame, growing: false, below: chat) { [weak self] in
            orb.show(animated: false)
            self?.transitioning = false
        }
        NSAnimationContext.runAnimationGroup({ ctx in
            ctx.duration = 0.12
            chat.animator().alphaValue = 0
        }, completionHandler: { [weak self] in
            chat.orderOut(nil)
            chat.alphaValue = 1
            self?.afterWindowHidden()
        })
    }

    private func expandFromFirefly() {
        guard isCollapsed, let chat = chatWindow, !transitioning else { return }
        isCollapsed = false
        transitioning = true
        let from = orb?.discFrame ?? NSRect(x: chat.frame.midX, y: chat.frame.midY, width: 1, height: 1)
        orb?.hide()
        morph(from: from, to: chat.frame, growing: true, below: nil) { [weak self] in
            guard let self else { return }
            chat.alphaValue = 0
            self.present(chat)
            NSAnimationContext.runAnimationGroup({ ctx in
                ctx.duration = 0.12
                chat.animator().alphaValue = 1
            }, completionHandler: { self.transitioning = false })
            NotificationCenter.default.post(name: .jugnuFocusInput, object: nil)
        }
    }

    /// The Settings switch changed. Turning it off while collapsed just
    /// removes the orb; the menu bar icon still opens Jugnu.
    func minimizeToFireflyChanged() {
        guard !SettingsStore.shared.minimizeToFirefly, isCollapsed else { return }
        isCollapsed = false
        orb?.hide()
    }

    private func makeOrb() -> FireflyOrb {
        let orb = FireflyOrb()
        orb.onOpen = { [weak self] in self?.showChat() }
        orb.onSettings = { [weak self] in self?.showSettings() }
        orb.onHide = { SettingsStore.shared.minimizeToFirefly = false }
        self.orb = orb
        return orb
    }

    /// The collapse/expand animation: a dark panel the colour of the chat
    /// window that shrinks into the orb's disc (or grows back out of it)
    /// while the firefly glows up in its centre, so the last frame looks
    /// exactly like the orb. Core Animation in a transparent, click-through
    /// overlay; skipped with Reduce Motion on.
    ///
    /// It deliberately doesn't use a picture of the window: AppKit's own
    /// snapshots leave out the scrolling conversation, and capturing the
    /// real window needs deprecated screen-capture APIs.
    private func morph(from: NSRect, to: NSRect, growing: Bool, below window: NSWindow?,
                       completion: @escaping () -> Void) {
        let screen = NSScreen.screens.first { $0.frame.intersects(growing ? to : from) } ?? NSScreen.main
        guard let area = screen?.frame, !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion else {
            completion()
            return
        }
        let overlay = NSWindow(contentRect: area, styleMask: .borderless, backing: .buffered, defer: false)
        overlay.isOpaque = false
        overlay.backgroundColor = .clear
        overlay.hasShadow = false
        overlay.level = .floating
        overlay.ignoresMouseEvents = true
        overlay.isReleasedWhenClosed = false
        overlay.canHide = false   // Jugnu hides itself as the panel collapses; the morph must stay up
        overlay.collectionBehavior = [.canJoinAllSpaces, .transient, .fullScreenAuxiliary]
        let root = CALayer()
        let host = NSView(frame: NSRect(origin: .zero, size: area.size))
        host.layer = root
        host.wantsLayer = true
        overlay.contentView = host

        func local(_ r: NSRect) -> CGRect { r.offsetBy(dx: -area.minX, dy: -area.minY) }
        let small = local(growing ? from : to), large = local(growing ? to : from)
        let a = growing ? small : large, b = growing ? large : small

        let card = CALayer()
        card.backgroundColor = NSColor(Theme.windowBackground).withAlphaComponent(0.95).cgColor
        card.borderColor = NSColor.white.withAlphaComponent(0.1).cgColor
        card.borderWidth = 1
        card.shadowColor = NSColor.black.cgColor
        card.shadowOpacity = 0.35
        card.shadowRadius = 10
        card.shadowOffset = CGSize(width: 0, height: -3)
        card.frame = b
        card.cornerRadius = growing ? 16 : b.width / 2
        root.addSublayer(card)

        let glyph = CALayer()
        glyph.contents = Self.fireflyGlyph
        glyph.contentsScale = 2
        glyph.bounds = CGRect(x: 0, y: 0, width: 64, height: 64)
        glyph.position = CGPoint(x: b.midX, y: b.midY)
        glyph.opacity = growing ? 0 : 1
        root.addSublayer(glyph)

        if let window {
            overlay.order(.below, relativeTo: window.windowNumber)
        } else {
            overlay.orderFrontRegardless()
        }

        func anim(_ key: String, _ fromValue: Any, _ toValue: Any) -> CABasicAnimation {
            let x = CABasicAnimation(keyPath: key)
            x.fromValue = fromValue
            x.toValue = toValue
            return x
        }
        let duration = 0.32
        let timing = CAMediaTimingFunction(controlPoints: 0.3, 0.0, 0.2, 1.0)
        let cardMotion = CAAnimationGroup()
        cardMotion.animations = [
            anim("position", NSValue(point: NSPoint(x: a.midX, y: a.midY)), NSValue(point: NSPoint(x: b.midX, y: b.midY))),
            anim("bounds.size", NSValue(size: a.size), NSValue(size: b.size)),
            anim("cornerRadius", growing ? a.width / 2 : 16, growing ? 16 : b.width / 2),
        ]
        cardMotion.duration = duration
        cardMotion.timingFunction = timing
        let glyphMotion = CAAnimationGroup()
        glyphMotion.animations = [
            anim("position", NSValue(point: NSPoint(x: a.midX, y: a.midY)), NSValue(point: NSPoint(x: b.midX, y: b.midY))),
            anim("opacity", growing ? 1 : 0, growing ? 0 : 1),
        ]
        glyphMotion.duration = duration
        glyphMotion.timingFunction = timing

        CATransaction.begin()
        CATransaction.setCompletionBlock {
            completion()
            // Let whatever replaces the stand-in draw first.
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { overlay.orderOut(nil) }
        }
        card.add(cardMotion, forKey: "morph")
        glyph.add(glyphMotion, forKey: "morph")
        CATransaction.commit()
    }

    /// The firefly, drawn once as an image for the morph.
    private static let fireflyGlyph: CGImage? = {
        let renderer = ImageRenderer(content: FireflyView(state: .idle, size: 34).frame(width: 64, height: 64))
        renderer.scale = 2
        return renderer.cgImage
    }()

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
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
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

    /// The trading terminal, in Terminal.app. It's a terminal program, so
    /// it belongs in a terminal -- a SwiftUI window over the same engine
    /// was a slower way to reach the same numbers, and one more place for
    /// them to disagree.
    func openTradingTerminal() {
        let script = Bundle.main.path(forResource: "jugnu", ofType: nil, inDirectory: "PythonBackend")
            ?? NSString(string: "~/Desktop/Jugnu_test/PythonBackend/jugnu").expandingTildeInPath
        let source = "tell application \"Terminal\"\n"
            + "  activate\n"
            + "  do script \"clear && '\(script)'\"\n"
            + "end tell"
        DispatchQueue.global(qos: .userInitiated).async {
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
            process.arguments = ["-e", source]
            try? process.run()
        }
    }

    /// Jugnu's desk: a live view of the session Jugnu works in.
    func showDesk() {
        let window = deskWindow ?? makeDeskWindow()
        present(window)
    }

    private func makeDeskWindow() -> NSWindow {
        let window = NSWindow(
            contentRect: NSRect(origin: .zero, size: CGSize(width: 900, height: 640)),
            styleMask: [.titled, .closable, .resizable, .miniaturizable, .fullSizeContentView],
            backing: .buffered, defer: false)
        window.title = "Jugnu's Desk"
        window.titlebarAppearsTransparent = true
        window.appearance = NSAppearance(named: .darkAqua)
        window.backgroundColor = NSColor(Theme.windowBackground)
        window.isReleasedWhenClosed = false
        window.contentMinSize = CGSize(width: 560, height: 420)
        window.contentView = NSHostingView(rootView: DeskView(appState: AppState.shared))
        window.center()
        window.setFrameAutosaveName("JugnuDeskWindow")
        deskWindow = window
        return window
    }

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

    /// A window minimized to the Dock still counts as open: the app keeps
    /// its Dock icon so the minimized window has somewhere to live.
    private var anyWindowVisible: Bool {
        chatWindow?.isVisible == true || chatWindow?.isMiniaturized == true || settingsWindow?.isVisible == true
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

    func windowDidDeminiaturize(_ notification: Notification) {
        updateActivationPolicy()
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

/// The chat panel's window. Escape hides it, like a menu; minimizing goes
/// through WindowManager so it can collapse into the firefly.
private final class ChatWindow: NSWindow {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }

    override func cancelOperation(_ sender: Any?) {
        WindowManager.shared.hideChat()
    }

    override func miniaturize(_ sender: Any?) {
        WindowManager.shared.minimizeChat()
    }

    /// The standard minimize to the Dock, for when the firefly is turned off.
    func miniaturizeToDock() {
        super.miniaturize(nil)
    }
}
