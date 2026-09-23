import SwiftUI
import AppKit

/// The chat panel: header, conversation, approvals, input. Activity opens
/// over it. The window itself is created and owned by WindowManager.
struct ContentView: View {
    @ObservedObject var appState: AppState
    @State private var showActivity = false

    var body: some View {
        VStack(spacing: 0) {
            // Above the conversation for hit-testing: a scrolled ScrollView's
            // content extends up behind the header and would otherwise take
            // its clicks.
            HeaderView(appState: appState, showActivity: $showActivity)
                .zIndex(1)
            ConversationView(appState: appState)
            ApprovalStackView(appState: appState)
            InputBarView(appState: appState)
        }
        .overlay {
            if showActivity {
                ActivityView(appState: appState, isPresented: $showActivity)
                    .transition(.opacity)
            }
        }
        .animation(Theme.Motion.settle, value: showActivity)
        .background(WindowBackdrop())
        .ignoresSafeArea()
        .frame(minWidth: Theme.WindowSize.min.width, minHeight: Theme.WindowSize.min.height)
        .preferredColorScheme(.dark)
        .onReceive(NotificationCenter.default.publisher(for: .jugnuToggleActivity)) { _ in showActivity.toggle() }
        .onReceive(NotificationCenter.default.publisher(for: .jugnuShowActivity)) { _ in showActivity = true }
    }
}

/// Frosted, warm, still. Vibrancy underneath a near-opaque tint, with one
/// faint static glow at the top so the surface isn't flat.
struct WindowBackdrop: View {
    var body: some View {
        ZStack {
            VisualEffectBackground(material: .hudWindow, blendingMode: .behindWindow)
            Theme.windowBackground.opacity(0.9)
            RadialGradient(colors: [Theme.clay.opacity(0.07), .clear],
                           center: UnitPoint(x: 0.5, y: 0), startRadius: 0, endRadius: 320)
        }
        .ignoresSafeArea()
    }
}

// MARK: - Header

/// One slim row that shares the title bar with the close button.
private struct HeaderView: View {
    @ObservedObject var appState: AppState
    @Binding var showActivity: Bool

    static func menuLabel(_ mode: SettingsStore.ModelMode) -> String {
        switch mode {
        case .api: return "API"
        case .hybrid: return "Hybrid — local for easy work"
        case .local: return "Local only"
        case .claude: return "Claude — your Claude plan"
        }
    }

    private var state: FireflyState {
        if appState.errorMessage != nil { return .error }
        return appState.thinking ? .thinking : .idle
    }

    private var status: String? {
        switch appState.statusLabel {
        case "ready": return nil
        case "starting": return "Starting…"
        case "error": return "Needs attention"
        case "thinking": return "Working…"
        default: return appState.thinking ? "Working…" : nil
        }
    }

    var body: some View {
        HStack(spacing: 7) {
            FireflyView(state: state, size: 16)
            Text("Jugnu")
                .font(Theme.text(12.5, weight: .semibold))
                .foregroundStyle(Theme.ink)
            if let mode = SettingsStore.ModelMode(rawValue: appState.modelMode), mode != .api {
                Text(mode.label)
                    .font(Theme.text(10, weight: .medium))
                    .foregroundStyle(Theme.inkDim)
                    .padding(.horizontal, 5).padding(.vertical, 1)
                    .background(Capsule().fill(Color.white.opacity(0.07)))
                    .help(mode.help)
            }
            if let status {
                Text(status)
                    .font(Theme.text(11.5))
                    .foregroundStyle(appState.errorMessage != nil ? Theme.danger.opacity(0.9) : Theme.inkFaint)
                    .lineLimit(1)
                    .transition(.opacity)
            }
            Spacer(minLength: 8)

            IconButton(symbol: "square.and.pencil", size: 12.5, help: "New conversation (⌘N)") {
                appState.clear()
            }
            .disabled(appState.thinking || appState.messages.isEmpty)
            .opacity(appState.thinking || appState.messages.isEmpty ? 0.4 : 1)

            IconButton(symbol: "list.bullet.rectangle", size: 12.5, help: "Activity (⌘J)") {
                showActivity.toggle()
            }
            .overlay(alignment: .topTrailing) {
                if appState.activityNeedsAttention || !appState.backgroundRuns.isEmpty {
                    Circle()
                        .fill(appState.activityNeedsAttention ? Theme.danger : Theme.clay)
                        .frame(width: 6, height: 6)
                        .offset(x: -3, y: 3)
                        .allowsHitTesting(false)
                }
            }

            Menu {
                Section("Windows") {
                    Button("Jugnu's Desk") { WindowManager.shared.showDesk() }
                        .keyboardShortcut("d", modifiers: [.command, .shift])
                    Button("Trading Terminal") { WindowManager.shared.openTradingTerminal() }
                        .keyboardShortcut("t", modifiers: [.command, .shift])
                    Button("Activity") { showActivity.toggle() }
                        .keyboardShortcut("j", modifiers: .command)
                }
                Section("This conversation") {
                    Button("New conversation") { appState.clear() }
                        .keyboardShortcut("n", modifiers: .command)
                        .disabled(appState.thinking || appState.messages.isEmpty)
                    Button("Daily Review") { appState.requestDailyReview() }
                        .disabled(appState.thinking)
                }
                Section("Where Jugnu thinks") {
                    Picker("Model", selection: Binding(
                        get: { SettingsStore.ModelMode(rawValue: appState.modelMode) ?? .api },
                        set: { appState.setModelMode($0) })) {
                        ForEach(SettingsStore.ModelMode.allCases) { mode in
                            Text(Self.menuLabel(mode)).tag(mode)
                        }
                    }
                    .pickerStyle(.inline)
                }
                Divider()
                Button("Settings…") { WindowManager.shared.showSettings() }
                    .keyboardShortcut(",", modifiers: .command)
                // Jugnu's engine is a Python process started at launch: after
                // an update it keeps running the code it loaded, which looks
                // exactly like a fix that didn't work.
                Button("Restart engine") { appState.restartBackend() }
                    .disabled(appState.thinking)
                Button("Hide Jugnu") { WindowManager.shared.hideChat() }
            } label: {
                Image(systemName: "ellipsis")
                    .font(.system(size: 12.5))
                    .foregroundStyle(Theme.inkDim)
                    .frame(width: 26, height: 24)
                    .contentShape(Rectangle())
            }
            .menuStyle(.borderlessButton)
            .menuIndicator(.hidden)
            .fixedSize()
            .help("More")
        }
        .animation(Theme.Motion.snappy, value: status)
        // Clears the window buttons at the left of the title bar.
        .padding(.leading, 78)
        .padding(.trailing, 8)
        .frame(height: 30)
        .padding(.top, 1)
    }
}
