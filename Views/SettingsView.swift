import SwiftUI

/// Jugnu's Settings window: five short panes. Autonomy applies instantly;
/// everything the backend reads at startup shows a "Restart to apply" bar
/// only when something has actually changed.
struct SettingsView: View {
    @ObservedObject var appState: AppState
    @ObservedObject private var settings = SettingsStore.shared
    @State private var pane: Pane

    init(appState: AppState, initialPane: Pane = .general) {
        self.appState = appState
        self._pane = State(initialValue: initialPane)
    }

    enum Pane: String, CaseIterable, Identifiable {
        case general = "General", autonomy = "Autonomy", models = "Models", privacy = "Privacy", about = "About"
        var id: String { rawValue }
        var symbol: String {
            switch self {
            case .general: return "gearshape"
            case .autonomy: return "hand.raised"
            case .models: return "cpu"
            case .privacy: return "lock"
            case .about: return "info.circle"
            }
        }
    }

    private var needsRestart: Bool {
        !appState.launchedEnvironment.isEmpty && settings.backendEnvironment != appState.launchedEnvironment
    }

    var body: some View {
        VStack(spacing: 0) {
            tabBar
                .zIndex(1)   // keeps scrolled content from taking the tabs' clicks
            Rectangle().fill(Theme.border).frame(height: 1)
            ScrollView {
                Group {
                    switch pane {
                    case .general: GeneralPane(settings: settings)
                    case .autonomy: AutonomyPane(appState: appState)
                    case .models: ModelsPane(settings: settings, appState: appState)
                    case .privacy: PrivacyPane(appState: appState)
                    case .about: AboutPane(appState: appState)
                    }
                }
                .padding(.horizontal, 22)
                .padding(.vertical, 18)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            if needsRestart { restartBar }
        }
        .frame(width: Theme.WindowSize.settings.width, height: Theme.WindowSize.settings.height)
        .background(Theme.windowBackground)
        .foregroundStyle(Theme.ink)
        .tint(Theme.clay)
        .preferredColorScheme(.dark)
        .ignoresSafeArea()
    }

    private var tabBar: some View {
        HStack(spacing: 2) {
            ForEach(Pane.allCases) { p in
                Button { pane = p } label: {
                    VStack(spacing: 3) {
                        Image(systemName: p.symbol).font(.system(size: 15))
                            .frame(height: 18)
                        Text(p.rawValue).font(Theme.text(10.5, weight: .medium))
                    }
                    .foregroundStyle(pane == p ? Theme.clay : Theme.inkDim)
                    .frame(width: 70, height: 44)
                    .background(RoundedRectangle(cornerRadius: 8).fill(pane == p ? Color.white.opacity(0.07) : .clear))
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 30)
        .padding(.bottom, 8)
    }

    private var restartBar: some View {
        HStack(spacing: 10) {
            Image(systemName: "arrow.clockwise.circle.fill").foregroundStyle(Theme.clay)
            Text("Restart Jugnu's engine to apply your changes.")
                .font(Theme.text(12))
                .foregroundStyle(Theme.inkDim)
            Spacer()
            Button("Restart") { appState.restartBackend() }
                .buttonStyle(JugnuButtonStyle(kind: .primary, compact: true))
                .disabled(appState.thinking)
                .help(appState.thinking ? "Wait for the current task to finish" : "")
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 10)
        .background(Theme.panel)
        .overlay(alignment: .top) { Rectangle().fill(Theme.border).frame(height: 1) }
    }
}

// MARK: - General

private struct GeneralPane: View {
    @ObservedObject var settings: SettingsStore
    @ObservedObject private var hotKeys = HotKeys.shared
    @State private var launchAtLogin = LoginItemManager.isEnabled

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            SettingsGroup("App") {
                ToggleRow("Launch at login", isOn: Binding(get: { launchAtLogin }, set: { _ in
                    LoginItemManager.toggle()
                    launchAtLogin = LoginItemManager.isEnabled
                }))
                RowDivider()
                ToggleRow("Show in Dock while open", detail: "Gives Jugnu its own menu bar when it's in front.",
                          isOn: $settings.showInDockWhenOpen)
                RowDivider()
                ToggleRow("Minimize to firefly", detail: "Shrinks into a firefly in the corner instead of the Dock.",
                          isOn: $settings.minimizeToFirefly)
            }
            SettingsGroup("Shortcuts") {
                HStack {
                    RowLabel("Open Jugnu", detail: "From any app. Anything you've selected there comes along.")
                    Spacer()
                    Picker("", selection: $settings.openHotkey) {
                        ForEach(SettingsStore.HotkeyPreset.allCases) { Text($0.label).tag($0) }
                    }
                    .labelsHidden().fixedSize()
                }
                .padding(.vertical, 2)
                if let problem = hotKeys.problem {
                    Label(problem, systemImage: "exclamationmark.triangle.fill")
                        .font(Theme.text(11))
                        .foregroundStyle(Theme.clay)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.bottom, 4)
                }
                RowDivider()
                ToggleRow("Hold ⌥ to talk", detail: "While Jugnu is in front: hold Option, speak, let go to send.",
                          isOn: $settings.holdOptionToTalk)
            }
            SettingsGroup("Working") {
                ToggleRow("Work quietly", detail: "Silent commands and background tabs instead of taking over the screen.",
                          isOn: $settings.quietModeEnabled)
                RowDivider()
                ToggleRow("Use the Jugnu account", detail: "The default for new requests. Pick per request next to the message box.",
                          isOn: $settings.useAgentAccountByDefault)
                RowDivider()
                ToggleRow("Situational awareness", detail: "Know the frontmost app and window, so “this” means something.",
                          isOn: $settings.situationalAwarenessEnabled)
            }
            SettingsGroup("Proactive") {
                ToggleRow("Check in on its own", detail: "Speaks up now and then when it notices something useful.",
                          isOn: $settings.proactiveEnabled)
                RowDivider()
                ToggleRow("Act on its own", detail: "Research, lookups and notes only. Everything else asks.",
                          isOn: $settings.proactiveActionsEnabled)
                    .disabled(!settings.proactiveEnabled)
                    .opacity(settings.proactiveEnabled ? 1 : 0.45)
            }
            SettingsGroup("Daily review") {
                HStack {
                    RowLabel("Send a daily review", detail: nil)
                    Spacer()
                    if settings.dailyReviewEnabled {
                        DatePicker("", selection: $settings.dailyReviewTime, displayedComponents: .hourAndMinute)
                            .labelsHidden()
                            .datePickerStyle(.field)
                            .fixedSize()
                    }
                    Toggle("", isOn: $settings.dailyReviewEnabled).labelsHidden().toggleStyle(.switch).controlSize(.small)
                }
                .padding(.vertical, 2)
                RowDivider()
                ToggleRow("Track app usage", detail: "What the review is built from. Stays on this Mac.",
                          isOn: $settings.activityTrackingEnabled)
                RowDivider()
                ToggleRow("Include Safari history", detail: "Needs Full Disk Access.",
                          isOn: $settings.includeBrowserHistoryInReview)
            }
        }
    }
}

// MARK: - Models

private struct ModelsPane: View {
    @ObservedObject var settings: SettingsStore
    @ObservedObject var appState: AppState
    @State private var perRole = false
    @State private var moreLocal = false
    @State private var showSpotify = false

    private var mode: Binding<SettingsStore.ModelMode> {
        Binding(get: { settings.modelMode }, set: { appState.setModelMode($0) })
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            SettingsGroup("Mode") {
                Picker("", selection: mode) {
                    ForEach(SettingsStore.ModelMode.allCases) { Text($0.label).tag($0) }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .padding(.top, 4)
                Text(settings.modelMode.help)
                    .font(Theme.text(11.5))
                    .foregroundStyle(Theme.inkDim)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.top, 4)
                if settings.modelMode != .api {
                    Label(localStatus, systemImage: appState.localRunning ? "checkmark.circle.fill" : "exclamationmark.circle")
                        .font(Theme.text(11))
                        .foregroundStyle(appState.localRunning ? Theme.success : Theme.clay)
                        .padding(.vertical, 5)
                } else {
                    Spacer().frame(height: 4)
                }
            }
            Footnote("Switches instantly. Also in the ··· menu in Jugnu's window.")
                .padding(.top, -12)

            SettingsGroup("Hosted API") {
                FieldRow("Base URL", text: $settings.apiBaseURL, placeholder: "https://api.openai.com/v1")
                RowDivider()
                FieldRow("API key", text: $settings.apiKey, placeholder: "Stored in Keychain", secure: true)
                RowDivider()
                FieldRow("Model", text: $settings.apiChatModel, placeholder: "gemini-3.1-flash-lite")
                RowDivider()
                FieldRow("Fast model", text: $settings.apiFastModel, placeholder: "Same as Model")
                RowDivider()
                FieldRow("Deep model", text: $settings.apiDeepModel, placeholder: "Same as Model")
                RowDivider()
                DisclosureRow("Per-role models", isOn: $perRole)
                if perRole {
                    RowDivider()
                    FieldRow("Planner", text: $settings.apiPlannerModelOverride, placeholder: "Same as Model")
                    RowDivider()
                    FieldRow("Agent", text: $settings.apiAgentModelOverride, placeholder: "Same as Model")
                    RowDivider()
                    FieldRow("Search", text: $settings.apiSearchModelOverride, placeholder: "Same as Model")
                }
            }

            SettingsGroup("On this Mac (Ollama)") {
                LocalModelRow("Easy work", text: $settings.easyModel, placeholder: "qwen3.5:9b",
                              installed: appState.localModels)
                RowDivider()
                DisclosureRow("Models for Local mode", isOn: $moreLocal)
                if moreLocal {
                    RowDivider()
                    LocalModelRow("Planner", text: $settings.plannerModel, placeholder: "qwen3.6:27b",
                                  installed: appState.localModels)
                    RowDivider()
                    LocalModelRow("Agent", text: $settings.agentModel, placeholder: "qwen3.5:9b",
                                  installed: appState.localModels)
                    RowDivider()
                    LocalModelRow("Fast", text: $settings.fastModel, placeholder: "Same as Agent",
                                  installed: appState.localModels)
                    RowDivider()
                    LocalModelRow("Deep", text: $settings.deepModel, placeholder: "Same as Planner",
                                  installed: appState.localModels)
                }
            }
            Footnote("Model changes here apply after a restart; the mode above applies instantly.")
                .padding(.top, -12)

            SettingsGroup("Memory embeddings") {
                HStack {
                    RowLabel("Provider", detail: nil)
                    Spacer()
                    Picker("", selection: $settings.embedProvider) {
                        ForEach(SettingsStore.Provider.allCases) { Text($0.label).tag($0) }
                    }
                    .pickerStyle(.segmented).labelsHidden().fixedSize()
                }
                .padding(.vertical, 2)
                RowDivider()
                if settings.embedProvider == .local {
                    FieldRow("Model", text: $settings.embedModel, placeholder: "nomic-embed-text")
                } else {
                    FieldRow("Model", text: $settings.apiEmbedModel, placeholder: "text-embedding-3-small")
                }
            }

            SettingsGroup("Spotify") {
                DisclosureRow("Web API keys (optional)", isOn: $showSpotify)
                if showSpotify {
                    RowDivider()
                    FieldRow("Client ID", text: $settings.spotifyClientID, placeholder: "developer.spotify.com")
                    RowDivider()
                    FieldRow("Secret", text: $settings.spotifyClientSecret, placeholder: "Stored in Keychain", secure: true)
                }
            }
            Footnote("Not needed to play music. Only used as an extra way to look songs up.")
                .padding(.top, -12)
        }
    }

    private var localStatus: String {
        guard appState.localRunning else {
            return settings.modelMode == .hybrid ? "Ollama isn't running, so easy work uses the API until it is."
                : "Ollama isn't running. Start it, or switch to API."
        }
        let wanted = settings.easyModel
        if !appState.localModels.isEmpty && !appState.localModels.contains(wanted)
            && !appState.localModels.contains(wanted + ":latest") {
            return "Ollama is running, but \(wanted) isn't installed (ollama pull \(wanted))."
        }
        return "Ollama is running · \(wanted)"
    }
}

/// A model name field with a menu of the models Ollama has installed.
private struct LocalModelRow: View {
    let label: String
    @Binding var text: String
    var placeholder = ""
    let installed: [String]

    init(_ label: String, text: Binding<String>, placeholder: String, installed: [String]) {
        self.label = label
        self._text = text
        self.placeholder = placeholder
        self.installed = installed
    }

    var body: some View {
        HStack(spacing: 6) {
            FieldRow(label, text: $text, placeholder: placeholder)
            if !installed.isEmpty {
                Menu {
                    ForEach(installed.filter { !$0.contains("embed") }, id: \.self) { name in
                        Button(name) { text = name }
                    }
                } label: {
                    Image(systemName: "chevron.up.chevron.down").font(.system(size: 10))
                        .foregroundStyle(Theme.inkDim)
                        .frame(width: 20, height: 22)
                }
                .menuStyle(.borderlessButton)
                .menuIndicator(.hidden)
                .fixedSize()
                .help("Installed models")
            }
        }
    }
}

// MARK: - Privacy

private struct PrivacyPane: View {
    @ObservedObject var appState: AppState

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            SettingsGroup("Your data") {
                HStack(spacing: 10) {
                    RowLabel("Stored in", detail: nil)
                    Text(SettingsStore.dataDirectoryDisplayPath)
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.inkDim)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Spacer()
                    Button("Reveal") { SettingsStore.revealDataDirectory() }
                        .buttonStyle(JugnuButtonStyle(kind: .secondary, compact: true))
                }
                .padding(.vertical, 2)
            }
            Footnote("Everything Jugnu remembers lives in this folder, in plain files. Secrets are never stored there.")
                .padding(.top, -12)

            SettingsGroup("Reset") {
                ResetRow(title: "Saved facts", detail: "Your name, preferences and standing instructions.",
                         confirm: "Reset all saved facts?") { appState.resetFacts() }
                RowDivider()
                ResetRow(title: "Activity memory", detail: "App usage, searches and habits behind the daily review.",
                         confirm: "Reset activity memory?") { appState.resetMemory() }
                RowDivider()
                ResetRow(title: "Conversation", detail: "The current chat.",
                         confirm: "Clear this conversation?") { appState.clear() }
            }
            if let feedback = appState.settingsFeedback {
                Label(feedback, systemImage: "checkmark.circle.fill")
                    .font(Theme.text(12))
                    .foregroundStyle(Theme.success)
            }
        }
    }
}

private struct ResetRow: View {
    let title: String
    let detail: String
    let confirm: String
    let action: () -> Void
    @State private var confirming = false

    var body: some View {
        HStack {
            RowLabel(title, detail: detail)
            Spacer()
            Button("Reset…") { confirming = true }
                .buttonStyle(JugnuButtonStyle(kind: .destructive, compact: true))
        }
        .padding(.vertical, 2)
        .confirmationDialog(confirm, isPresented: $confirming) {
            Button("Reset", role: .destructive, action: action)
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This can't be undone.")
        }
    }
}

// MARK: - About

private struct AboutPane: View {
    @ObservedObject var appState: AppState

    var body: some View {
        VStack(spacing: 20) {
            VStack(spacing: 8) {
                FireflyView(state: .idle, size: 52)
                Text("Jugnu").font(Theme.text(17, weight: .semibold))
                Text("Version \(SettingsStore.appVersion)")
                    .font(Theme.text(11.5))
                    .foregroundStyle(Theme.inkFaint)
            }
            .frame(maxWidth: .infinity)
            .padding(.top, 8)

            SettingsGroup(nil) {
                InfoRow("Planner", appState.plannerModelName)
                RowDivider()
                InfoRow("Agent", appState.agentModelName)
                RowDivider()
                InfoRow("Tools", appState.toolCount > 0 ? "\(appState.toolCount)" : "")
                RowDivider()
                InfoRow("Engine", appState.statusLabel == "error" ? "Stopped" : (appState.statusLabel == "starting" ? "Starting…" : "Running"))
            }
            Button("Restart Engine") { appState.restartBackend() }
                .buttonStyle(JugnuButtonStyle(kind: .secondary, compact: true))
                .disabled(appState.thinking)
        }
    }
}

private struct InfoRow: View {
    let label: String
    let value: String
    init(_ label: String, _ value: String) { self.label = label; self.value = value }

    var body: some View {
        HStack {
            Text(label).font(Theme.text(12.5)).foregroundStyle(Theme.inkDim)
            Spacer()
            Text(value.isEmpty ? "—" : value).font(Theme.mono(11.5)).foregroundStyle(Theme.ink).textSelection(.enabled)
        }
        .padding(.vertical, 3)
    }
}

// MARK: - Building blocks (shared with AutonomyPane)

struct SettingsGroup<Content: View>: View {
    let title: String?
    let content: Content

    init(_ title: String?, @ViewBuilder content: () -> Content) {
        self.title = title
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            if let title {
                Text(title)
                    .font(Theme.text(11.5, weight: .semibold))
                    .foregroundStyle(Theme.inkFaint)
                    .padding(.leading, 2)
            }
            VStack(alignment: .leading, spacing: 0) { content }
                .padding(.horizontal, 12)
                .padding(.vertical, 6)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: Theme.radiusMedium, style: .continuous).fill(Theme.panel))
                .overlay(RoundedRectangle(cornerRadius: Theme.radiusMedium, style: .continuous).stroke(Theme.border, lineWidth: 1))
        }
    }
}

struct RowDivider: View {
    var body: some View {
        Rectangle().fill(Theme.border).frame(height: 1).padding(.vertical, 6)
    }
}

struct RowLabel: View {
    let title: String
    let detail: String?
    init(_ title: String, detail: String?) { self.title = title; self.detail = detail }

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title).font(Theme.text(12.5)).foregroundStyle(Theme.ink)
            if let detail {
                Text(detail)
                    .font(Theme.text(11))
                    .foregroundStyle(Theme.inkFaint)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

struct ToggleRow: View {
    let title: String
    var detail: String? = nil
    @Binding var isOn: Bool

    init(_ title: String, detail: String? = nil, isOn: Binding<Bool>) {
        self.title = title
        self.detail = detail
        self._isOn = isOn
    }

    var body: some View {
        HStack(spacing: 12) {
            RowLabel(title, detail: detail)
            Spacer(minLength: 8)
            Toggle("", isOn: $isOn).labelsHidden().toggleStyle(.switch).controlSize(.small)
        }
        .padding(.vertical, 2)
    }
}

struct FieldRow: View {
    let label: String
    @Binding var text: String
    var placeholder = ""
    var secure = false

    init(_ label: String, text: Binding<String>, placeholder: String = "", secure: Bool = false) {
        self.label = label
        self._text = text
        self.placeholder = placeholder
        self.secure = secure
    }

    var body: some View {
        HStack(spacing: 12) {
            Text(label).font(Theme.text(12.5)).foregroundStyle(Theme.ink)
                .frame(width: 92, alignment: .leading)
            Group {
                if secure {
                    SecureField(placeholder, text: $text)
                } else {
                    TextField(placeholder, text: $text)
                }
            }
            .textFieldStyle(.plain)
            .font(Theme.mono(11.5))
            .padding(.horizontal, 8)
            .padding(.vertical, 5)
            .background(RoundedRectangle(cornerRadius: 6).fill(Theme.well))
            .overlay(RoundedRectangle(cornerRadius: 6).stroke(Theme.border, lineWidth: 1))
        }
        .padding(.vertical, 1)
    }
}

struct DisclosureRow: View {
    let title: String
    @Binding var isOn: Bool
    init(_ title: String, isOn: Binding<Bool>) { self.title = title; self._isOn = isOn }

    var body: some View {
        Button { isOn.toggle() } label: {
            HStack {
                Text(title).font(Theme.text(12.5)).foregroundStyle(Theme.ink)
                Spacer()
                Image(systemName: "chevron.right")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(Theme.inkFaint)
                    .rotationEffect(.degrees(isOn ? 90 : 0))
            }
            .padding(.vertical, 3)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}

struct Footnote: View {
    let text: String
    init(_ text: String) { self.text = text }

    var body: some View {
        Text((try? AttributedString(markdown: text)) ?? AttributedString(text))
            .font(Theme.text(11))
            .foregroundStyle(Theme.inkFaint)
            .fixedSize(horizontal: false, vertical: true)
            .padding(.leading, 2)
    }
}

#Preview {
    SettingsView(appState: AppState())
}
