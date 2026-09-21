import SwiftUI
import AppKit

/// Autonomy mode, capability switches, allowed folders and macOS permission
/// status. Unlike the rest of Settings, these apply immediately -- the
/// backend reads permissions.json on every tool call -- so there's no
/// restart involved.
struct AutonomySettingsSection: View {
    @ObservedObject var appState: AppState
    @State private var writeRoots = ""
    @State private var projectRoots = ""
    @State private var readRoots = ""
    @State private var loadedFolders = false

    private var perms: JSONValue { appState.permissions }
    private var mode: String { perms["autonomy_mode"]?.string ?? "balanced" }

    private static let modeHelp: [String: String] = [
        "manual": "Previews every action before it runs, except reading.",
        "balanced": "Does ordinary, reversible work on its own -- reading, drafting, creating files, organizing into the Trash. Asks before anything it can't undo.",
        "autonomous": "Completes the tasks you start without check-ins, within the permissions below.",
    ]

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: 8) {
                Text("Autonomy").font(Theme.text(13, weight: .semibold)).foregroundStyle(Theme.ink)
                Picker("", selection: Binding(get: { mode }, set: { new in
                    appState.updatePermissions(["autonomy_mode": .string(new)])
                })) {
                    Text("Manual").tag("manual")
                    Text("Balanced").tag("balanced")
                    Text("Autonomous").tag("autonomous")
                }
                .pickerStyle(.segmented).labelsHidden()
                Text(Self.modeHelp[mode] ?? "")
                    .font(Theme.text(11)).foregroundStyle(Theme.inkDim).fixedSize(horizontal: false, vertical: true)
                Label("In every mode, sending messages or email, permanent deletion, installing software, publishing, purchases and security changes always ask first.",
                      systemImage: "lock.shield")
                    .font(Theme.text(10.5)).foregroundStyle(Theme.inkFaint).fixedSize(horizontal: false, vertical: true)
            }

            capabilities

            folders

            osPermissions
        }
        .padding(Theme.Spacing.lg)
        .background(RoundedRectangle(cornerRadius: Theme.radiusMedium).fill(Theme.panel))
        .onAppear {
            appState.refreshPermissions()
            appState.sendRequest("os_permissions")
        }
        .onChange(of: appState.permissions) { _, _ in loadFolders() }
    }

    // MARK: Capabilities

    private var capabilities: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("What Jugnu may use").font(Theme.text(12, weight: .semibold)).foregroundStyle(Theme.ink)
            let info = perms["capability_info"]?.object ?? [:]
            let caps = perms["capabilities"]?.object ?? [:]
            LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], alignment: .leading, spacing: 6) {
                ForEach(info.keys.sorted(), id: \.self) { key in
                    Toggle(isOn: Binding(get: { caps[key]?.bool ?? true }, set: { value in
                        appState.updatePermissions(["capabilities": .object([key: .bool(value)])])
                    })) {
                        Text(info[key]?["label"]?.string ?? key).font(Theme.text(11.5))
                    }
                    .toggleStyle(.checkbox)
                    .help(info[key]?["description"]?.string ?? "")
                }
            }
        }
    }

    // MARK: Folders

    private var folders: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Folders").font(Theme.text(12, weight: .semibold)).foregroundStyle(Theme.ink)
            Text("One per line. Outside these, Jugnu asks first. Protected places (keychains, ~/.ssh, system folders) always ask. The Jugnu account's own home is always its workspace.")
                .font(Theme.text(10.5)).foregroundStyle(Theme.inkFaint).fixedSize(horizontal: false, vertical: true)
            folderEditor("Can change files in", text: $writeRoots)
            folderEditor("Project folders (builds, tests, git)", text: $projectRoots)
            folderEditor("Can read", text: $readRoots)
            HStack {
                Spacer()
                Button("Save folders") {
                    appState.updatePermissions([
                        "write_roots": lines(writeRoots), "project_roots": lines(projectRoots), "read_roots": lines(readRoots),
                    ])
                }
                .buttonStyle(.borderless).foregroundStyle(Theme.clay).font(Theme.text(11.5, weight: .semibold))
            }
        }
    }

    private func folderEditor(_ title: String, text: Binding<String>) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(title).font(Theme.text(10.5)).foregroundStyle(Theme.inkDim)
            TextEditor(text: text)
                .font(Theme.mono(10.5))
                .frame(height: 58)
                .scrollContentBackground(.hidden)
                .padding(4)
                .background(RoundedRectangle(cornerRadius: 6).fill(Color.black.opacity(0.22)))
        }
    }

    private func lines(_ text: String) -> JSONValue {
        .array(text.split(whereSeparator: \.isNewline).map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }.map { .string($0) })
    }

    private func loadFolders() {
        guard !loadedFolders, !perms.isNull else { return }
        writeRoots = (perms["write_roots"]?.array ?? []).compactMap(\.string).joined(separator: "\n")
        projectRoots = (perms["project_roots"]?.array ?? []).compactMap(\.string).joined(separator: "\n")
        readRoots = (perms["read_roots"]?.array ?? []).compactMap(\.string).joined(separator: "\n")
        loadedFolders = true
    }

    // MARK: macOS permissions

    private var osPermissions: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("macOS permissions").font(Theme.text(12, weight: .semibold)).foregroundStyle(Theme.ink)
                Spacer()
                Button("Check again") { appState.sendRequest("os_permissions") }
                    .buttonStyle(.borderless).foregroundStyle(Theme.clay).font(Theme.text(11))
            }
            let sessions = appState.osPermissions.object
            if sessions.isEmpty {
                Text("Checking…").font(Theme.text(11)).foregroundStyle(Theme.inkFaint)
            }
            ForEach(sessions.keys.sorted(), id: \.self) { name in
                sessionBlock(name, sessions[name] ?? .null)
            }
        }
    }

    @ViewBuilder
    private func sessionBlock(_ name: String, _ data: JSONValue) -> some View {
        let title = name == "agent" ? "Jugnu account" : "Your account"
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(Theme.text(11, weight: .semibold)).foregroundStyle(Theme.inkDim)
            if data["ok"]?.bool != true {
                Text(data["error"]?.string == "not running" ? "Not running (log into it to use it)." : (data["error"]?.string ?? "Unavailable"))
                    .font(Theme.text(10.5)).foregroundStyle(Theme.inkFaint)
            } else {
                let entries = data["permissions"]?.object ?? [:]
                ForEach(entries.keys.sorted(), id: \.self) { key in
                    let entry = entries[key] ?? .null
                    let status = entry["status"]?.string ?? "unknown"
                    HStack(spacing: 6) {
                        Image(systemName: status == "granted" || status == "granted_or_unknown" ? "checkmark.circle.fill"
                              : (status == "unknown" ? "questionmark.circle" : "exclamationmark.triangle.fill"))
                            .font(.system(size: 10))
                            .foregroundStyle(status.hasPrefix("granted") ? Color.green.opacity(0.8)
                                             : (status == "unknown" ? Theme.inkFaint : Theme.clay))
                        Text(key.replacingOccurrences(of: "_", with: " ").capitalized).font(Theme.text(11))
                        Text(status.replacingOccurrences(of: "_", with: " ")).font(Theme.text(10)).foregroundStyle(Theme.inkFaint)
                        Spacer()
                        if !status.hasPrefix("granted"), status != "unknown", let url = Self.settingsURL[key] {
                            Button("Open") { NSWorkspace.shared.open(url) }
                                .buttonStyle(.borderless).font(Theme.text(10.5)).foregroundStyle(Theme.clay)
                        }
                    }
                    .help(entry["needed_for"]?.string ?? "")
                }
                if name == "agent" {
                    Text("Grants in the Jugnu account have to be given while logged in as that user.")
                        .font(Theme.text(10)).foregroundStyle(Theme.inkFaint)
                }
            }
        }
        .padding(.vertical, 2)
    }

    private static let settingsURL: [String: URL] = {
        let base = "x-apple.systempreferences:com.apple.preference.security?"
        let panes = ["accessibility": "Privacy_Accessibility", "screen_recording": "Privacy_ScreenCapture",
                     "calendar": "Privacy_Calendars", "reminders": "Privacy_Reminders", "contacts": "Privacy_Contacts",
                     "full_disk_access": "Privacy_AllFiles", "automation": "Privacy_Automation"]
        var urls = panes.compactMapValues { URL(string: base + $0) }
        urls["notifications"] = URL(string: "x-apple.systempreferences:com.apple.preference.notifications")
        return urls
    }()
}
