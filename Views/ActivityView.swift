import SwiftUI

/// Everything Jugnu is doing and knows, one tap away from the conversation:
/// the current objective and plan, background and scheduled work, memories,
/// learned skills, and the full audit log. The main window stays a quiet
/// conversation; detail lives here.
struct ActivityView: View {
    @ObservedObject var appState: AppState
    @Binding var isPresented: Bool
    @State private var tab: Tab = .now

    enum Tab: String, CaseIterable, Identifiable {
        case now = "Now", tasks = "Tasks", memory = "Memory", skills = "Skills", log = "Log"
        var id: String { rawValue }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
                .zIndex(1)   // keeps scrolled content from taking the header's clicks
            Rectangle().fill(Theme.border).frame(height: 1)
            ScrollView {
                Group {
                    switch tab {
                    case .now: NowTab(appState: appState)
                    case .tasks: TasksTab(appState: appState)
                    case .memory: MemoryTab(appState: appState)
                    case .skills: SkillsTab(appState: appState)
                    case .log: LogTab(appState: appState)
                    }
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 12)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .background(WindowBackdrop())
        .onAppear(perform: refresh)
        .onChange(of: tab) { _, _ in refresh() }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Text("Activity")
                    .font(Theme.text(12.5, weight: .semibold))
                    .foregroundStyle(Theme.ink)
                Spacer()
                IconButton(symbol: "arrow.clockwise", size: 11.5, help: "Refresh", action: refresh)
                IconButton(symbol: "xmark", size: 11, help: "Close (Esc)") { isPresented = false }
                    .keyboardShortcut(.escape, modifiers: [])
            }
            .padding(.leading, 36)
            .padding(.trailing, 8)
            .frame(height: 30)
            .padding(.top, 1)

            Picker("", selection: $tab) {
                ForEach(Tab.allCases) { t in Text(t.rawValue).tag(t) }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .padding(.horizontal, 14)
            .padding(.bottom, 10)
        }
    }

    private func refresh() {
        switch tab {
        case .now: appState.sendRequest("get_approvals")
        case .tasks:
            appState.sendRequest("runs_list")
            appState.sendRequest("schedules_list")
        case .memory:
            appState.sendRequest("memory_list")
            appState.sendRequest("tool_stats")
        case .skills:
            appState.sendRequest("skills_list")
            appState.sendRequest("proposals_list")
            appState.suggestedSkillName = nil
        case .log: appState.refreshAudit()
        }
    }
}

// MARK: - Shared pieces

struct SectionLabel: View {
    let text: String
    var body: some View {
        Text(text)
            .font(Theme.text(11.5, weight: .semibold))
            .foregroundStyle(Theme.inkFaint)
            .padding(.top, 6)
    }
}

struct Pill: View {
    let text: String
    var tint: Color = Theme.inkDim
    var body: some View {
        Text(text)
            .font(Theme.text(10, weight: .medium))
            .foregroundStyle(tint)
            .padding(.horizontal, 6).padding(.vertical, 1.5)
            .background(Capsule().fill(tint.opacity(0.12)))
    }
}

struct Card<Content: View>: View {
    @ViewBuilder var content: Content
    var body: some View {
        VStack(alignment: .leading, spacing: 6) { content }
            .padding(11)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: Theme.radiusMedium, style: .continuous).fill(Theme.panel))
            .overlay(RoundedRectangle(cornerRadius: Theme.radiusMedium, style: .continuous).stroke(Theme.border, lineWidth: 1))
    }
}

private func stateColor(_ state: TaskState) -> Color {
    switch state {
    case .done: return Theme.success
    case .failed, .blocked: return Theme.danger
    case .waitingApproval: return Theme.clay
    case .running, .verifying: return Theme.clay
    default: return Theme.inkFaint
    }
}

private func relative(_ ts: Double?) -> String {
    guard let ts, ts > 0 else { return "" }
    let f = RelativeDateTimeFormatter()
    f.unitsStyle = .abbreviated
    return f.localizedString(for: Date(timeIntervalSince1970: ts), relativeTo: Date())
}

// MARK: - Now

private struct NowTab: View {
    @ObservedObject var appState: AppState

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if !appState.interruptedRuns.isEmpty { interrupted }
            if !appState.approvals.isEmpty {
                SectionLabel(text: "Waiting for you")
                ForEach(appState.approvals) { approval in
                    ApprovalCardView(approval: approval, remaining: 0) { decision, note in
                        appState.respond(to: approval, decision: decision, note: note)
                    }
                }
            }
            if let run = appState.activeRun ?? appState.runs.first {
                RunDetail(run: run, appState: appState)
            } else {
                VStack(spacing: 8) {
                    Image(systemName: "list.bullet.rectangle").font(.system(size: 22)).foregroundStyle(Theme.inkFaint)
                    Text("Nothing running").font(Theme.text(12.5, weight: .medium)).foregroundStyle(Theme.inkDim)
                    Text("Plans and progress show up here while Jugnu works.")
                        .font(Theme.text(11.5)).foregroundStyle(Theme.inkFaint).multilineTextAlignment(.center)
                }
                .frame(maxWidth: .infinity)
                .padding(.top, 40)
            }
            if !appState.backgroundRuns.isEmpty {
                SectionLabel(text: "In the background")
                ForEach(appState.backgroundRuns) { run in RunDetail(run: run, appState: appState, compact: true) }
            }
        }
    }

    private var interrupted: some View {
        VStack(alignment: .leading, spacing: 6) {
            SectionLabel(text: "Interrupted by a restart")
            ForEach(appState.interruptedRuns, id: \.self) { run in
                let id = run["run_id"]?.string ?? ""
                Card {
                    Text(run["goal"]?.string ?? "A task").font(Theme.text(12)).foregroundStyle(Theme.ink)
                    HStack {
                        Button("Resume") { appState.resumeInterrupted(id) }.buttonStyle(.borderless).foregroundStyle(Theme.clay)
                        Button("Dismiss") { appState.dismissInterrupted(id) }.buttonStyle(.borderless).foregroundStyle(Theme.inkFaint)
                    }.font(Theme.text(11.5, weight: .medium))
                }
            }
        }
    }
}

struct RunDetail: View {
    let run: RunActivity
    @ObservedObject var appState: AppState
    var compact = false
    @State private var expandedTask: String?

    var body: some View {
        Card {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(run.objective.isEmpty ? run.goal : run.objective)
                        .font(Theme.text(12.5, weight: .semibold)).foregroundStyle(Theme.ink)
                        .fixedSize(horizontal: false, vertical: true)
                    HStack(spacing: 6) {
                        Pill(text: run.state, tint: run.isActive ? Theme.clay : Theme.inkDim)
                        if !run.tasks.isEmpty { Pill(text: "\(run.completedTasks)/\(run.tasks.count) done") }
                        if run.origin != "user" { Pill(text: run.origin) }
                        if !run.session.isEmpty && run.session != "primary" { Pill(text: "Jugnu account") }
                    }
                }
                Spacer()
                if run.isActive {
                    Button(run.state == "paused" ? "Resume" : "Pause") {
                        run.state == "paused" ? appState.resumeRun(run.id) : appState.pauseRun(run.id)
                    }
                    .buttonStyle(.borderless).font(Theme.text(11)).foregroundStyle(Theme.inkDim)
                    Button("Stop") { appState.cancelRun(run.id) }
                        .buttonStyle(.borderless).font(Theme.text(11, weight: .semibold)).foregroundStyle(Theme.danger)
                }
            }
            if !compact && !run.assumptions.isEmpty {
                Text("Assuming: " + run.assumptions.joined(separator: "; "))
                    .font(Theme.text(10.5)).foregroundStyle(Theme.inkDim)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let last = run.progress.last, run.isActive {
                Text(last).font(Theme.text(10.5)).foregroundStyle(Theme.inkFaint)
            }
            ForEach(run.tasks) { task in taskRow(task) }
            let orphanTools = run.tools.filter { t in !run.tasks.contains { $0.id == t.taskId } }
            if !compact && !orphanTools.isEmpty {
                ForEach(orphanTools.suffix(8)) { tool in ToolRow(tool: tool) }
            }
        }
    }

    @ViewBuilder
    private func taskRow(_ task: PlanTask) -> some View {
        let tools = run.tools.filter { $0.taskId == task.id }
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .top, spacing: 7) {
                Image(systemName: task.state.symbol)
                    .font(.system(size: 11))
                    .foregroundStyle(stateColor(task.state))
                    .frame(width: 14)
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 5) {
                        Text(task.description.isEmpty ? "Task \(task.id)" : task.description)
                            .font(Theme.text(11.5)).foregroundStyle(Theme.ink)
                            .lineLimit(expandedTask == task.id ? nil : 2)
                        Spacer(minLength: 4)
                        Text(task.agent).font(Theme.mono(9.5)).foregroundStyle(Theme.inkFaint)
                    }
                    if !task.note.isEmpty && task.state != .done || (expandedTask == task.id && !task.note.isEmpty) {
                        Text(task.note).font(Theme.text(10.5))
                            .foregroundStyle(task.state == .failed ? Theme.danger.opacity(0.85) : Theme.inkDim)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            .contentShape(Rectangle())
            .onTapGesture { withAnimation(Theme.Motion.snappy) { expandedTask = expandedTask == task.id ? nil : task.id } }
            if expandedTask == task.id {
                ForEach(tools) { tool in ToolRow(tool: tool).padding(.leading, 21) }
                if tools.isEmpty {
                    Text("No tool calls yet").font(Theme.text(10)).foregroundStyle(Theme.inkFaint).padding(.leading, 21)
                }
            } else if !tools.isEmpty {
                Text("\(tools.count) tool call\(tools.count == 1 ? "" : "s") · tap to expand")
                    .font(Theme.text(9.5)).foregroundStyle(Theme.inkFaint).padding(.leading, 21)
            }
        }
    }
}

struct ToolRow: View {
    let tool: ToolActivity
    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 6) {
                Image(systemName: tool.ok == nil ? "ellipsis.circle" : (tool.ok! ? "checkmark" : "xmark"))
                    .font(.system(size: 9, weight: .bold))
                    .foregroundStyle(tool.ok == false ? Theme.danger : Theme.inkDim)
                Text(tool.tool).font(Theme.mono(10)).foregroundStyle(Theme.inkDim)
                if !tool.risk.isEmpty && tool.risk != "read" { Pill(text: tool.risk, tint: tool.risk == "high" ? Theme.danger : Theme.inkFaint) }
                if let e = tool.errorType { Pill(text: e, tint: Theme.danger) }
                Spacer()
                if let d = tool.duration { Text(String(format: "%.1fs", d)).font(Theme.mono(9)).foregroundStyle(Theme.inkFaint) }
            }
            Text(expanded ? (tool.preview.isEmpty ? tool.summary : tool.summary + "\n→ " + tool.preview) : tool.summary)
                .font(Theme.mono(9.5)).foregroundStyle(Theme.inkFaint)
                .lineLimit(expanded ? nil : 1)
                .textSelection(.enabled)
        }
        .contentShape(Rectangle())
        .onTapGesture { expanded.toggle() }
    }
}

// MARK: - Tasks (background runs + schedules)

private struct TasksTab: View {
    @ObservedObject var appState: AppState

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            SectionLabel(text: "Scheduled")
            if appState.schedules.isEmpty {
                Text("Nothing scheduled. Try “every weekday at 8am, prepare me for the day” or “remind me in 20 minutes to stretch”.")
                    .font(Theme.text(11)).foregroundStyle(Theme.inkFaint).fixedSize(horizontal: false, vertical: true)
            }
            ForEach(appState.schedules, id: \.self) { s in scheduleRow(s) }

            SectionLabel(text: "Recent tasks")
            ForEach(appState.runHistory, id: \.self) { run in runRow(run) }
        }
    }

    private func scheduleRow(_ s: JSONValue) -> some View {
        let id = s["id"]?.string ?? ""
        let enabled = s["enabled"]?.bool ?? false
        return Card {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(s["title"]?.string ?? "").font(Theme.text(12)).foregroundStyle(enabled ? Theme.ink : Theme.inkFaint)
                    HStack(spacing: 6) {
                        Pill(text: s["kind"]?.string == "notify" ? "timer" : "task")
                        if let next = s["next_run_text"]?.string { Pill(text: "next: \(next)") }
                        if let every = s["repeat_minutes"]?.double { Pill(text: "every \(Int(every)) min") }
                        if let last = s["last_status"]?.string { Pill(text: last) }
                    }
                }
                Spacer()
                Toggle("", isOn: Binding(get: { enabled }, set: { value in
                    appState.sendRequest("schedule_toggle", ["schedule_id": .string(id), "enabled": .bool(value)])
                })).toggleStyle(.switch).controlSize(.mini).labelsHidden()
                Button { appState.sendRequest("schedule_cancel", ["schedule_id": .string(id)]) } label: {
                    Image(systemName: "trash").font(.system(size: 10))
                }.buttonStyle(.plain).foregroundStyle(Theme.inkFaint).help("Delete schedule")
            }
        }
    }

    private func runRow(_ run: JSONValue) -> some View {
        let id = run["run_id"]?.string ?? ""
        let status = run["status"]?.string ?? ""
        return Card {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(run["objective"]?.string?.isEmpty == false ? run["objective"]!.string! : (run["goal"]?.string ?? ""))
                        .font(Theme.text(11.5)).foregroundStyle(Theme.ink).lineLimit(2)
                    HStack(spacing: 6) {
                        Pill(text: status, tint: ["failed", "partial"].contains(status) ? Theme.danger : Theme.inkDim)
                        if let total = run["task_count"]?.int, total > 0 {
                            Pill(text: "\(run["tasks_done"]?.int ?? 0)/\(total) tasks")
                        }
                        if run["background"]?.bool == true { Pill(text: "background") }
                        Text(relative(run["updated_at"]?.double)).font(Theme.text(10)).foregroundStyle(Theme.inkFaint)
                    }
                    if let q = run["question"]?.string, status == "waiting_user" {
                        Text("Waiting for: \(q)").font(Theme.text(10.5)).foregroundStyle(Theme.clay)
                    }
                }
                Spacer()
                if ["interrupted", "partial", "failed"].contains(status) {
                    Button("Resume") { appState.sendRequest("run_resume", ["run_id": .string(id)]) }
                        .buttonStyle(.borderless).font(Theme.text(11)).foregroundStyle(Theme.clay)
                }
            }
        }
    }
}

// MARK: - Memory

private struct MemoryTab: View {
    @ObservedObject var appState: AppState
    @State private var kind = ""
    @State private var query = ""
    @State private var editingId: String?
    @State private var editText = ""
    @State private var newMemory = ""

    private let kinds = [("", "All"), ("user", "About you"), ("semantic", "Knowledge"), ("episodic", "Past work"),
                         ("strategy", "Strategies"), ("error", "Mistakes")]

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Edit anything that's wrong; your edits count as confirmed.")
                .font(Theme.text(11)).foregroundStyle(Theme.inkFaint).fixedSize(horizontal: false, vertical: true)
            Picker("", selection: $kind) {
                ForEach(kinds, id: \.0) { k in Text(label(for: k)).tag(k.0) }
            }.pickerStyle(.menu).labelsHidden().onChange(of: kind) { _, _ in reload() }
            HStack {
                TextField("Search memories", text: $query).textFieldStyle(.roundedBorder).onSubmit(reload)
                Button("Search", action: reload).buttonStyle(.borderless).foregroundStyle(Theme.clay)
            }
            HStack {
                TextField("Tell Jugnu something to remember about you", text: $newMemory).textFieldStyle(.roundedBorder)
                    .onSubmit(add)
                Button("Add", action: add).buttonStyle(.borderless).foregroundStyle(Theme.clay).disabled(newMemory.isEmpty)
            }
            if appState.memories.isEmpty {
                Text("Nothing here yet.").font(Theme.text(11.5)).foregroundStyle(Theme.inkFaint)
            }
            ForEach(appState.memories, id: \.self) { m in row(m) }
            if !appState.toolStats.isEmpty {
                SectionLabel(text: "Tool reliability")
                ForEach(appState.toolStats.prefix(15), id: \.self) { t in
                    HStack {
                        Text(t["tool"]?.string ?? "").font(Theme.mono(10)).foregroundStyle(Theme.inkDim)
                        Spacer()
                        if let r = t["reliability"]?.double {
                            Text("\(Int(r * 100))%").font(Theme.mono(10))
                                .foregroundStyle(r < 0.6 ? Theme.danger : Theme.inkDim)
                        }
                        Text("\((t["successes"]?.int ?? 0) + (t["failures"]?.int ?? 0)) uses")
                            .font(Theme.mono(9.5)).foregroundStyle(Theme.inkFaint)
                    }
                }
            }
        }
    }

    private func label(for k: (String, String)) -> String {
        guard !k.0.isEmpty, let n = appState.memoryCounts[k.0]?["active"]?.int else { return k.1 }
        return "\(k.1) (\(n))"
    }

    private func reload() {
        var extra: [String: JSONValue] = ["query": .string(query)]
        if !kind.isEmpty { extra["kind"] = .string(kind) }
        appState.sendRequest("memory_list", extra)
    }

    private func add() {
        guard !newMemory.trimmingCharacters(in: .whitespaces).isEmpty else { return }
        appState.sendRequest("memory_add", ["content": .string(newMemory), "kind": .string("user"),
                                            "list_kind": .string(kind)])
        newMemory = ""
    }

    @ViewBuilder
    private func row(_ m: JSONValue) -> some View {
        let id = m["id"]?.string ?? ""
        Card {
            if editingId == id {
                TextField("", text: $editText, axis: .vertical).textFieldStyle(.roundedBorder)
                HStack {
                    Button("Save") {
                        appState.sendRequest("memory_update", ["memory_id": .string(id),
                                                               "fields": .object(["content": .string(editText)]),
                                                               "list_kind": .string(kind)])
                        editingId = nil
                    }.foregroundStyle(Theme.clay)
                    Button("Cancel") { editingId = nil }.foregroundStyle(Theme.inkFaint)
                }.buttonStyle(.borderless).font(Theme.text(11))
            } else {
                Text(m["content"]?.string ?? "").font(Theme.text(11.5)).foregroundStyle(Theme.ink)
                    .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                HStack(spacing: 6) {
                    Pill(text: m["kind"]?.string ?? "")
                    Pill(text: m["source"]?.string ?? "", tint: m["unconfirmed"]?.bool == true ? Theme.clay : Theme.inkDim)
                    if let c = m["confidence"]?.double { Pill(text: "\(Int(c * 100))% sure") }
                    if let scope = m["scope"]?.string, scope != "global" { Pill(text: scope) }
                    Text(relative(m["updated_at"]?.double)).font(Theme.text(9.5)).foregroundStyle(Theme.inkFaint)
                    Spacer()
                    Button { editingId = id; editText = m["content"]?.string ?? "" } label: {
                        Image(systemName: "pencil").font(.system(size: 10))
                    }.buttonStyle(.plain).foregroundStyle(Theme.inkDim).help("Edit")
                    Button { appState.sendRequest("memory_delete", ["memory_id": .string(id), "list_kind": .string(kind)]) } label: {
                        Image(systemName: "trash").font(.system(size: 10))
                    }.buttonStyle(.plain).foregroundStyle(Theme.inkFaint).help("Delete")
                }
            }
        }
    }
}

// MARK: - Skills

private struct SkillsTab: View {
    @ObservedObject var appState: AppState
    @State private var expanded: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Workflows learned from tasks that worked. Suggestions do nothing until you accept them.")
                .font(Theme.text(11)).foregroundStyle(Theme.inkFaint).fixedSize(horizontal: false, vertical: true)
            if appState.skillLibrary.isEmpty {
                Text("No skills yet -- they're suggested after multi-step tasks succeed.")
                    .font(Theme.text(11.5)).foregroundStyle(Theme.inkFaint)
            }
            ForEach(appState.skillLibrary, id: \.self) { s in row(s) }
            if !appState.proposals.isEmpty {
                SectionLabel(text: "Suggested changes to Jugnu's own code (never applied automatically)")
                ForEach(appState.proposals, id: \.self) { p in
                    Card {
                        Text(p["summary"]?.string ?? "").font(Theme.text(11)).foregroundStyle(Theme.inkDim)
                            .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                        Text(relative(p["ts"]?.double)).font(Theme.text(9.5)).foregroundStyle(Theme.inkFaint)
                    }
                }
            }
        }
    }

    @ViewBuilder
    private func row(_ s: JSONValue) -> some View {
        let id = s["id"]?.string ?? ""
        let status = s["status"]?.string ?? ""
        Card {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(s["name"]?.string ?? "").font(Theme.text(12.5, weight: .semibold)).foregroundStyle(Theme.ink)
                    Text(s["purpose"]?.string ?? "").font(Theme.text(11)).foregroundStyle(Theme.inkDim)
                        .fixedSize(horizontal: false, vertical: true)
                    HStack(spacing: 6) {
                        Pill(text: status, tint: status == "suggested" ? Theme.clay : Theme.inkDim)
                        Pill(text: "v\(s["version"]?.int ?? 1)")
                        Pill(text: "risk: \(s["risk"]?.string ?? "")", tint: s["risk"]?.string == "high" ? Theme.danger : Theme.inkDim)
                        if let r = s["success_rate"]?.double {
                            Pill(text: "\(Int(r * 100))% of \((s["successes"]?.int ?? 0) + (s["failures"]?.int ?? 0))")
                        }
                        if s["needs_review"]?.bool == true { Pill(text: "needs review", tint: Theme.danger) }
                    }
                }
                Spacer()
            }
            HStack(spacing: 10) {
                if status == "suggested" {
                    Button("Accept") { appState.sendRequest("skill_accept", ["skill_id": .string(id)]) }.foregroundStyle(Theme.clay)
                } else if status == "active" {
                    Button("Disable") { appState.sendRequest("skill_disable", ["skill_id": .string(id)]) }.foregroundStyle(Theme.inkDim)
                } else {
                    Button("Enable") { appState.sendRequest("skill_accept", ["skill_id": .string(id)]) }.foregroundStyle(Theme.clay)
                }
                Button(expanded == id ? "Hide steps" : "Steps") { expanded = expanded == id ? nil : id }.foregroundStyle(Theme.inkDim)
                Spacer()
                Button("Delete") { appState.sendRequest("skill_delete", ["skill_id": .string(id)]) }.foregroundStyle(Theme.danger)
            }
            .buttonStyle(.borderless).font(Theme.text(11))
            if expanded == id {
                ForEach(s["steps"]?.array ?? [], id: \.self) { step in
                    Text("\(step["id"]?.string ?? "·"). [\(step["agent"]?.string ?? "")] \(step["instruction"]?.string ?? "")")
                        .font(Theme.text(10.5)).foregroundStyle(Theme.inkDim).fixedSize(horizontal: false, vertical: true)
                }
                if let v = s["verification"]?.string, !v.isEmpty {
                    Text("Done when: \(v)").font(Theme.text(10.5)).foregroundStyle(Theme.inkFaint)
                }
            }
        }
    }
}

// MARK: - Log

private struct LogTab: View {
    @ObservedObject var appState: AppState
    @State private var filter = ""
    @State private var expanded: Set<Int> = []

    private var events: [JSONValue] {
        guard !filter.isEmpty else { return appState.auditEvents }
        return appState.auditEvents.filter { $0["kind"]?.string == filter }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Picker("", selection: $filter) {
                Text("Everything").tag("")
                Text("Tool calls").tag("tool_result")
                Text("Approvals").tag("approval_decision")
                Text("Errors & recovery").tag("recovery")
                Text("Verification").tag("verification")
                Text("Plans").tag("plan")
                Text("Memory changes").tag("memory_change")
                Text("Security").tag("injection_warning")
            }.pickerStyle(.menu).labelsHidden()
            ForEach(Array(events.enumerated()), id: \.offset) { index, e in
                let id = e["id"]?.int ?? index
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 6) {
                        Text(e["kind"]?.string ?? "").font(Theme.mono(9.5)).foregroundStyle(Theme.clay)
                        if let tool = e["tool"]?.string, !tool.isEmpty { Text(tool).font(Theme.mono(9.5)).foregroundStyle(Theme.inkDim) }
                        if let status = e["status"]?.string, !status.isEmpty {
                            Pill(text: status, tint: ["ok", "passed", "allow", "approve"].contains(status) ? Theme.inkDim : Theme.clay)
                        }
                        Spacer()
                        Text(relative(e["ts"]?.double)).font(Theme.text(9.5)).foregroundStyle(Theme.inkFaint)
                    }
                    Text(e["summary"]?.string ?? "").font(Theme.text(10.5)).foregroundStyle(Theme.inkDim)
                        .lineLimit(expanded.contains(id) ? nil : 2).textSelection(.enabled)
                    if expanded.contains(id), let detail = e["detail"], !detail.isNull {
                        Text(detail.compactText).font(Theme.mono(9)).foregroundStyle(Theme.inkFaint).textSelection(.enabled)
                    }
                }
                .padding(.vertical, 4)
                .contentShape(Rectangle())
                .onTapGesture { if expanded.contains(id) { expanded.remove(id) } else { expanded.insert(id) } }
                Divider().overlay(Theme.border)
            }
        }
    }
}
