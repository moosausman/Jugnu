import SwiftUI
import AppKit

/// Autonomy mode, what Jugnu may use, where it may work, and the macOS
/// permissions behind it. Applies immediately -- the backend reads
/// permissions.json on every tool call.
struct AutonomyPane: View {
    @ObservedObject var appState: AppState
    @State private var writeRoots = ""
    @State private var projectRoots = ""
    @State private var readRoots = ""
    @State private var loadedFolders = false
    @State private var showFolders = false

    private var perms: JSONValue { appState.permissions }
    private var mode: String { perms["autonomy_mode"]?.string ?? "balanced" }

    private static let modeHelp: [String: String] = [
        "manual": "Shows you every action first, except reading.",
        "balanced": "Does reversible work on its own. Asks before anything it can't undo.",
        "autonomous": "Finishes the tasks you start without checking in.",
    ]

    private var foldersChanged: Bool {
        text(for: "write_roots") != writeRoots || text(for: "project_roots") != projectRoots
            || text(for: "read_roots") != readRoots
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            SettingsGroup("Mode") {
                Picker("", selection: Binding(get: { mode }, set: { new in
                    appState.updatePermissions(["autonomy_mode": .string(new)])
                })) {
                    Text("Manual").tag("manual")
                    Text("Balanced").tag("balanced")
                    Text("Autonomous").tag("autonomous")
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .padding(.top, 4)
                Text(Self.modeHelp[mode] ?? "")
                    .font(Theme.text(11.5))
                    .foregroundStyle(Theme.inkDim)
                    .padding(.vertical, 6)
            }
            Label("Sending, deleting for good, installing, buying and security changes always ask. In the "
                  + "Jugnu account, only sending, publishing, buying and Mac-wide changes ask.",
                  systemImage: "lock.fill")
                .font(Theme.text(11))
                .foregroundStyle(Theme.inkFaint)
                .padding(.top, -12)
                .padding(.leading, 2)

            capabilities
            folders
            osPermissions
        }
        .onAppear {
            appState.refreshPermissions()
            appState.sendRequest("os_permissions")
            loadFolders()
        }
        .onChange(of: appState.permissions) { _, _ in loadFolders() }
    }

    // MARK: Capabilities

    private var capabilities: some View {
        let info = perms["capability_info"]?.object ?? [:]
        let caps = perms["capabilities"]?.object ?? [:]
        return SettingsGroup("Jugnu may use") {
            if info.isEmpty {
                Text("Loading…").font(Theme.text(12)).foregroundStyle(Theme.inkFaint).padding(.vertical, 4)
            }
            LazyVGrid(columns: [GridItem(.flexible(), alignment: .leading), GridItem(.flexible(), alignment: .leading)],
                      alignment: .leading, spacing: 8) {
                ForEach(info.keys.sorted(), id: \.self) { key in
                    Toggle(isOn: Binding(get: { caps[key]?.bool ?? true }, set: { value in
                        appState.updatePermissions(["capabilities": .object([key: .bool(value)])])
                    })) {
                        Text(info[key]?["label"]?.string ?? key).font(Theme.text(12))
                    }
                    .toggleStyle(.checkbox)
                    .help(info[key]?["description"]?.string ?? "")
                }
            }
            .padding(.vertical, 5)
        }
    }

    // MARK: Folders

    private var folders: some View {
        VStack(alignment: .leading, spacing: 7) {
            SettingsGroup("Folders") {
                DisclosureRow("Where Jugnu can work without asking", isOn: $showFolders)
                if showFolders {
                    RowDivider()
                    folderEditor("Can change", text: $writeRoots)
                    RowDivider()
                    folderEditor("Projects", text: $projectRoots)
                    RowDivider()
                    folderEditor("Can read", text: $readRoots)
                    HStack {
                        Spacer()
                        Button("Save") {
                            appState.updatePermissions([
                                "write_roots": lines(writeRoots), "project_roots": lines(projectRoots),
                                "read_roots": lines(readRoots),
                            ])
                        }
                        .buttonStyle(JugnuButtonStyle(kind: .primary, compact: true))
                        .disabled(!foldersChanged)
                        .opacity(foldersChanged ? 1 : 0.5)
                    }
                    .padding(.vertical, 6)
                }
            }
            if showFolders {
                Footnote("One folder per line. Anywhere else, and protected places like ~/.ssh, it asks first.")
            }
        }
    }

    private func folderEditor(_ title: String, text: Binding<String>) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Text(title).font(Theme.text(12.5)).frame(width: 92, alignment: .leading).padding(.top, 4)
            TextEditor(text: text)
                .font(Theme.mono(11))
                .scrollContentBackground(.hidden)
                .frame(height: 52)
                .padding(4)
                .background(RoundedRectangle(cornerRadius: 6).fill(Theme.well))
                .overlay(RoundedRectangle(cornerRadius: 6).stroke(Theme.border, lineWidth: 1))
        }
    }

    private func text(for key: String) -> String {
        (perms[key]?.array ?? []).compactMap(\.string).joined(separator: "\n")
    }

    private func lines(_ text: String) -> JSONValue {
        .array(text.split(whereSeparator: \.isNewline).map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }.map { .string($0) })
    }

    private func loadFolders() {
        guard !loadedFolders, !perms.isNull else { return }
        writeRoots = text(for: "write_roots")
        projectRoots = text(for: "project_roots")
        readRoots = text(for: "read_roots")
        loadedFolders = true
    }

    // MARK: macOS permissions

    private var osPermissions: some View {
        let sessions = appState.osPermissions.object
        return VStack(alignment: .leading, spacing: 7) {
            HStack {
                Text("macOS permissions")
                    .font(Theme.text(11.5, weight: .semibold))
                    .foregroundStyle(Theme.inkFaint)
                Spacer()
                Button("Check again") { appState.sendRequest("os_permissions") }
                    .buttonStyle(.plain)
                    .font(Theme.text(11.5))
                    .foregroundStyle(Theme.clay)
            }
            .padding(.horizontal, 2)
            if sessions.isEmpty {
                SettingsGroup(nil) {
                    Text("Checking…").font(Theme.text(12)).foregroundStyle(Theme.inkFaint).padding(.vertical, 4)
                }
            }
            ForEach(sessions.keys.sorted(), id: \.self) { name in
                SettingsGroup(nil) { sessionRows(name, sessions[name] ?? .null) }
            }
        }
    }

    @ViewBuilder
    private func sessionRows(_ name: String, _ data: JSONValue) -> some View {
        Text(name == "agent" ? "Jugnu account" : "Your account")
            .font(Theme.text(12, weight: .semibold))
            .foregroundStyle(Theme.inkDim)
            .padding(.vertical, 4)
        if data["ok"]?.bool != true {
            Text(data["error"]?.string == "not running" ? "Not running. Log in to that account to use it."
                 : (data["error"]?.string ?? "Unavailable"))
                .font(Theme.text(11.5))
                .foregroundStyle(Theme.inkFaint)
                .padding(.bottom, 4)
        } else {
            let entries = data["permissions"]?.object ?? [:]
            ForEach(entries.keys.sorted(), id: \.self) { key in
                let status = entries[key]?["status"]?.string ?? "unknown"
                let granted = status.hasPrefix("granted")
                HStack(spacing: 8) {
                    Image(systemName: granted ? "checkmark.circle.fill"
                          : (status == "unknown" ? "questionmark.circle" : "exclamationmark.circle.fill"))
                        .font(.system(size: 11))
                        .foregroundStyle(granted ? Theme.success : (status == "unknown" ? Theme.inkFaint : Theme.clay))
                    Text(key.replacingOccurrences(of: "_", with: " ").capitalized).font(Theme.text(12))
                    Spacer()
                    if !granted, status != "unknown", let url = Self.settingsURL[key] {
                        Button("Open") { NSWorkspace.shared.open(url) }
                            .buttonStyle(JugnuButtonStyle(kind: .secondary, compact: true))
                    } else if !granted {
                        Text(status.replacingOccurrences(of: "_", with: " "))
                            .font(Theme.text(11))
                            .foregroundStyle(Theme.inkFaint)
                    }
                }
                .padding(.vertical, 3)
                .help(entries[key]?["needed_for"]?.string ?? "")
            }
            if name == "agent" {
                Text("To grant these, log into the Jugnu account and open “Set Up Jugnu Permissions” in /Users/Shared.")
                    .font(Theme.text(11))
                    .foregroundStyle(Theme.inkFaint)
                    .padding(.vertical, 4)
            }
        }
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
