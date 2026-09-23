import SwiftUI

/// The message field. Grows to a few lines, sends on Return (Option-Return
/// for a new line), and turns its send button into Stop while Jugnu works.
/// You can keep typing the next message while it does.
struct InputBarView: View {
    @ObservedObject var appState: AppState
    @State private var text = ""
    @FocusState private var focused: Bool
    @StateObject private var speech = SpeechInput()

    private var placeholder: String {
        if speech.isListening { return "Listening… let go of ⌥ to send" }
        if appState.targetAccount == "agent" { return "Message Jugnu · runs on Jugnu" }
        return SettingsStore.shared.holdOptionToTalk ? "Message Jugnu, or hold ⌥ to talk" : "Message Jugnu"
    }

    private var canSend: Bool {
        !appState.thinking && appState.statusLabel != "starting"
            && !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    var body: some View {
        VStack(spacing: 6) {
            if let attachment = appState.attachment {
                AttachmentCard(attachment: attachment, disabled: appState.thinking) { instruction in
                    appState.send(instruction)
                } onRemove: {
                    appState.attachment = nil
                }
                .padding(.horizontal, 12)
            }
            field
        }
        .onReceive(NotificationCenter.default.publisher(for: .jugnuStartDictation)) { _ in
            if !speech.isListening {
                text = ""
                speech.start()
            }
        }
        .onReceive(NotificationCenter.default.publisher(for: .jugnuStopDictationAndSend)) { _ in
            // Push-to-talk released: stop, give the recognizer a moment to
            // settle its last words, then send what was said.
            speech.stop()
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.45) { send() }
        }
    }

    private var field: some View {
        HStack(alignment: .bottom, spacing: 6) {
            AccountChip(appState: appState)
                .padding(.bottom, 3)
            TextField(placeholder, text: $text, axis: .vertical)
                .textFieldStyle(.plain)
                .font(Theme.text(13.5))
                .foregroundStyle(Theme.ink)
                .lineLimit(1...6)
                .focused($focused)
                .onSubmit(send)
                .padding(.vertical, 5)
                .padding(.leading, 4)

            if !appState.thinking && text.isEmpty || speech.isListening {
                RoundButton(symbol: speech.isListening ? "waveform" : "mic",
                            fill: speech.isListening ? Theme.danger.opacity(0.18) : .clear,
                            tint: speech.isListening ? Theme.danger : Theme.inkFaint,
                            help: speech.isListening ? "Stop dictation" : "Dictate") {
                    speech.toggle()
                }
            }

            if appState.thinking {
                RoundButton(symbol: "stop.fill", fill: Color.white.opacity(0.12), tint: Theme.ink,
                            help: "Stop (⌘.)", symbolSize: 9) {
                    appState.cancelRun()
                }
                .keyboardShortcut(".", modifiers: .command)
            } else {
                RoundButton(symbol: "arrow.up", fill: canSend ? Theme.clay : Color.white.opacity(0.07),
                            tint: canSend ? Color(hex: 0x1A120C) : Theme.inkFaint, help: "Send (Return)",
                            weight: .semibold) {
                    send()
                }
                .disabled(!canSend)
            }
        }
        .padding(.leading, 10)
        .padding(.trailing, 6)
        .padding(.vertical, 6)
        .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(Theme.panel))
        .overlay(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .stroke(appState.targetAccount == "agent" ? Theme.clay.opacity(0.55)
                        : (focused ? Theme.clay.opacity(0.45) : Theme.borderStrong), lineWidth: 1)
        )
        .animation(Theme.Motion.snappy, value: focused)
        .padding(.horizontal, 12)
        .padding(.bottom, 12)
        .padding(.top, 4)
        .onAppear { focused = true }
        .onReceive(NotificationCenter.default.publisher(for: .jugnuFocusInput)) { _ in focused = true }
        .onChange(of: speech.transcript) { _, words in
            if speech.isListening || !words.isEmpty { text = words }
        }
        .onChange(of: speech.error) { _, message in
            if let message { appState.errorMessage = message }
        }
    }

    private func send() {
        guard canSend else { return }
        if speech.isListening { speech.stop() }
        appState.send(text)
        text = ""
    }
}

extension Notification.Name {
    static let jugnuFocusInput = Notification.Name("jugnuFocusInput")
}

/// Text selected in another app, attached to the next message, with
/// one-click actions. Typing an instruction and pressing Return works too.
private struct AttachmentCard: View {
    let attachment: AppState.Attachment
    let disabled: Bool
    let onAction: (String) -> Void
    let onRemove: () -> Void

    private static let actions: [(String, String)] = [
        ("Summarize", "Summarize this."),
        ("Explain", "Explain this simply."),
        ("Translate", "Translate this into English (or, if it's already English, into the language I usually use)."),
        ("Reply", "Draft a reply to this, in my voice. Just the reply."),
        ("To-do", "Turn this into reminders."),
    ]

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(spacing: 5) {
                Image(systemName: "text.quote").font(.system(size: 10))
                Text(attachment.app.isEmpty ? "Selected text" : "From \(attachment.app)")
                    .font(Theme.text(11, weight: .medium))
                Text("· \(attachment.text.count) characters").font(Theme.text(11)).foregroundStyle(Theme.inkFaint)
                Spacer()
                IconButton(symbol: "xmark", size: 9, help: "Remove", tint: Theme.inkFaint, action: onRemove)
                    .padding(.vertical, -6)
            }
            .foregroundStyle(Theme.inkDim)
            Text(attachment.text)
                .font(Theme.text(12))
                .foregroundStyle(Theme.ink.opacity(0.8))
                .lineLimit(3)
                .frame(maxWidth: .infinity, alignment: .leading)
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 5) {
                    ForEach(Self.actions, id: \.0) { label, instruction in
                        Button(label) { onAction(instruction) }
                            .buttonStyle(JugnuButtonStyle(kind: .secondary, compact: true))
                            .disabled(disabled)
                    }
                }
            }
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Theme.panel))
        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).stroke(Theme.clay.opacity(0.3), lineWidth: 1))
    }
}

/// Where the request runs: your account, the Jugnu account, or Auto.
///
/// It used to be a grey chip saying "Auto" that nobody would read as a
/// switch. It now names the account, carries its own icon, and turns clay
/// when work is going somewhere other than here -- because "which machine
/// did that happen on" is the one thing you can't afford to get wrong.
private struct AccountChip: View {
    @ObservedObject var appState: AppState
    @ObservedObject private var settings = SettingsStore.shared
    @State private var hovering = false

    private var elsewhere: Bool {
        appState.targetAccount == "agent"
            || (appState.targetAccount == "auto" && settings.useAgentAccountByDefault)
    }

    private var label: String {
        switch appState.targetAccount {
        case "primary": return "You"
        case "agent": return "Jugnu"
        default: return settings.useAgentAccountByDefault ? "Jugnu" : "You"
        }
    }

    private var symbol: String {
        elsewhere ? "desktopcomputer" : "person.crop.circle"
    }

    var body: some View {
        Menu {
            Picker("Run on", selection: $appState.targetAccount) {
                Label("This account (you)", systemImage: "person.crop.circle").tag("primary")
                Label("The Jugnu account", systemImage: "desktopcomputer").tag("agent")
                Divider()
                Text("Auto — \(settings.useAgentAccountByDefault ? "the Jugnu account" : "this account")")
                    .tag("auto")
            }
            .pickerStyle(.inline)
        } label: {
            HStack(spacing: 5) {
                Image(systemName: symbol).font(.system(size: 11, weight: .medium))
                Text(label).font(Theme.text(11.5, weight: .medium))
                if appState.targetAccount == "auto" {
                    Text("auto").font(Theme.text(9.5)).foregroundStyle(Theme.inkFaint)
                }
                Image(systemName: "chevron.up.chevron.down")
                    .font(.system(size: 7, weight: .semibold))
                    .foregroundStyle(Theme.inkFaint)
            }
            .foregroundStyle(elsewhere ? Theme.clay : (hovering ? Theme.ink : Theme.inkDim))
            .padding(.horizontal, 8)
            .padding(.vertical, 4)
            .background(Capsule().fill(elsewhere ? Theme.clay.opacity(0.16)
                                       : Color.white.opacity(hovering ? 0.09 : 0.05)))
            .overlay(Capsule().stroke(elsewhere ? Theme.clay.opacity(0.45) : Color.clear, lineWidth: 1))
            .contentShape(Capsule())
        }
        .menuStyle(.borderlessButton)
        .menuIndicator(.hidden)
        .fixedSize()
        .onHover { hovering = $0 }
        .animation(Theme.Motion.snappy, value: elsewhere)
        .help(elsewhere ? "Runs on the Jugnu account — watch it with ⌘⇧D"
              : "Runs on this account")
    }
}

private struct RoundButton: View {
    let symbol: String
    var fill: Color
    var tint: Color
    var help: String
    var symbolSize: CGFloat = 11.5
    var weight: Font.Weight = .medium
    let action: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: symbolSize, weight: weight))
                .foregroundStyle(tint)
                .frame(width: 26, height: 26)
                .background(Circle().fill(fill))
                .background(Circle().fill(Color.white.opacity(hovering ? 0.06 : 0)))
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .help(help)
    }
}
