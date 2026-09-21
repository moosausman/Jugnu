import AppKit
import Combine
import SwiftUI

/// Where the chat panel goes when you minimize it: a small firefly orb that
/// floats in a corner of the screen (bottom-right until you drag it).
/// Click to open Jugnu again; right-click for a menu. It keeps showing what
/// Jugnu is doing -- a faster wing-beat while it works, red on an error, and
/// a dot when a reply or an approval is waiting.
@MainActor
final class FireflyOrb {
    /// The visible disc's diameter; the window around it leaves room for the glow.
    static let discSize: CGFloat = 52
    private static let windowSize: CGFloat = 96
    private static let positionKey = "jugnu.fireflyOrbCenter"

    private let panel: OrbPanel
    private let content: OrbContentView
    private let model = OrbModel()

    var onOpen: () -> Void = {}
    var onSettings: () -> Void = {}
    var onHide: () -> Void = {}

    init() {
        panel = OrbPanel(size: Self.windowSize)
        content = OrbContentView(frame: NSRect(x: 0, y: 0, width: Self.windowSize, height: Self.windowSize))
        panel.contentView = content
        content.onClick = { [weak self] in self?.onOpen() }
        content.onMoved = { [weak self] in self?.savePosition() }
        content.menuProvider = { [weak self] in self?.menu() }
        content.model = model
    }

    var isVisible: Bool { panel.isVisible }

    /// The disc's frame in screen coordinates -- what the window collapses into.
    var discFrame: NSRect {
        let f = panel.frame
        return NSRect(x: f.midX - Self.discSize / 2, y: f.midY - Self.discSize / 2,
                      width: Self.discSize, height: Self.discSize)
    }

    /// Puts the orb in place (without showing it). `screen` is where the
    /// window was, used when there's no remembered spot on any screen.
    func prepare(on screen: NSScreen?, unreadAfter baseline: Int) {
        let host = NSHostingView(rootView: FireflyOrbView(appState: AppState.shared, model: model, baseline: baseline))
        host.frame = content.bounds
        host.autoresizingMask = [.width, .height]
        content.subviews.forEach { $0.removeFromSuperview() }
        content.addSubview(host)
        let center = savedCenter() ?? defaultCenter(on: screen ?? NSScreen.main)
        panel.setFrameOrigin(NSPoint(x: center.x - Self.windowSize / 2, y: center.y - Self.windowSize / 2))
    }

    func show(animated: Bool) {
        panel.alphaValue = animated ? 0 : 1
        panel.orderFrontRegardless()
        if animated {
            NSAnimationContext.runAnimationGroup { ctx in
                ctx.duration = 0.18
                panel.animator().alphaValue = 1
            }
        }
    }

    func hide() {
        panel.orderOut(nil)
        model.hovering = false
    }

    // MARK: Position

    private func defaultCenter(on screen: NSScreen?) -> NSPoint {
        let visible = (screen ?? NSScreen.screens.first)?.visibleFrame ?? .zero
        return NSPoint(x: visible.maxX - 46, y: visible.minY + 46)
    }

    private func savedCenter() -> NSPoint? {
        guard let raw = UserDefaults.standard.string(forKey: Self.positionKey) else { return nil }
        let point = NSPointFromString(raw)
        // Only if it's still on a connected screen.
        let onScreen = NSScreen.screens.contains { $0.visibleFrame.insetBy(dx: 20, dy: 20).contains(point) }
        return onScreen ? point : nil
    }

    private func savePosition() {
        let f = panel.frame
        UserDefaults.standard.set(NSStringFromPoint(NSPoint(x: f.midX, y: f.midY)), forKey: Self.positionKey)
    }

    // MARK: Menu

    private func menu() -> NSMenu {
        let menu = NSMenu()
        menu.addItem(ClosureMenuItem("Open Jugnu", symbol: "macwindow", action: onOpen))
        menu.addItem(ClosureMenuItem("Settings…", symbol: "gearshape", action: onSettings))
        menu.addItem(.separator())
        menu.addItem(ClosureMenuItem("Hide Firefly", symbol: "eye.slash", action: onHide))
        return menu
    }
}

// MARK: - Window and events

private final class OrbPanel: NSPanel {
    init(size: CGFloat) {
        super.init(contentRect: NSRect(x: 0, y: 0, width: size, height: size),
                   styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        isOpaque = false
        backgroundColor = .clear
        hasShadow = false
        level = .floating
        collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle, .fullScreenAuxiliary]
        hidesOnDeactivate = false
        canHide = false              // stays put when Jugnu hides itself
        isReleasedWhenClosed = false
        isMovable = false            // dragged by OrbContentView, so a click can't start a move
    }

    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

@MainActor
final class OrbModel: ObservableObject {
    @Published var hovering = false
}

/// Handles the orb's mouse: click opens, drag moves, right-click shows the menu.
private final class OrbContentView: NSView {
    var onClick: () -> Void = {}
    var onMoved: () -> Void = {}
    var menuProvider: () -> NSMenu? = { nil }
    var model: OrbModel?

    private var downPoint: NSPoint?
    private var startOrigin: NSPoint = .zero
    private var dragged = false

    override init(frame: NSRect) {
        super.init(frame: frame)
        toolTip = "Open Jugnu"
        addTrackingArea(NSTrackingArea(rect: .zero, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect],
                                       owner: self, userInfo: nil))
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) is not used") }

    // Every point inside is the orb's, not the SwiftUI view's.
    override func hitTest(_ point: NSPoint) -> NSView? {
        frame.contains(point) ? self : nil
    }

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func mouseEntered(with event: NSEvent) { model?.hovering = true }
    override func mouseExited(with event: NSEvent) { model?.hovering = false }

    override func mouseDown(with event: NSEvent) {
        downPoint = NSEvent.mouseLocation
        startOrigin = window?.frame.origin ?? .zero
        dragged = false
    }

    override func mouseDragged(with event: NSEvent) {
        guard let downPoint, let window else { return }
        let now = NSEvent.mouseLocation
        let dx = now.x - downPoint.x, dy = now.y - downPoint.y
        if !dragged && hypot(dx, dy) < 4 { return }
        dragged = true
        window.setFrameOrigin(NSPoint(x: startOrigin.x + dx, y: startOrigin.y + dy))
    }

    override func mouseUp(with event: NSEvent) {
        defer { downPoint = nil }
        if dragged {
            onMoved()
        } else {
            onClick()
        }
    }

    override func rightMouseDown(with event: NSEvent) {
        guard let menu = menuProvider() else { return }
        NSMenu.popUpContextMenu(menu, with: event, for: self)
    }
}

/// A menu item that runs a closure.
private final class ClosureMenuItem: NSMenuItem {
    private let handler: () -> Void

    init(_ title: String, symbol: String, action: @escaping () -> Void) {
        handler = action
        super.init(title: title, action: #selector(run), keyEquivalent: "")
        target = self
        image = NSImage(systemSymbolName: symbol, accessibilityDescription: nil)
    }

    required init(coder: NSCoder) { fatalError("init(coder:) is not used") }

    @objc private func run() { handler() }
}

// MARK: - The orb itself

private struct FireflyOrbView: View {
    @ObservedObject var appState: AppState
    @ObservedObject var model: OrbModel
    /// Message count when the window collapsed; anything newer from Jugnu is unread.
    let baseline: Int

    private var state: FireflyState {
        if appState.errorMessage != nil { return .error }
        return appState.thinking ? .thinking : .idle
    }

    private var badge: Color? {
        if !appState.approvals.isEmpty { return Theme.danger }
        let unread = appState.messages.count > baseline && appState.messages.last?.role == .assistant
        return unread ? Theme.clay : nil
    }

    var body: some View {
        ZStack {
            Circle()
                .fill(Theme.windowBackground.opacity(0.94))
                .overlay(Circle().stroke(Color.white.opacity(model.hovering ? 0.2 : 0.1), lineWidth: 1))
                .frame(width: FireflyOrb.discSize, height: FireflyOrb.discSize)
                .shadow(color: .black.opacity(0.35), radius: 9, y: 3)
            FireflyView(state: state, size: 34)
            if let badge {
                Circle()
                    .fill(badge)
                    .frame(width: 9, height: 9)
                    .overlay(Circle().stroke(Theme.windowBackground, lineWidth: 1.5))
                    .offset(x: 18, y: -18)
            }
        }
        .scaleEffect(model.hovering ? 1.07 : 1)
        .animation(Theme.Motion.snappy, value: model.hovering)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .preferredColorScheme(.dark)
    }
}
