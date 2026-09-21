import AppKit
import Combine
import SwiftUI

/// Jugnu's menu bar icon. Left-click opens or hides the chat panel;
/// right-click (or Control-click) opens a short menu. The firefly beats its
/// wings while Jugnu works and gets a dot when something needs your OK.
@MainActor
final class MenuBarController: NSObject {
    static let shared = MenuBarController()

    private var statusItem: NSStatusItem?
    private var cancellables = Set<AnyCancellable>()
    private var flutterTimer: Timer?
    private var flutterPhase = false
    private var iconState: FireflyState = .idle
    private var iconBadge = false

    var statusButton: NSStatusBarButton? { statusItem?.button }

    private var appState: AppState { AppState.shared }

    private override init() { super.init() }

    func install() {
        guard statusItem == nil else { return }
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        if let button = item.button {
            button.image = FireflyStatusIcon.image(for: .idle, flutter: false)
            button.setAccessibilityTitle("Jugnu")
            button.action = #selector(statusItemClicked)
            button.target = self
            button.sendAction(on: [.leftMouseUp, .rightMouseUp])
        }
        statusItem = item

        NotificationCenter.default.addObserver(
            forName: .jugnuNotificationTapped, object: nil, queue: .main
        ) { _ in
            Task { @MainActor in WindowManager.shared.showChat() }
        }

        Publishers.CombineLatest3(appState.$thinking, appState.$errorMessage, appState.$approvals)
            .receive(on: DispatchQueue.main)
            .sink { [weak self] thinking, error, approvals in
                let state: FireflyState = error != nil ? .error : (thinking ? .thinking : .idle)
                self?.updateIcon(state: state, badge: !approvals.isEmpty)
            }
            .store(in: &cancellables)
    }

    // MARK: - Icon

    private func updateIcon(state: FireflyState, badge: Bool) {
        guard state != iconState || badge != iconBadge else { return }
        iconState = state
        iconBadge = badge
        flutterTimer?.invalidate()
        flutterTimer = nil
        statusItem?.button?.image = FireflyStatusIcon.image(for: state, flutter: false, badge: badge)
        guard state == .thinking else { return }

        // A two-frame wing-beat while working. .common mode keeps it going
        // while a menu is open or a window is being dragged.
        let timer = Timer(timeInterval: 0.32, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self else { return }
                self.flutterPhase.toggle()
                self.statusItem?.button?.image = FireflyStatusIcon.image(
                    for: .thinking, flutter: self.flutterPhase, badge: self.iconBadge)
            }
        }
        RunLoop.main.add(timer, forMode: .common)
        flutterTimer = timer
    }

    // MARK: - Clicks

    @objc private func statusItemClicked() {
        let event = NSApp.currentEvent
        if event?.type == .rightMouseUp || event?.modifierFlags.contains(.control) == true {
            showMenu()
        } else {
            WindowManager.shared.toggleChat(below: statusItem?.button)
        }
    }

    private func showMenu() {
        // Attached only for this click: a menu left on the item would take
        // over left-clicks too.
        statusItem?.menu = buildMenu()
        statusItem?.button?.performClick(nil)
        statusItem?.menu = nil
    }

    // MARK: - Menu

    private func buildMenu() -> NSMenu {
        let menu = NSMenu()
        menu.autoenablesItems = false

        let header = NSMenuItem()
        let headerView = NSHostingView(rootView: MenuHeader(
            status: statusText, detail: detailText, color: statusColor))
        headerView.frame = NSRect(x: 0, y: 0, width: 250, height: detailText == nil ? 34 : 46)
        header.view = headerView
        menu.addItem(header)
        menu.addItem(.separator())

        let frontmost = WindowManager.shared.isChatFrontmost
        menu.addItem(item(frontmost ? "Hide Jugnu" : "Open Jugnu", symbol: "macwindow",
                          action: #selector(toggleChat)))
        let newChat = item("New Conversation", symbol: "square.and.pencil", action: #selector(newConversation))
        newChat.isEnabled = !appState.thinking && !appState.messages.isEmpty
        menu.addItem(newChat)
        if appState.thinking {
            menu.addItem(item("Stop", symbol: "stop.circle", action: #selector(stop)))
        } else {
            menu.addItem(item("Daily Review", symbol: "sun.max", action: #selector(dailyReview)))
        }
        let activity = item(appState.approvals.isEmpty ? "Activity" : "Activity — needs your OK",
                            symbol: "list.bullet.rectangle", action: #selector(showActivity))
        menu.addItem(activity)

        menu.addItem(.separator())
        menu.addItem(item("Settings…", symbol: "gearshape", action: #selector(openSettings), key: ","))
        let login = item("Launch at Login", symbol: nil, action: #selector(toggleLaunchAtLogin))
        login.state = LoginItemManager.isEnabled ? .on : .off
        menu.addItem(login)

        menu.addItem(.separator())
        menu.addItem(item("Quit Jugnu", symbol: nil, action: #selector(quit), key: "q"))
        return menu
    }

    private func item(_ title: String, symbol: String?, action: Selector, key: String = "") -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
        item.target = self
        if let symbol {
            item.image = NSImage(systemSymbolName: symbol, accessibilityDescription: nil)
        }
        return item
    }

    private var statusText: String {
        if appState.errorMessage != nil { return "Needs attention" }
        if !appState.approvals.isEmpty { return "Waiting for your OK" }
        switch appState.statusLabel {
        case "starting": return "Starting…"
        case "thinking": return "Working…"
        default: return appState.thinking ? "Working…" : "Ready"
        }
    }

    private var detailText: String? {
        if let error = appState.errorMessage { return String(error.prefix(60)) }
        if appState.thinking, let line = appState.progressLine, !line.isEmpty { return String(line.prefix(60)) }
        let model = appState.agentModelName.isEmpty ? appState.plannerModelName : appState.agentModelName
        return model.isEmpty ? nil : model
    }

    private var statusColor: Color {
        if appState.errorMessage != nil { return Theme.danger }
        if appState.thinking || !appState.approvals.isEmpty { return Theme.clay }
        return appState.statusLabel == "ready" ? Theme.success : Color.gray
    }

    // MARK: - Actions

    @objc private func toggleChat() { WindowManager.shared.toggleChat(below: statusItem?.button) }
    @objc private func newConversation() { appState.clear() }
    @objc private func stop() { appState.cancelRun() }
    @objc private func dailyReview() {
        WindowManager.shared.showChat(below: statusItem?.button)
        appState.requestDailyReview()
    }
    @objc private func showActivity() { WindowManager.shared.showActivity() }
    @objc private func openSettings() { WindowManager.shared.showSettings() }
    @objc private func toggleLaunchAtLogin() { LoginItemManager.toggle() }
    @objc private func quit() { NSApp.terminate(nil) }
}

/// The non-clickable first row of the menu: what Jugnu is doing right now.
private struct MenuHeader: View {
    let status: String
    let detail: String?
    let color: Color

    var body: some View {
        HStack(spacing: 9) {
            Circle().fill(color).frame(width: 7, height: 7)
            VStack(alignment: .leading, spacing: 1) {
                Text("Jugnu").font(.system(size: 13, weight: .semibold))
                    + Text("  \(status)").font(.system(size: 12)).foregroundColor(.secondary)
                if let detail {
                    Text(detail)
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.tail)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.leading, 14)
        .padding(.trailing, 12)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
    }
}
