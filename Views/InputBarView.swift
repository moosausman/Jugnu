import SwiftUI

/// The message field. Grows to a few lines, sends on Return (Option-Return
/// for a new line), and turns its send button into Stop while Jugnu works.
/// You can keep typing the next message while it does.
struct InputBarView: View {
    @ObservedObject var appState: AppState
    @State private var text = ""
    @FocusState private var focused: Bool
    @StateObject private var speech = SpeechInput()

    private var canSend: Bool {
        !appState.thinking && appState.statusLabel != "starting"
            && !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    var body: some View {
        HStack(alignment: .bottom, spacing: 6) {
            TextField(speech.isListening ? "Listening…" : "Message Jugnu", text: $text, axis: .vertical)
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
                .stroke(focused ? Theme.clay.opacity(0.45) : Theme.borderStrong, lineWidth: 1)
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
