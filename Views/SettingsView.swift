import SwiftUI

/// Jugnu's preferences window -- reachable via Cmd+, or the menu bar
/// icon's right-click menu (see MenuBarController). Closes the gap
/// AppState's own comments called out: every capability flag it already
/// tracked had nothing in the UI reading it. Every control here maps
/// directly to a real bridge.py environment variable -- see
/// SettingsStore.backendEnvironment for the exact mapping.
struct SettingsView: View {
    @ObservedObject private var settings = SettingsStore.shared
    @ObservedObject var appState: AppState

    /// Purely a disclosure flag for the API-mode per-role override
    /// fields -- not persisted, and deliberately not: reopening Settings
    /// to a collapsed "Chat model" field by default keeps the common
    /// case simple, without hiding that per-role control exists at all.
    @State private var showAPIRoleOverrides = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Spacing.xl) {
                header
                AutonomySettingsSection(appState: appState)
                modelSection
                awarenessSection
                spotifySection
                proactiveSection
                dailyReviewSection
                privacySection
                aboutSection
                restartFooter
            }
            .padding(Theme.Spacing.xl)
        }
        // 620x680, up from 480x600. Seven sections at the old size meant
        // most of the window was always off-screen; the Restart button that
        // makes any change take effect sat below the fold, unseen.
        .frame(width: 620, height: 680)
        .background(Theme.windowBackground)
        .foregroundStyle(Theme.ink)
    }

    // MARK: - Header

    // The window's own title bar already says "Jugnu Settings", so repeating
    // it here was the first thing in the content area telling you something
    // you'd just read. Replaced with the one line that IS worth leading on:
    // nothing takes effect until the backend restarts.
    private var header: some View {
        HStack(spacing: 10) {
            FireflyView(state: .idle, size: 26)
            Text("Changes apply when you restart the backend at the bottom of this window.")
                .font(Theme.text(12))
                .foregroundStyle(Theme.inkDim)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.bottom, 2)
    }

    // MARK: - Model
    // Four distinct roles, not one model for everything -- see
    // SettingsStore's own doc comment for the full explanation. Local and
    // API modes both surface the same three chat roles (Orchestration,
    // Agent, Search); embeddings are configured separately below since
    // they follow their own, independent provider choice.

    @ViewBuilder
    private var modelSection: some View {
        SettingsSection(
            title: "Model",
            description: "Jugnu runs three different jobs on three (optionally different) models -- see the role explanations below."
        ) {
            Picker("Provider", selection: $settings.provider) {
                ForEach(SettingsStore.Provider.allCases) { p in
                    Text(p.label).tag(p)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .tint(Theme.clay)

            if settings.provider == .local {
                localModelFields
            } else {
                apiModelFields
            }
        }

        SettingsSection(
            title: "Embeddings",
            description: "Turns text into vectors for memory recall -- kept separate because it's usually worth leaving on the local model even when chat goes through a hosted API: cheap, fast, and it never leaves your Mac."
        ) {
            Picker("Embed provider", selection: $settings.embedProvider) {
                ForEach(SettingsStore.Provider.allCases) { p in
                    Text(p.label).tag(p)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .tint(Theme.clay)

            if settings.embedProvider == .local {
                SettingsTextRow(label: "Embedding model", text: $settings.embedModel, placeholder: "nomic-embed-text")
            } else {
                SettingsTextRow(label: "Embedding model", text: $settings.apiEmbedModel, placeholder: "e.g. text-embedding-3-small")
            }
        }
    }

    private var localModelFields: some View {
        Group {
            SettingsTextRow(
                label: "Orchestration model (Planner)",
                text: $settings.plannerModel,
                placeholder: "qwen3.6:27b",
                helpText: "Decides the task plan and which tool to call at each step -- point this at the smartest model you've got."
            )
            SettingsTextRow(
                label: "Agent model",
                text: $settings.agentModel,
                placeholder: "qwen3.5:9b",
                helpText: "Executes one already-decided step. Can be smaller/cheaper than the orchestration model."
            )
            SettingsTextRow(
                label: "Search model",
                text: $settings.searchModel,
                placeholder: "leave blank to use the Agent model",
                helpText: "Rewrites and reasons over web search results specifically. Optional -- falls back to Agent."
            )
            SettingsTextRow(
                label: "Fast model (checks and routing)",
                text: $settings.fastModel,
                placeholder: "leave blank to use the Agent model",
                helpText: "Cheap judgement calls: verifying results, deciding if a reply answers a question, extracting memories."
            )
            SettingsTextRow(
                label: "Deep model (hard work)",
                text: $settings.deepModel,
                placeholder: "leave blank to use the Planner model",
                helpText: "Complex planning, writing study material and reports, analysis and code."
            )
            Text("Pulled via `ollama pull <model>` — these are Ollama model tags, not filenames.")
                .font(Theme.text(10.5))
                .foregroundStyle(Theme.inkFaint)
        }
    }

    private var apiModelFields: some View {
        Group {
            SettingsTextRow(label: "API base URL", text: $settings.apiBaseURL, placeholder: "https://api.openai.com/v1")
            SettingsSecureRow(label: "API key", text: $settings.apiKey)
            SettingsTextRow(
                label: "Chat model",
                text: $settings.apiChatModel,
                placeholder: "e.g. gemini-3.1-flash-lite",
                helpText: "The default every role below uses unless you override it specifically."
            )

            SettingsTextRow(
                label: "Fast model (checks and routing)",
                text: $settings.apiFastModel,
                placeholder: "same as Agent / Chat model",
                helpText: "Cheap judgement calls: verifying results, routing, extracting memories. Use your quickest model."
            )
            SettingsTextRow(
                label: "Deep model (hard work)",
                text: $settings.apiDeepModel,
                placeholder: "same as Planner / Chat model",
                helpText: "Complex planning, writing, analysis and code. Point this at your strongest model, e.g. a Pro model."
            )

            Toggle("Use a different model per role", isOn: $showAPIRoleOverrides.animation(Theme.Motion.snappy))
                .toggleStyle(.checkbox)
                .font(Theme.text(12))
                .foregroundStyle(Theme.inkDim)
                .tint(Theme.clay)

            if showAPIRoleOverrides {
                SettingsTextRow(
                    label: "Orchestration model (Planner)",
                    text: $settings.apiPlannerModelOverride,
                    placeholder: "same as Chat model above",
                    helpText: "Decides the task plan and which tool to call at each step."
                )
                SettingsTextRow(
                    label: "Agent model",
                    text: $settings.apiAgentModelOverride,
                    placeholder: "same as Chat model above",
                    helpText: "Executes one already-decided step."
                )
                SettingsTextRow(
                    label: "Search model",
                    text: $settings.apiSearchModelOverride,
                    placeholder: "same as Chat model above",
                    helpText: "Rewrites and reasons over web search results specifically."
                )
            }
        }
    }

    // MARK: - Spotify

    private var spotifySection: some View {
        SettingsSection(
            title: "Spotify",
            description: "Only needed to play a song by name. Spotify's own scripting has no search, and its window is Electron with no controls Jugnu can read — so a name can't be turned into something playable without this. Pause, skip, volume and \"what's playing\" all work without it."
        ) {
            SettingsTextRow(
                label: "Client ID",
                text: $settings.spotifyClientID,
                placeholder: "from developer.spotify.com/dashboard"
            )
            SettingsSecureRow(label: "Client Secret", text: $settings.spotifyClientSecret)
            Text("Create any app at developer.spotify.com/dashboard, then copy its Client ID and Secret here. Free, about two minutes, and no redirect URI is needed.")
                .font(Theme.text(10.5))
                .foregroundStyle(Theme.inkFaint)
                .lineSpacing(1.5)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    // MARK: - Awareness & memory

    private var awarenessSection: some View {
        SettingsSection(title: "Awareness & Memory", description: nil) {
            SettingsToggleRow(
                title: "Work quietly",
                description: "Runs commands silently and opens pages in background tabs, instead of taking over your screen with Terminal windows and foreground tabs. Ask Jugnu to \"show me\" or \"run it in Terminal\" any time you do want to watch. Opening an app, and anything that types or clicks for you, still has to come to the front.",
                isOn: $settings.quietModeEnabled
            )
            SettingsToggleRow(
                title: "Work in the Jugnu account",
                description: "Does desktop work -- apps, Terminal, files -- in the separate Jugnu macOS user instead of yours, so it never takes over your screen. Switch to that user to watch progress. Any request can still say \"on my account\" or \"on the Jugnu account\" to override this. The Jugnu user has to be logged in (it keeps running after you switch back).",
                isOn: $settings.useAgentAccountByDefault
            )
            SettingsToggleRow(
                title: "Situational awareness",
                description: "Lets Jugnu see which app and window are frontmost, so \"close this\" resolves to something real.",
                isOn: $settings.situationalAwarenessEnabled
            )
            SettingsToggleRow(
                title: "Activity tracking",
                description: "Logs app + window title + duration through the day to build the daily review. Off by default -- and the daily review has nothing to work with while it's off.",
                isOn: $settings.activityTrackingEnabled
            )
            SettingsToggleRow(
                title: "Include browser history in daily review",
                description: "Reads today's Safari history for the recap. Needs Full Disk Access, granted in System Settings > Privacy & Security.",
                isOn: $settings.includeBrowserHistoryInReview
            )
        }
    }

    // MARK: - Proactive

    private var proactiveSection: some View {
        SettingsSection(title: "Proactive Behavior", description: nil) {
            SettingsToggleRow(
                title: "Let Jugnu check in unprompted",
                description: "Notices when you switch apps and, rarely, says something worth saying -- never just a comment on the switch itself.",
                isOn: $settings.proactiveEnabled
            )
            SettingsToggleRow(
                title: "Allow autonomous actions",
                description: "On top of checking in, lets Jugnu actually do something on its own -- but only research, contacts, lookups, and saving facts. Anything touching files, mail, the calendar, or your system still asks first, every time.",
                isOn: $settings.proactiveActionsEnabled
            )
            .disabled(!settings.proactiveEnabled)
            .opacity(settings.proactiveEnabled ? 1 : 0.4)
        }
    }

    // MARK: - Daily review

    private var dailyReviewSection: some View {
        SettingsSection(title: "Daily Review", description: nil) {
            SettingsToggleRow(
                title: "Scheduled daily review",
                description: "Sends an end-of-day recap unprompted at the time below. You can always ask for one on demand from the menu bar regardless of this setting.",
                isOn: $settings.dailyReviewEnabled
            )
            DatePicker("Time", selection: $settings.dailyReviewTime, displayedComponents: .hourAndMinute)
                .foregroundStyle(Theme.ink)
                .disabled(!settings.dailyReviewEnabled)
                .opacity(settings.dailyReviewEnabled ? 1 : 0.4)
        }
    }

    // MARK: - Privacy

    private var privacySection: some View {
        SettingsSection(
            title: "Privacy",
            description: "Everything Jugnu remembers lives in one folder, in plain files you can open, back up, or delete yourself."
        ) {
            HStack {
                Text(SettingsStore.dataDirectoryDisplayPath)
                    .font(Theme.mono(10.5))
                    .foregroundStyle(Theme.inkDim)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Spacer()
                Button("Reveal in Finder") { SettingsStore.revealDataDirectory() }
                    .buttonStyle(.plain)
                    .foregroundStyle(Theme.clay)
                    .font(Theme.text(11.5, weight: .medium))
            }

            Divider().overlay(Theme.border)

            ResetRow(
                title: "Reset Facts…",
                description: "Everything Jugnu has saved about you -- name, preferences, standing instructions.",
                confirmTitle: "Reset all saved facts?",
                confirmMessage: "This clears everything Jugnu has saved about you via save_fact -- name, preferences, standing instructions. It can't be undone. Your conversation and activity history aren't affected.",
                action: { appState.resetFacts() }
            )

            ResetRow(
                title: "Reset Memory…",
                description: "Activity log, search history, and habit patterns -- what the daily review is built from.",
                confirmTitle: "Reset activity and habit memory?",
                confirmMessage: "This clears the activity log, search history, and recurring-pattern tracking semantic_memory.py has built up. It can't be undone. Facts and your conversation aren't affected.",
                action: { appState.resetMemory() }
            )

            Button("Clear This Conversation…") { appState.clear() }
                .buttonStyle(.plain)
                .foregroundStyle(Theme.danger)
                .font(Theme.text(12, weight: .medium))

            if let feedback = appState.settingsFeedback {
                Text(feedback)
                    .font(Theme.text(11.5, weight: .medium))
                    .foregroundStyle(Theme.clay)
                    .transition(.opacity)
            }
        }
        .animation(Theme.Motion.settle, value: appState.settingsFeedback)
    }

    // MARK: - About

    private var aboutSection: some View {
        SettingsSection(title: "About", description: nil) {
            VStack(alignment: .leading, spacing: 4) {
                aboutRow("Version", SettingsStore.appVersion)
                aboutRow("Planner", appState.plannerModelName.isEmpty ? "—" : appState.plannerModelName)
                aboutRow("Agent", appState.agentModelName.isEmpty ? "—" : appState.agentModelName)
                aboutRow("Tools available", appState.toolCount > 0 ? "\(appState.toolCount)" : "—")
            }
        }
    }

    private func aboutRow(_ label: String, _ value: String) -> some View {
        HStack {
            Text(label)
                .font(Theme.text(11.5))
                .foregroundStyle(Theme.inkDim)
            Spacer()
            Text(value)
                .font(Theme.mono(11))
                .foregroundStyle(Theme.ink)
        }
    }

    // MARK: - Restart footer

    private var restartFooter: some View {
        HStack {
            Spacer()
            Button(action: { appState.restartBackend() }) {
                Text("Restart Backend to Apply")
                    .font(Theme.text(13, weight: .semibold))
                    .padding(.horizontal, 14)
                    .padding(.vertical, 8)
            }
            .buttonStyle(.plain)
            .foregroundStyle(Theme.windowBackground)
            .background(Capsule().fill(Theme.clay))
            Spacer()
        }
        .padding(.top, Theme.Spacing.sm)
    }
}

// MARK: - Reusable pieces
// Same instinct as HistoryView's HistoryRow -- one small container/row
// type, reused per section, instead of re-typing the same VStack +
// padding + background boilerplate five times over.

private struct SettingsSection<Content: View>: View {
    let title: String
    let description: String?
    let content: Content

    init(title: String, description: String? = nil, @ViewBuilder content: () -> Content) {
        self.title = title
        self.description = description
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.sm) {
            Text(title.uppercased())
                .font(Theme.text(10.5, weight: .semibold))
                .tracking(0.8)
                .foregroundStyle(Theme.clay)
            if let description {
                // inkDim, not inkFaint. At 0.42 opacity these explanations
                // were dimmer than the disabled-control text beneath them.
                Text(description)
                    .font(Theme.text(11.5))
                    .foregroundStyle(Theme.inkDim)
                    .lineSpacing(2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            VStack(alignment: .leading, spacing: Theme.Spacing.sm) {
                content
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(Theme.Spacing.md)
            .background(RoundedRectangle(cornerRadius: Theme.radiusMedium).fill(Theme.panel))
        }
    }
}

private struct SettingsToggleRow: View {
    let title: String
    let description: String
    @Binding var isOn: Bool

    var body: some View {
        Toggle(isOn: $isOn) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(Theme.text(12.5, weight: .medium))
                    .foregroundStyle(Theme.ink)
                Text(description)
                    .font(Theme.text(11))
                    .foregroundStyle(Theme.inkDim)
                    .lineSpacing(2)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .toggleStyle(.switch)
        .tint(Theme.clay)
    }
}

private struct SettingsTextRow: View {
    let label: String
    @Binding var text: String
    var placeholder: String = ""
    var helpText: String? = nil

    @State private var focused = false

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label)
                .font(Theme.text(11, weight: .medium))
                .foregroundStyle(Theme.inkDim)

            // Values stay monospace on purpose -- an API base URL or a model
            // tag is checked character by character, which is exactly what
            // monospace is good for.
            //
            // The API base URL used to run straight off the right edge of
            // this field with nothing to indicate it: the visible text just
            // stopped mid-domain, so you couldn't tell a correct endpoint
            // from a truncated one. A horizontal ScrollView lets a long
            // value be read in full without forcing the window wider, and
            // the focus ring makes it obvious which field is live.
            ScrollView(.horizontal, showsIndicators: false) {
                TextField(placeholder, text: $text)
                    .textFieldStyle(.plain)
                    .font(Theme.mono(11.5))
                    .foregroundStyle(Theme.ink)
                    .frame(minWidth: 420, alignment: .leading)
            }
            .padding(7)
            .background(RoundedRectangle(cornerRadius: Theme.radiusSmall).fill(Theme.windowBackground))
            .overlay(
                RoundedRectangle(cornerRadius: Theme.radiusSmall)
                    .stroke(focused ? Theme.borderStrong : Theme.border, lineWidth: 1)
            )
            .onTapGesture { focused = true }

            if let helpText {
                Text(helpText)
                    .font(Theme.text(10.5))
                    .foregroundStyle(Theme.inkFaint)
                    .lineSpacing(1.5)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

/// A destructive action behind a confirmation, same shape as HistoryView's
/// "Clear" -> confirmationDialog pattern -- used for Reset Facts / Reset
/// Memory, both irreversible and both worth a real "are you sure" rather
/// than a single accidental click.
private struct ResetRow: View {
    let title: String
    let description: String
    let confirmTitle: String
    let confirmMessage: String
    let action: () -> Void

    @State private var confirming = false

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Button(title) { confirming = true }
                .buttonStyle(.plain)
                .foregroundStyle(Theme.danger)
                .font(Theme.text(12, weight: .medium))
            Text(description)
                .font(Theme.text(11))
                .foregroundStyle(Theme.inkDim)
                .lineSpacing(2)
                .fixedSize(horizontal: false, vertical: true)
        }
        .confirmationDialog(confirmTitle, isPresented: $confirming, titleVisibility: .visible) {
            Button(title.replacingOccurrences(of: "…", with: ""), role: .destructive) { action() }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text(confirmMessage)
        }
    }
}

private struct SettingsSecureRow: View {
    let label: String
    @Binding var text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(label)
                .font(Theme.text(11, weight: .medium))
                .foregroundStyle(Theme.inkDim)
            SecureField("", text: $text)
                .textFieldStyle(.plain)
                .font(Theme.mono(11.5))
                .foregroundStyle(Theme.ink)
                .padding(7)
                .background(RoundedRectangle(cornerRadius: Theme.radiusSmall).fill(Theme.windowBackground))
                .overlay(
                    RoundedRectangle(cornerRadius: Theme.radiusSmall)
                        .stroke(Theme.border, lineWidth: 1)
                )
            Text(text.isEmpty
                 ? "Not set. Stored in your Keychain, never in preferences or in Jugnu's own environment."
                 : "Stored in your Keychain.")
                .font(Theme.text(10.5))
                .foregroundStyle(Theme.inkFaint)
        }
    }
}

#Preview {
    SettingsView(appState: AppState())
}

/// What JugnuApp's `Settings { }` scene actually points at -- AppState
/// lives as ContentView's @StateObject, reachable elsewhere only via the
/// weak AppState.shared it sets in its own init(). That's set almost
/// immediately after launch (ContentView renders as part of the app's one
/// WindowGroup), so in practice this only shows the fallback for an
/// instant, if ever -- but Cmd+, is real, user-triggerable input, so it
/// gets a real fallback rather than force-unwrapping and risking a crash
/// on a launch-timing fluke.
struct SettingsRootView: View {
    var body: some View {
        if let appState = AppState.shared {
            SettingsView(appState: appState)
        } else {
            VStack(spacing: 8) {
                FireflyView(state: .idle, size: 28)
                Text("Jugnu is still starting up…")
                    .font(Theme.mono(11.5))
                    .foregroundStyle(Theme.inkFaint)
            }
            .frame(width: 480, height: 200)
            .background(Theme.windowBackground)
        }
    }
}
