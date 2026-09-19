
import AppKit
import Darwin
import Foundation
import Combine

/// Single source of truth for every user-configurable preference --
/// backed by UserDefaults for plain settings, Keychain for the API key
/// (see KeychainStore). A plain ObservableObject class, not
/// per-View @AppStorage, on purpose: this needs to be readable from
/// AppKit code that isn't a SwiftUI View at all (MenuBarController,
/// AppState) and, most importantly, needs to become the environment
/// variables the Python backend actually reads (see backendEnvironment
/// below) when bridge.py launches.
///
/// Every key here was pulled directly from the real `os.environ.get(...)`
/// calls in bridge.py / llm_client.py / memory.py / semantic_memory.py /
/// awareness.py / daily_review.py / proactive.py, and cross-checked
/// against BackendBridge.swift's own `backendEnvironment(workingDirectory:)`
/// -- worth re-checking against those files if this ever drifts, since a
/// typo'd env var name here would silently do nothing rather than error.
///
/// One thing that cross-check changed: BackendBridge.swift already
/// defaults to `provider = .api` (Gemini, via Google's OpenAI-compatible
/// endpoint) when nothing else is configured, not `.local`. Every default
/// below matches that -- and every other one of BackendBridge's own
/// built-in fallbacks -- on purpose. This class always writes an
/// explicit value for everything it manages (see applyToProcessEnvironment),
/// which would otherwise silently override BackendBridge's sensible
/// defaults with `.local` from the very first launch, before anyone had
/// ever opened Settings, and Ollama might not even be installed yet.
///
/// Deliberately does NOT expose every knob those files support --
/// JUGNU_DATA_DIR, poll/cooldown intervals, and retry counts all stay
/// advanced/env-var-only. Surfacing all thirty-odd variables here
/// wouldn't make Jugnu more configurable in any way that matters, just
/// harder to actually use.
@MainActor
final class SettingsStore: ObservableObject {
    static let shared = SettingsStore()

    enum Provider: String, CaseIterable, Identifiable, Hashable {
        case local, api
        var id: String { rawValue }
        var label: String { self == .local ? "Local (Ollama)" : "Hosted API" }
    }

    // MARK: - Provider (chat) + per-role model overrides
    // llm_client.py runs four distinct *roles*, not one model for
    // everything -- PLANNER (decides the task plan and which tool to
    // call each step, wants the smartest model you've got), AGENT
    // (executes one already-decided step, can be smaller/cheaper),
    // SEARCH (rewrites and reasons over web results, falls back to the
    // Agent model if not set), and EMBED (turns text into vectors for
    // memory recall -- configured independently below, see
    // embedProvider, since it defaults to staying local even when chat
    // goes through a hosted API).

    @Published var provider: Provider { didSet { defaults.set(provider.rawValue, forKey: Keys.provider) } }

    // Local mode
    @Published var plannerModel: String { didSet { defaults.set(plannerModel, forKey: Keys.plannerModel) } }
    @Published var agentModel: String { didSet { defaults.set(agentModel, forKey: Keys.agentModel) } }
    /// Optional -- empty means "use agentModel," matching JUGNU_SEARCH_MODEL's
    /// own fallback to JUGNU_MODEL in llm_client.py.
    @Published var searchModel: String { didSet { defaults.set(searchModel, forKey: Keys.searchModel) } }

    // API mode
    @Published var apiBaseURL: String { didSet { defaults.set(apiBaseURL, forKey: Keys.apiBaseURL) } }
    /// The baseline every role in API mode falls back to unless
    /// overridden below.
    @Published var apiChatModel: String { didSet { defaults.set(apiChatModel, forKey: Keys.apiChatModel) } }
    /// Optional per-role overrides -- each empty by default, meaning
    /// "use apiChatModel," exactly matching JUGNU_{ROLE}_API_MODEL's own
    /// fallback in llm_client.py's _api_model_for().
    @Published var apiPlannerModelOverride: String { didSet { defaults.set(apiPlannerModelOverride, forKey: Keys.apiPlannerModelOverride) } }
    @Published var apiAgentModelOverride: String { didSet { defaults.set(apiAgentModelOverride, forKey: Keys.apiAgentModelOverride) } }
    @Published var apiSearchModelOverride: String { didSet { defaults.set(apiSearchModelOverride, forKey: Keys.apiSearchModelOverride) } }
    @Published var apiKey: String { didSet { KeychainStore.set(apiKey, for: Keys.apiKey) } }

    // MARK: - Embeddings (independent of the chat provider above)

    @Published var embedProvider: Provider { didSet { defaults.set(embedProvider.rawValue, forKey: Keys.embedProvider) } }
    @Published var embedModel: String { didSet { defaults.set(embedModel, forKey: Keys.embedModel) } }
    @Published var apiEmbedModel: String { didSet { defaults.set(apiEmbedModel, forKey: Keys.apiEmbedModel) } }

    // MARK: - Spotify
    // Needed only to turn a song NAME into something playable. Spotify's
    // own AppleScript has no search command, and its desktop UI is Electron
    // with no named accessibility controls, so there is no local way to do
    // it. Playback control (pause, skip, volume, what's playing) needs none
    // of this.
    //
    // Client Credentials flow: an app-level token, no user login, no
    // redirect. The secret goes to the Keychain, like the model API key.

    @Published var spotifyClientID: String { didSet { defaults.set(spotifyClientID, forKey: Keys.spotifyClientID) } }
    @Published var spotifyClientSecret: String { didSet { KeychainStore.set(spotifyClientSecret, for: Keys.spotifyClientSecret) } }

    // MARK: - Awareness & memory

    /// Work without taking over the screen: silent shell commands instead of
    /// a Terminal window, background tabs instead of foreground ones. On by
    /// default. Doesn't (and can't) cover everything -- opening an app, or
    /// anything driving the keyboard/mouse, has to bring that app forward to
    /// work at all.
    @Published var quietModeEnabled: Bool { didSet { defaults.set(quietModeEnabled, forKey: Keys.quietMode) } }

    @Published var situationalAwarenessEnabled: Bool { didSet { defaults.set(situationalAwarenessEnabled, forKey: Keys.awareness) } }
    @Published var activityTrackingEnabled: Bool { didSet { defaults.set(activityTrackingEnabled, forKey: Keys.activityTracking) } }
    @Published var includeBrowserHistoryInReview: Bool { didSet { defaults.set(includeBrowserHistoryInReview, forKey: Keys.browserHistory) } }

    // MARK: - Proactive behavior

    @Published var proactiveEnabled: Bool {
        didSet {
            defaults.set(proactiveEnabled, forKey: Keys.proactive)
            // Actions are a strict subset of proactive -- turning proactive
            // off should never leave "autonomous actions" silently still
            // on in storage, waiting to reactivate the moment proactive
            // gets flipped back on without the person re-deciding on it.
            if !proactiveEnabled { proactiveActionsEnabled = false }
        }
    }
    @Published var proactiveActionsEnabled: Bool { didSet { defaults.set(proactiveActionsEnabled, forKey: Keys.proactiveActions) } }

    // MARK: - Daily review

    @Published var dailyReviewEnabled: Bool { didSet { defaults.set(dailyReviewEnabled, forKey: Keys.dailyReviewEnabled) } }
    @Published var dailyReviewTime: Date { didSet { defaults.set(dailyReviewTime, forKey: Keys.dailyReviewTime) } }

    // MARK: - Storage

    private let defaults = UserDefaults.standard

    private enum Keys {
        static let provider = "settings.provider"
        static let plannerModel = "settings.plannerModel"
        static let agentModel = "settings.agentModel"
        static let searchModel = "settings.searchModel"
        static let apiBaseURL = "settings.apiBaseURL"
        static let apiChatModel = "settings.apiChatModel"
        static let apiPlannerModelOverride = "settings.apiPlannerModelOverride"
        static let apiAgentModelOverride = "settings.apiAgentModelOverride"
        static let apiSearchModelOverride = "settings.apiSearchModelOverride"
        static let apiKey = "apiKey" // Keychain account name, not a UserDefaults key
        static let embedProvider = "settings.embedProvider"
        static let embedModel = "settings.embedModel"
        static let apiEmbedModel = "settings.apiEmbedModel"
        static let spotifyClientID = "settings.spotifyClientID"
        static let spotifyClientSecret = "spotifyClientSecret"  // Keychain account
        static let quietMode = "settings.quietMode"
        static let awareness = "settings.awareness"
        static let activityTracking = "settings.activityTracking"
        static let browserHistory = "settings.browserHistory"
        static let proactive = "settings.proactive"
        static let proactiveActions = "settings.proactiveActions"
        static let dailyReviewEnabled = "settings.dailyReviewEnabled"
        static let dailyReviewTime = "settings.dailyReviewTime"
    }

    // MARK: - Init
    // Every default mirrors an existing fallback exactly -- BackendBridge
    // .swift's own hardcoded defaults where it has one (provider,
    // apiBaseURL, apiChatModel, embedProvider), llm_client.py's where it
    // doesn't (plannerModel, agentModel, embedModel) -- so a fresh
    // install behaves identically to before this Settings window
    // existed. activityTrackingEnabled is the one deliberate exception:
    // it defaults to `false` here regardless of what bridge.py does,
    // because bridge.py's own default was actually the *opposite* of
    // what a neighboring file's comment claimed ("off by default") until
    // that got fixed alongside this. Defaulting to false here too means
    // continuous activity logging never silently turns itself on,
    // regardless of which copy of bridge.py ends up running it.

    private init() {
        provider = Provider(rawValue: defaults.string(forKey: Keys.provider) ?? "") ?? .api
        plannerModel = defaults.string(forKey: Keys.plannerModel) ?? "qwen3.6:27b"
        agentModel = defaults.string(forKey: Keys.agentModel) ?? "qwen3.5:9b"
        searchModel = defaults.string(forKey: Keys.searchModel) ?? ""
        apiBaseURL = defaults.string(forKey: Keys.apiBaseURL)
            ?? "https://generativelanguage.googleapis.com/v1beta/openai"
        apiChatModel = defaults.string(forKey: Keys.apiChatModel) ?? "gemini-3.1-flash-lite"
        apiPlannerModelOverride = defaults.string(forKey: Keys.apiPlannerModelOverride) ?? ""
        apiAgentModelOverride = defaults.string(forKey: Keys.apiAgentModelOverride) ?? ""
        apiSearchModelOverride = defaults.string(forKey: Keys.apiSearchModelOverride) ?? ""
        apiKey = KeychainStore.get(Keys.apiKey) ?? ""
        embedProvider = Provider(rawValue: defaults.string(forKey: Keys.embedProvider) ?? "") ?? .local
        embedModel = defaults.string(forKey: Keys.embedModel) ?? "nomic-embed-text"
        apiEmbedModel = defaults.string(forKey: Keys.apiEmbedModel) ?? ""
        spotifyClientID = defaults.string(forKey: Keys.spotifyClientID) ?? ""
        spotifyClientSecret = KeychainStore.get(Keys.spotifyClientSecret) ?? ""
        quietModeEnabled = defaults.object(forKey: Keys.quietMode) as? Bool ?? true
        situationalAwarenessEnabled = defaults.object(forKey: Keys.awareness) as? Bool ?? true
        activityTrackingEnabled = defaults.object(forKey: Keys.activityTracking) as? Bool ?? false
        includeBrowserHistoryInReview = defaults.object(forKey: Keys.browserHistory) as? Bool ?? true
        proactiveEnabled = defaults.object(forKey: Keys.proactive) as? Bool ?? false
        proactiveActionsEnabled = defaults.object(forKey: Keys.proactiveActions) as? Bool ?? false
        dailyReviewEnabled = defaults.object(forKey: Keys.dailyReviewEnabled) as? Bool ?? false
        dailyReviewTime = defaults.object(forKey: Keys.dailyReviewTime) as? Date
            ?? Calendar.current.date(bySettingHour: 21, minute: 0, second: 0, of: Date()) ?? Date()
    }

    // MARK: - Bridging to the Python backend
    // bridge.py/llm_client.py read all of this at *import* time, as plain
    // module-level constants computed once from os.environ -- there's no
    // hot-reload path on the Python side, so a settings change only takes
    // effect the next time bridge.py actually starts. See
    // applyToProcessEnvironment() and AppState.restartBackend().

    var backendEnvironment: [String: String] {
        var env: [String: String] = [
            "JUGNU_PROVIDER": provider.rawValue,
            "JUGNU_EMBED_PROVIDER": embedProvider.rawValue,
            "JUGNU_QUIET_MODE": quietModeEnabled ? "1" : "0",
            "JUGNU_ENABLE_AWARENESS": situationalAwarenessEnabled ? "1" : "0",
            "JUGNU_TRACK_ACTIVITY": activityTrackingEnabled ? "1" : "0",
            "JUGNU_INCLUDE_BROWSER_HISTORY": includeBrowserHistoryInReview ? "1" : "0",
            "JUGNU_PROACTIVE": proactiveEnabled ? "1" : "0",
            "JUGNU_PROACTIVE_ACTIONS": proactiveActionsEnabled ? "1" : "0",
            "JUGNU_DAILY_REVIEW": dailyReviewEnabled ? "1" : "0",
            "JUGNU_DAILY_REVIEW_TIME": dailyReviewTimeString,
        ]

        switch provider {
        case .local:
            env["JUGNU_PLANNER_MODEL"] = plannerModel
            // AGENT role's local-mode model var is just JUGNU_MODEL, not
            // JUGNU_AGENT_MODEL -- it also doubles as SEARCH's fallback.
            env["JUGNU_MODEL"] = agentModel
            if !searchModel.isEmpty { env["JUGNU_SEARCH_MODEL"] = searchModel }
        case .api:
            env["JUGNU_API_BASE_URL"] = apiBaseURL
            env["JUGNU_API_CHAT_MODEL"] = apiChatModel
            if !apiPlannerModelOverride.isEmpty { env["JUGNU_PLANNER_API_MODEL"] = apiPlannerModelOverride }
            if !apiAgentModelOverride.isEmpty { env["JUGNU_AGENT_API_MODEL"] = apiAgentModelOverride }
            if !apiSearchModelOverride.isEmpty { env["JUGNU_SEARCH_API_MODEL"] = apiSearchModelOverride }
            if !apiKey.isEmpty {
                // First of several candidate names llm_client._api_key()
                // checks, so this always wins regardless of what
                // provider-native vars (OPENAI_API_KEY etc.) might also
                // happen to be set in the shell Jugnu was launched from.
                env["JUGNU_API_KEY"] = apiKey
            }
        }

        if !spotifyClientID.isEmpty && !spotifyClientSecret.isEmpty {
            env["SPOTIFY_CLIENT_ID"] = spotifyClientID
            env["SPOTIFY_CLIENT_SECRET"] = spotifyClientSecret
        }

        switch embedProvider {
        case .local:
            env["JUGNU_EMBED_MODEL"] = embedModel
        case .api:
            env["JUGNU_API_EMBED_MODEL"] = apiEmbedModel
        }

        return env
    }

    private var dailyReviewTimeString: String {
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm"
        return formatter.string(from: dailyReviewTime)
    }

    // NOTE: there used to be an applyToProcessEnvironment() here that
    // setenv()'d every value above -- the Keychain-stored API key
    // included -- into Jugnu's own process, relying on BackendBridge to
    // pick them up incidentally through ProcessInfo.processInfo.environment.
    //
    // It's gone. AppState now passes `backendEnvironment` straight to
    // BackendBridge.start(workingDirectory:settingsEnvironment:), which
    // layers it onto the child's environment and nowhere else. Same
    // precedence, same behaviour, minus a copy of the user's API key
    // sitting in this process's environment block for the lifetime of the
    // app where crash reports, diagnostics and every other child process
    // could read it. Putting the key in the Keychain rather than
    // UserDefaults (see KeychainStore) was the entire point; leaving it in
    // `environ` gave most of that back.

    // MARK: - Data location (Privacy section)
    // Mirrors memory.py's own default (APP_SUPPORT_DIR) exactly. Doesn't
    // account for JUGNU_DATA_DIR being set to something else -- that
    // variable is deliberately not exposed as a setting (see the type's
    // doc comment), so this is only wrong if it's been overridden by hand
    // outside of Jugnu's own settings.

    static var dataDirectoryURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/Jugnu", isDirectory: true)
    }

    static var dataDirectoryDisplayPath: String {
        dataDirectoryURL.path.replacingOccurrences(of: NSHomeDirectory(), with: "~")
    }

    static func revealDataDirectory() {
        NSWorkspace.shared.activateFileViewerSelecting([dataDirectoryURL])
    }

    static var appVersion: String {
        let short = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "—"
        let build = Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? ""
        return build.isEmpty ? short : "\(short) (\(build))"
    }
}
