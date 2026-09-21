import Foundation
import AppKit
import Combine
import SwiftUI

@MainActor
final class AppState: ObservableObject {
    /// The one AppState. Owned here rather than by a view, so closing or
    /// hiding the chat window never tears down the conversation or the
    /// backend process behind it.
    static let shared = AppState()

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

    // MARK: Agent activity
    // Everything the backend reports about work in progress: the plan and
    // its tasks, each tool call, approvals waiting on the user. Kept
    // separate from `messages` so the conversation stays a conversation --
    // the activity drawer shows this detail only when asked.

    /// Most recent first; capped so a long session doesn't grow unbounded.
    @Published var runs: [RunActivity] = []
    @Published var approvals: [ApprovalRequest] = []
    /// The latest one-line progress update for the active run, shown under
    /// the answer while Jugnu works on something long.
    @Published var progressLine: String?
    @Published var securityNotice: String?
    @Published var permissions: JSONValue = .null
    @Published var auditEvents: [JSONValue] = []
    @Published var noticeMessage: String?

    // Management data for the activity panel's tabs, refreshed on demand.
    @Published var memories: [JSONValue] = []
    @Published var memoryCounts: JSONValue = .null
    @Published var skillLibrary: [JSONValue] = []
    @Published var runHistory: [JSONValue] = []
    @Published var schedules: [JSONValue] = []
    @Published var osPermissions: JSONValue = .null
    @Published var toolStats: [JSONValue] = []
    @Published var proposals: [JSONValue] = []
    @Published var interruptedRuns: [JSONValue] = []
    @Published var suggestedSkillName: String?

    var activeRun: RunActivity? { runs.first(where: { $0.isActive && $0.origin == "user" }) }
    var backgroundRuns: [RunActivity] { runs.filter { $0.isActive && $0.origin != "user" } }
    /// Something in the activity panel wants attention: an approval, a
    /// suggested skill, or an interrupted task.
    var activityNeedsAttention: Bool {
        !approvals.isEmpty || suggestedSkillName != nil || !interruptedRuns.isEmpty
    }
    private static let maxRuns = 30

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

    /// The environment the running backend was started with, so Settings can
    /// tell whether a change still needs a restart to apply.
    @Published private(set) var launchedEnvironment: [String: String] = [:]

    init() {}

    /// Starts bridge.py from the app bundle.
    func launchBackend() {
        guard let scriptPath = Bundle.main.path(forResource: "bridge", ofType: "py", inDirectory: "PythonBackend") else {
            errorMessage = "Couldn't find bridge.py in the app bundle. Add the PythonBackend folder to the target "
                + "as a folder reference (blue icon), not a group."
            statusLabel = "error"
            return
        }
        launch(bridgeScriptPath: scriptPath, workingDirectory: (scriptPath as NSString).deletingLastPathComponent)
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
        let environment = SettingsStore.shared.backendEnvironment
        newBridge.start(workingDirectory: workingDirectory, settingsEnvironment: environment)
        launchedEnvironment = environment
        bridge = newBridge
    }

    /// Which Mac account the next requests run on: "auto" (the default from
    /// Settings, or whatever the request itself says), "primary" (yours) or
    /// "agent" (the Jugnu account). Stays until changed.
    @Published var targetAccount: String = UserDefaults.standard.string(forKey: "jugnu.targetAccount") ?? "auto" {
        didSet { UserDefaults.standard.set(targetAccount, forKey: "jugnu.targetAccount") }
    }

    func send(_ text: String) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !thinking, !trimmed.isEmpty else { return }

        let id = UUID().uuidString
        thinking = true
        statusLabel = "thinking"
        errorMessage = nil
        messages.append(ChatMessage(role: .user, content: trimmed))
        var extra: [String: JSONValue] = [:]
        if targetAccount == "primary" || targetAccount == "agent" {
            extra["session"] = .string(targetAccount)
        }
        bridge?.send(OutgoingRequest(type: "send", id: id, text: trimmed, extra: extra))
    }

    // MARK: - Run control

    func respond(to approval: ApprovalRequest, decision: String, note: String = "") {
        approvals.removeAll { $0.id == approval.id }
        bridge?.send(OutgoingRequest(type: "approval_response", id: nil, text: nil, extra: [
            "approval_id": .string(approval.id),
            "decision": .string(decision),
            "note": .string(note),
        ]))
    }

    /// Stops the run in progress (or a specific background run). The
    /// backend stops at its next checkpoint -- between tool calls, or
    /// immediately if it's waiting on an approval.
    func cancelRun(_ runId: String? = nil) {
        guard let target = runId ?? activeRun?.id else { return }
        updateRun(target) { $0.state = "cancelling" }
        bridge?.send(OutgoingRequest(type: "cancel", id: nil, text: nil, extra: ["run_id": .string(target)]))
    }

    func resumeInterrupted(_ runId: String) {
        interruptedRuns.removeAll { $0["run_id"]?.string == runId }
        sendRequest("run_resume", ["run_id": .string(runId)])
    }

    func dismissInterrupted(_ runId: String) {
        interruptedRuns.removeAll { $0["run_id"]?.string == runId }
    }

    func pauseRun(_ runId: String) {
        bridge?.send(OutgoingRequest(type: "pause", id: nil, text: nil, extra: ["run_id": .string(runId)]))
    }

    func resumeRun(_ runId: String) {
        bridge?.send(OutgoingRequest(type: "resume", id: nil, text: nil, extra: ["run_id": .string(runId)]))
    }

    // MARK: - Management requests

    func refreshPermissions() {
        bridge?.send(OutgoingRequest(type: "get_permissions", id: nil, text: nil))
    }

    func updatePermissions(_ patch: [String: JSONValue]) {
        bridge?.send(OutgoingRequest(type: "set_permissions", id: nil, text: nil,
                                     extra: ["patch": .object(patch)]))
    }

    func refreshAudit(limit: Int = 200) {
        bridge?.send(OutgoingRequest(type: "get_audit", id: nil, text: nil, extra: ["limit": .number(Double(limit))]))
    }

    /// Generic management request for the memory/skills/task views.
    func sendRequest(_ type: String, _ extra: [String: JSONValue] = [:]) {
        bridge?.send(OutgoingRequest(type: type, id: nil, text: nil, extra: extra))
    }

    private func updateRun(_ runId: String, _ change: (inout RunActivity) -> Void) {
        guard let index = runs.firstIndex(where: { $0.id == runId }) else { return }
        change(&runs[index])
    }

    private func ensureRun(_ runId: String, requestId: String, goal: String, origin: String = "user") {
        guard !runId.isEmpty, !runs.contains(where: { $0.id == runId }) else { return }
        runs.insert(RunActivity(id: runId, requestId: requestId, goal: goal, origin: origin), at: 0)
        if runs.count > Self.maxRuns { runs.removeLast(runs.count - Self.maxRuns) }
    }

    private func finishRun(_ runId: String, state: String) {
        updateRun(runId) { run in
            run.state = state
            run.finished = Date()
            for i in run.tasks.indices where !run.tasks[i].state.isFinished {
                run.tasks[i].state = state == "cancelled" ? .cancelled : (state == "failed" ? .failed : run.tasks[i].state)
            }
        }
        if activeRun == nil { progressLine = nil }
        approvals.removeAll { $0.runId == runId }
    }

    /// Handles every activity-related event. Returns true if it consumed it.
    private func handleActivity(_ event: IncomingEvent) -> Bool {
        let p = event.payload
        let runId = p["run_id"]?.string ?? ""
        switch event.type {
        case "run_started":
            let origin = p["origin"]?.string ?? "user"
            ensureRun(runId, requestId: event.id ?? "", goal: p["goal"]?.string ?? "", origin: origin)
            if origin == "user" {
                progressLine = nil
                securityNotice = nil
            }
            interruptedRuns.removeAll { $0["run_id"]?.string == runId }
            return true

        case "plan":
            ensureRun(runId, requestId: event.id ?? "", goal: p["goal"]?.string ?? "")
            updateRun(runId) { run in
                run.objective = p["objective"]?.string ?? p["goal"]?.string ?? run.goal
                run.assumptions = (p["assumptions"]?.array ?? []).compactMap { $0.string }
                run.tasks = (p["tasks"]?.array ?? []).map { PlanTask(json: $0) }
                run.session = p["session"]?.string ?? ""
            }
            return true

        case "task_status", "task_update":
            let taskId = p["task_id"]?.string ?? ""
            let state = TaskState(rawValue: p["status"]?.string ?? "")
            updateRun(runId) { run in
                if let i = run.tasks.firstIndex(where: { $0.id == taskId }) {
                    if let state { run.tasks[i].state = state }
                    if let note = p["note"]?.string { run.tasks[i].note = note }
                } else if let task = p["task"], task["id"] != nil {
                    var t = PlanTask(json: task)
                    if let state { t.state = state }
                    run.tasks.append(t)
                }
            }
            return true

        case "replan":
            updateRun(runId) { run in
                for task in p["tasks"]?.array ?? [] where !run.tasks.contains(where: { $0.id == task["id"]?.string }) {
                    run.tasks.append(PlanTask(json: task))
                }
                run.assumptions = (p["assumptions"]?.array ?? []).compactMap { $0.string }
                if let reason = p["reason"]?.string, !reason.isEmpty { run.progress.append("Revised plan: \(reason)") }
            }
            return true

        case "verification":
            let taskId = p["task_id"]?.string ?? ""
            updateRun(runId) { run in
                if let i = run.tasks.firstIndex(where: { $0.id == taskId }), let reason = p["reason"]?.string {
                    run.tasks[i].note = reason
                }
            }
            return true

        case "background_done":
            let content = p["content"]?.string ?? ""
            messages.append(ChatMessage(role: .assistant, content: content, origin: .background))
            finishRun(runId, state: p["state"]?.string ?? "done")
            notifyIfBackgrounded(title: "Background task", body: content)
            return true

        case "run_interrupted":
            interruptedRuns.append(p)
            return true

        case "skill_suggested":
            suggestedSkillName = p["skill"]?["name"]?.string
            sendRequest("skills_list")
            return true

        case "memories":
            memories = p["items"]?.array ?? []
            memoryCounts = p["counts"] ?? .null
            return true

        case "skills":
            skillLibrary = p["skills"]?.array ?? []
            return true

        case "runs":
            runHistory = p["runs"]?.array ?? []
            return true

        case "schedules":
            schedules = p["schedules"]?.array ?? []
            return true

        case "os_permissions":
            osPermissions = p["sessions"] ?? .null
            return true

        case "tool_stats":
            toolStats = p["tools"]?.array ?? []
            return true

        case "proposals":
            proposals = p["proposals"]?.array ?? []
            return true

        case "tool_start":
            let activity = ToolActivity(runId: runId, taskId: p["task_id"]?.string ?? "",
                                        tool: p["tool"]?.string ?? "tool",
                                        summary: p["summary"]?.string ?? "", risk: p["risk"]?.string ?? "")
            updateRun(runId) { run in
                run.tools.append(activity)
                if run.tools.count > 200 { run.tools.removeFirst(run.tools.count - 200) }
            }
            return true

        case "tool_end":
            let tool = p["tool"]?.string ?? ""
            updateRun(runId) { run in
                if let i = run.tools.lastIndex(where: { $0.tool == tool && $0.ok == nil }) {
                    run.tools[i].ok = p["ok"]?.bool
                    run.tools[i].errorType = p["error_type"]?.string
                    run.tools[i].preview = p["preview"]?.string ?? ""
                    run.tools[i].duration = p["duration"]?.double
                } else if p["ok"]?.bool == false {
                    // Rejected before running (bad arguments, blocked, denied).
                    var a = ToolActivity(runId: runId, taskId: p["task_id"]?.string ?? "", tool: tool,
                                         summary: p["preview"]?.string ?? "", risk: "")
                    a.ok = false
                    a.errorType = p["error_type"]?.string
                    run.tools.append(a)
                }
            }
            return true

        case "progress":
            let line = p["message"]?.string ?? ""
            updateRun(runId) { run in
                run.progress.append(line)
                if run.progress.count > 100 { run.progress.removeFirst(run.progress.count - 100) }
            }
            if runs.first(where: { $0.id == runId })?.origin ?? "user" == "user" { progressLine = line }
            return true

        case "approval_request":
            if let approval = ApprovalRequest(json: p), !approvals.contains(where: { $0.id == approval.id }) {
                approvals.append(approval)
                if !NSApp.isActive {
                    NotificationManager.shared.notify(title: "Jugnu needs your OK", body: approval.summary)
                }
            }
            return true

        case "approval_resolved":
            let approvalId = p["approval_id"]?.string ?? ""
            approvals.removeAll { $0.id == approvalId }
            return true

        case "approvals":
            approvals = (p["approvals"]?.array ?? []).compactMap { ApprovalRequest(json: $0) }
            return true

        case "run_state":
            let state = p["state"]?.string ?? "running"
            updateRun(runId) { $0.state = state }
            return true

        case "run_finished":
            finishRun(runId, state: p["state"]?.string ?? "done")
            return true

        case "security_warning":
            securityNotice = p["message"]?.string
            return true

        case "permissions":
            permissions = p["permissions"] ?? .null
            if p["saved"]?.bool == true { showSettingsFeedback("Permissions saved.") }
            return true

        case "audit":
            auditEvents = p["events"]?.array ?? []
            return true

        case "notice":
            noticeMessage = p["message"]?.string
            Task { @MainActor in
                try? await Task.sleep(nanoseconds: 4_000_000_000)
                self.noticeMessage = nil
            }
            return true

        default:
            return false
        }
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
        if handleActivity(event) { return }
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
            refreshPermissions()
            sendRequest("get_approvals")
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
            if let runId = event.payload["run_id"]?.string {
                let usage = event.payload["usage"]
                ensureRun(runId, requestId: event.id ?? "", goal: messages.last(where: { $0.role == .user })?.content ?? "")
                updateRun(runId) { run in
                    run.modelCalls = usage?["calls"]?.int
                    run.tokens = (usage?["prompt_tokens"]?.int ?? 0) + (usage?["completion_tokens"]?.int ?? 0)
                }
                finishRun(runId, state: event.payload["cancelled"]?.bool == true ? "cancelled" : "done")
            }
            progressLine = nil
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
            if let runId = event.payload["run_id"]?.string { finishRun(runId, state: "failed") }
            progressLine = nil
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
