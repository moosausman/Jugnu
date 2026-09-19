import Foundation
import AppKit
import Combine
import SwiftUI

@MainActor
final class AppState: ObservableObject {
    /// Lets AppKit-side singletons (MenuBarController) reach the live
    /// AppState without threading it through AppDelegate -- same pattern
    /// as NotificationManager.shared / MenuBarController.shared elsewhere.
    static weak var shared: AppState?

    @Published var messages: [ChatMessage] = []
    @Published var thinking = false
    @Published var statusLabel = "starting"
    @Published var errorMessage: String?
    @Published var plannerModelName = ""
    @Published var agentModelName = ""
    @Published var toolCount = 0

    // Capability flags the backend reports on "ready"/"ping". Nothing in
    // the UI reads these yet -- exposed here so a future settings/status
    // view can find out what's actually turned on without a protocol
    // change. See bridge.py's module docstring for what each one gates.
    @Published var proactiveEnabled = false
    @Published var proactiveActionsEnabled = false
    @Published var activityTrackingEnabled = false
    @Published var dailyReviewScheduled = false

    /// Kept as a real, assignable @Published property (not derived) so
    /// anything already reading -- or, in principle, setting -- a single
    /// model name keeps working unchanged. Populated from
    /// plannerModelName/agentModelName on "ready"; prefer those two
    /// directly in new code, since Jugnu actually runs two different
    /// models for two different jobs (see llm_client.py) and this just
    /// smushes them into one string for display.
    @Published var modelName = ""

    /// Brief, self-clearing confirmation for an action that has no other
    /// natural home in the UI -- right now just "Facts cleared."/"Memory
    /// cleared." from Settings' Privacy section, which isn't part of the
    /// chat transcript and has nothing else to show success or failure.
    @Published var settingsFeedback: String?

    private var bridge: BackendBridge?

    /// Remembered from launch() so restartBackend() can relaunch bridge.py
    /// with the exact same paths -- launch() itself only ever receives
    /// these once, from ContentView's launchBackend(), with nowhere to
    /// reuse them from otherwise.
    private var bridgeScriptPath: String?
    private var workingDirectory: String?

    /// True only between calling requestDailyReview() and the "digest"
    /// (or "error") that answers it -- distinguishes that reply from a
    /// "digest" arriving unprompted from the scheduled trigger, which
    /// should NOT touch thinking/statusLabel since nothing was waiting on it.
    private var awaitingDailyReview = false

    init() {
        AppState.shared = self
    }

    func launch(bridgeScriptPath: String, workingDirectory: String) {
        NotificationManager.shared.requestAuthorization()

        self.bridgeScriptPath = bridgeScriptPath
        self.workingDirectory = workingDirectory

        launchBridgeProcess(bridgeScriptPath: bridgeScriptPath, workingDirectory: workingDirectory)

        NotificationCenter.default.addObserver(
            forName: NSApplication.willTerminateNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            guard let self else { return }
            Task { @MainActor in
                self.bridge?.stop()
            }
        }
    }

    /// Stops the current backend process and relaunches it with whatever
    /// SettingsStore holds right now -- the only way any Settings change
    /// actually takes effect, since bridge.py and llm_client.py read
    /// their environment variables once at import time, not per turn.
    /// Safe to call mid-conversation: in-memory `messages` isn't touched,
    /// so the transcript on screen survives the restart even though the
    /// new process reloads its own copy from disk independently.
    ///
    /// Waits for BackendBridge.stop()'s completion before starting the
    /// new process rather than firing both at once -- stop() used to be
    /// synchronous, so the old sequential code was accidentally correct;
    /// now that it isn't (see BackendBridge.swift), starting a second
    /// bridge.py before the first one has actually exited would mean two
    /// processes briefly touching the same conversation/facts files.
    func restartBackend() {
        guard let bridgeScriptPath, let workingDirectory else { return }
        guard !thinking else { return } // don't yank the backend out from under an in-flight turn

        statusLabel = "starting"
        errorMessage = nil

        guard let bridge else {
            launchBridgeProcess(bridgeScriptPath: bridgeScriptPath, workingDirectory: workingDirectory)
            return
        }
        bridge.stop { [weak self] in
            self?.launchBridgeProcess(bridgeScriptPath: bridgeScriptPath, workingDirectory: workingDirectory)
        }
    }

    /// Shared by launch() and restartBackend() -- both need "create a
    /// BackendBridge, wire its callbacks, start it" and previously
    /// duplicated all three lines of that verbatim.
    private func launchBridgeProcess(bridgeScriptPath: String, workingDirectory: String) {
        let newBridge = BackendBridge(bridgeScriptPath: bridgeScriptPath)
        newBridge.onEvent = { [weak self] event in self?.handle(event) }
        newBridge.onLaunchFailure = { [weak self] message in
            self?.errorMessage = message
            self?.statusLabel = "error"
            self?.thinking = false
        }
        // Read fresh at every launch, so a Settings change picked up by
        // restartBackend() takes effect. bridge.py and llm_client.py read
        // their environment once at import time and have no hot-reload
        // path, which is exactly why restarting the process is the only
        // way a settings change ever applies.
        newBridge.start(
            workingDirectory: workingDirectory,
            settingsEnvironment: SettingsStore.shared.backendEnvironment
        )
        bridge = newBridge
    }

    func send(_ text: String) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !thinking, !trimmed.isEmpty else { return }

        let id = UUID().uuidString
        thinking = true
        statusLabel = "thinking"
        errorMessage = nil
        messages.append(ChatMessage(role: .user, content: trimmed))
        bridge?.send(OutgoingRequest(type: "send", id: id, text: trimmed))
    }

    func clear() {
        messages.removeAll()
        errorMessage = nil
        bridge?.send(OutgoingRequest(type: "clear", id: nil, text: nil))
    }

    /// Asks for today's recap right now instead of waiting for the
    /// scheduled trigger (if one's even configured) -- see
    /// daily_review.py. Shares the same "thinking" busy state as a
    /// normal send(), since it shares the backend's turn_lock and a
    /// recap can genuinely take a while to assemble (it runs a couple of
    /// small agent tasks -- Notion, calendar -- along the way).
    func requestDailyReview() {
        guard !thinking else { return }
        let id = UUID().uuidString
        thinking = true
        statusLabel = "thinking"
        errorMessage = nil
        awaitingDailyReview = true
        bridge?.send(OutgoingRequest(type: "daily_review", id: id, text: nil))
    }

    func shutdown() {
        bridge?.stop()
    }

    /// Wipes memory.py's saved facts (name, preferences, standing
    /// instructions) -- see bridge.py's "reset_facts" handler. Doesn't
    /// touch the conversation on screen or semantic memory; those are
    /// resetMemory()'s and clear()'s jobs respectively. Refuses mid-turn
    /// for the same reason restartBackend() does -- this shares the
    /// backend's turn_lock, so it would otherwise queue up behind
    /// whatever's already running rather than actually doing nothing.
    func resetFacts() {
        guard !thinking else { return }
        bridge?.send(OutgoingRequest(type: "reset_facts", id: nil, text: nil))
    }

    /// Wipes semantic_memory.py's longitudinal store -- activity log,
    /// search history, habit patterns -- see bridge.py's "reset_memory"
    /// handler. Same scope note as resetFacts(): conversation and facts
    /// are untouched.
    func resetMemory() {
        guard !thinking else { return }
        bridge?.send(OutgoingRequest(type: "reset_memory", id: nil, text: nil))
    }

    /// Only worth a banner if the person's actually stepped away --
    /// no point notifying about a reply that's already sitting right in
    /// front of them. `NSApp.isActive` is true whenever Jugnu is the
    /// frontmost app, whether or not the chat window itself is visible.
    private func notifyIfBackgrounded(title: String, body: String) {
        guard !NSApp.isActive, !body.isEmpty else { return }
        NotificationManager.shared.notify(title: title, body: body)
    }

    private func handle(_ event: IncomingEvent) {
        switch event.type {
        case "ready":
            plannerModelName = event.plannerModel ?? ""
            agentModelName = event.agentModel ?? ""
            modelName = [plannerModelName, agentModelName].filter { !$0.isEmpty }.joined(separator: " / ")
            toolCount = event.toolCount ?? 0
            proactiveEnabled = event.proactive ?? false
            proactiveActionsEnabled = event.proactiveActions ?? false
            activityTrackingEnabled = event.activityTracking ?? false
            dailyReviewScheduled = event.dailyReview ?? false
            statusLabel = "ready"
            if let history = event.history {
                messages = history.map {
                    ChatMessage(role: ChatMessage.Role(rawValue: $0.role) ?? .assistant, content: $0.content)
                }
            }

        case "status":
            statusLabel = event.label ?? "thinking"

        case "tool":
            let name = event.name ?? "tool"
            messages.append(ChatMessage(role: .tool, content: "\(name)(\(event.content ?? ""))"))

        case "assistant":
            let content = event.content ?? ""
            messages.append(ChatMessage(role: .assistant, content: content))
            thinking = false
            statusLabel = "ready"
            notifyIfBackgrounded(title: "Jugnu's done", body: content)

        case "nudge":
            // Always unprompted -- nothing was in flight, so thinking/
            // statusLabel are left exactly as they already are.
            let content = event.content ?? ""
            messages.append(ChatMessage(role: .assistant, content: content, origin: .nudge))
            notifyIfBackgrounded(title: "Jugnu checked in", body: content)

        case "digest":
            let content = event.content ?? ""
            messages.append(ChatMessage(role: .assistant, content: content, origin: .digest))
            if awaitingDailyReview {
                // The reply to requestDailyReview(), not the scheduled
                // push -- clear the busy state that call set.
                awaitingDailyReview = false
                thinking = false
                statusLabel = "ready"
            }
            notifyIfBackgrounded(title: "Daily digest ready", body: content)

        case "error":
            errorMessage = event.message ?? "Something went wrong."
            awaitingDailyReview = false
            thinking = false
            statusLabel = "error"

        case "cleared":
            statusLabel = "ready"

        case "facts_cleared":
            showSettingsFeedback("Facts cleared.")

        case "memory_cleared":
            showSettingsFeedback("Memory cleared.")

        default:
            break
        }
    }

    /// Shows a brief confirmation in Settings' Privacy section, then
    /// clears itself -- a fire-and-forget toast rather than a persistent
    /// piece of state anything else needs to reset by hand. 2.5s is long
    /// enough to register, short enough that it's gone well before
    /// anyone would wonder why it's still there.
    private func showSettingsFeedback(_ message: String) {
        settingsFeedback = message
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 2_500_000_000)
            if settingsFeedback == message {
                settingsFeedback = nil
            }
        }
    }
}
