import AppKit

import Combine

import SwiftUI

/// Puts Jugnu in the menu bar instead of the Dock, the way Ollama's

/// background app works: no Dock icon, no Cmd+Tab entry, just a status

/// item shaped like Jugnu's firefly. Left-click shows/activates the chat

/// window, or hides it if it's already the frontmost, focused thing on

/// screen; right-click (or Control-click) opens a normal context menu

/// with the rest of the app's controls -- Clear Conversation, Request

/// Daily Review, Launch at Login, Quit.

///

/// Call `install(window:)` once the window exists -- see AppDelegate /

/// WindowAccessor for how the window reference gets here.

final class MenuBarController: NSObject {

    static let shared = MenuBarController()

    private var statusItem: NSStatusItem?

    private weak var window: NSWindow?

    private weak var appState: AppState?

    private var cancellables = Set<AnyCancellable>()

    private var flutterTimer: Timer?

    private var flutterPhase = false

    private var currentFireflyState: FireflyState = .idle

    private override init() { super.init() }

    // MARK: - Install

    func install(window: NSWindow) {

        self.window = window

        // No Dock icon, no Cmd+Tab entry -- purely a menu bar presence.

        // (Equivalent to setting LSUIElement = YES in Info.plist; doing

        // it here instead keeps it in one place with the rest of this.)

        NSApp.setActivationPolicy(.accessory)

        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)

        if let button = item.button {

            button.image = FireflyStatusIcon.image(for: .idle, flutter: false)

            button.action = #selector(statusItemClicked)

            button.target = self

            button.sendAction(on: [.leftMouseUp, .rightMouseUp])

        }

        statusItem = item

        // The red close button (and Cmd+W) should hide the window, not

        // destroy it -- the status item is now the only way back in, and

        // a deallocated window can't be reshown.

        window.isReleasedWhenClosed = false

        NotificationCenter.default.addObserver(

            self, selector: #selector(windowWillClose),

            name: NSWindow.willCloseNotification, object: window

        )

        // A tapped notification (see NotificationManager) should raise

        // the window the same way clicking the status item does.

        NotificationCenter.default.addObserver(

            self, selector: #selector(bringToFront),

            name: .jugnuNotificationTapped, object: nil

        )

        bindAppStateIfNeeded(attempt: 0)

    }

    /// AppState is created by ContentView as a @StateObject, so it may

    /// not exist the instant install(window:) runs. Retry briefly rather

    /// than requiring a specific init order between the two.

    private func bindAppStateIfNeeded(attempt: Int) {

        if let appState = AppState.shared {

            bind(to: appState)

            return

        }

        guard attempt < 40 else { return } // give up quietly after ~2s

        DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { [weak self] in

            self?.bindAppStateIfNeeded(attempt: attempt + 1)

        }

    }

    private func bind(to appState: AppState) {

        self.appState = appState

        cancellables.removeAll()

        Publishers.CombineLatest(appState.$thinking, appState.$errorMessage)

            .receive(on: DispatchQueue.main)

            .sink { [weak self] thinking, errorMessage in

                let state: FireflyState = errorMessage != nil ? .error : (thinking ? .thinking : .idle)

                self?.setFireflyState(state)

            }

            .store(in: &cancellables)

    }

    // MARK: - Icon

    private func setFireflyState(_ state: FireflyState) {

        guard state != currentFireflyState else { return }

        currentFireflyState = state

        flutterTimer?.invalidate()

        flutterTimer = nil

        guard state == .thinking else {

            statusItem?.button?.image = FireflyStatusIcon.image(for: state, flutter: false)

            return

        }

        // Alternate between two wing angles while thinking -- a small,

        // readable "wing-beat" in the menu bar rather than an animated

        // image format. .common run loop mode keeps it going even while a

        // menu is tracking or the window is being dragged.

        let timer = Timer(timeInterval: 0.32, repeats: true) { [weak self] _ in

            guard let self else { return }

            self.flutterPhase.toggle()

            self.statusItem?.button?.image = FireflyStatusIcon.image(for: .thinking, flutter: self.flutterPhase)

        }

        RunLoop.main.add(timer, forMode: .common)

        flutterTimer = timer

    }

    // MARK: - Click handling

    @objc private func statusItemClicked() {

        let event = NSApp.currentEvent

        let isSecondaryClick = event?.type == .rightMouseUp || event?.modifierFlags.contains(.control) == true

        if isSecondaryClick {

            showContextMenu()

        } else {

            toggleWindow()

        }

    }

    private func toggleWindow() {

        guard let window else { return }

        // Only hide if it's genuinely the frontmost, focused thing --

        // otherwise every click activates it. Treating "merely visible"

        // as "hide it" is what made the icon feel unresponsive any time

        // the chat window was open but not the focused app (e.g. you'd

        // switched away): the old logic would just hide an

        // already-unfocused window instead of bringing it forward.

        if window.isVisible, window.isKeyWindow {

            window.orderOut(nil)

        } else {

            positionUnderStatusItem(window)

            bringToFront()

        }

    }

    @objc private func toggleWindowFromMenu() {

        toggleWindow()

    }

    @objc private func bringToFront() {

        guard let window else { return }

        NSApp.activate(ignoringOtherApps: true)

        window.makeKeyAndOrderFront(nil)

    }

    @objc private func windowWillClose(_ note: Notification) {

        guard let window, (note.object as? NSWindow) === window else { return }

        window.orderOut(nil)

    }

    // MARK: - Context menu

    private func showContextMenu() {

        let menu = buildMenu()

        // Attach, pop it open, then detach -- otherwise a menu sitting on

        // the status item permanently would hijack every future

        // left-click too, since AppKit prefers .menu over .action.

        statusItem?.menu = menu

        statusItem?.button?.performClick(nil)

        statusItem?.menu = nil

    }

    private func buildMenu() -> NSMenu {

        let menu = NSMenu()

        let isFrontmost = (window?.isVisible == true) && (window?.isKeyWindow == true)

        menu.addItem(NSMenuItem(

            title: isFrontmost ? "Hide Jugnu" : "Show Jugnu",

            action: #selector(toggleWindowFromMenu), keyEquivalent: ""

        ))

        if let appState {

            let status = NSMenuItem(title: statusDescription(for: appState), action: nil, keyEquivalent: "")

            status.isEnabled = false

            menu.addItem(status)

        }

        menu.addItem(.separator())

        let clearItem = NSMenuItem(title: "Clear Conversation", action: #selector(clearConversation), keyEquivalent: "")

        clearItem.isEnabled = (appState?.thinking == false)

        menu.addItem(clearItem)

        let dailyReviewItem = NSMenuItem(title: "Request Daily Review", action: #selector(requestDailyReview), keyEquivalent: "")

        dailyReviewItem.isEnabled = (appState?.thinking == false)

        menu.addItem(dailyReviewItem)

        menu.addItem(.separator())

        menu.addItem(.separator())

        if #available(macOS 14.0, *) {

            let settingsItem = NSMenuItem()

            let settingsView = NSHostingView(rootView: MenuBarSettingsLink())

            settingsView.frame = NSRect(x: 0, y: 0, width: 220, height: 26)

            settingsItem.view = settingsView

            menu.addItem(settingsItem)

        } else {

            let settingsItem = NSMenuItem(

                title: "Settings…",

                action: #selector(openLegacySettings),

                keyEquivalent: ","

            )

            menu.addItem(settingsItem)

        }

        let launchItem = NSMenuItem(title: "Launch at Login", action: #selector(toggleLaunchAtLogin), keyEquivalent: "")

        launchItem.state = LoginItemManager.isEnabled ? .on : .off

        menu.addItem(launchItem)

        menu.addItem(.separator())

        menu.addItem(NSMenuItem(title: "Quit Jugnu", action: #selector(quit), keyEquivalent: "q"))

        menu.items.forEach { $0.target = self }

        return menu

    }

    private func statusDescription(for appState: AppState) -> String {

        switch appState.statusLabel {

        case "thinking":

            return "Jugnu is thinking…"

        case "error":

            return appState.errorMessage.map { "Error: \($0)" } ?? "Something went wrong"

        case "starting":

            return "Starting…"

        default:

            return appState.modelName.isEmpty ? "Ready" : "Ready — \(appState.modelName)"

        }

    }

    @objc private func clearConversation() {

        appState?.clear()

    }

    @objc private func requestDailyReview() {

        appState?.requestDailyReview()

    }

    @objc private func toggleLaunchAtLogin() {

        LoginItemManager.toggle()

    }

    /// Compatibility path for systems older than macOS 14, where

    /// SwiftUI.SettingsLink is unavailable.

    @objc private func openLegacySettings() {

        NSApp.activate(ignoringOtherApps: true)

        NSApp.sendAction(Selector(("showSettingsWindow:")), to: nil, from: nil)

    }

    @objc private func quit() {

        NSApp.terminate(nil)

    }

    private func positionUnderStatusItem(_ window: NSWindow) {

        guard let buttonWindow = statusItem?.button?.window,

              let screen = buttonWindow.screen else { return }

        let itemFrame = buttonWindow.frame

        let x = itemFrame.midX - window.frame.width / 2

        let y = itemFrame.minY - window.frame.height - 6

        let minX = screen.visibleFrame.minX + 8

        let maxX = screen.visibleFrame.maxX - window.frame.width - 8

        window.setFrameOrigin(NSPoint(x: min(max(x, minX), maxX), y: y))

    }

}


@available(macOS 14.0, *)

private struct MenuBarSettingsLink: View {

    @State private var isHovering = false

    var body: some View {

        SettingsLink {

            HStack(spacing: 8) {

                Text("Settings…")

                Spacer()

                Text("⌘,")

                    .foregroundStyle(isHovering ? Color.white.opacity(0.8) : Color.secondary)

            }

            .padding(.horizontal, 12)

            .frame(width: 220, height: 26)

            .contentShape(Rectangle())

            .foregroundStyle(isHovering ? Color.white : Color.primary)

            .background(isHovering ? Color.accentColor : Color.clear)

        }

        .buttonStyle(.plain)

        .keyboardShortcut(",", modifiers: .command)

        .onHover { isHovering = $0 }

    }

}


