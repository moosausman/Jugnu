import SwiftUI

struct InputBarView: View {
    @ObservedObject var appState: AppState
    @State private var text: String = ""
    @FocusState private var focused: Bool

    var body: some View {
        HStack(spacing: 9) {
            Text("\u{203A}")
                .foregroundStyle(Theme.clay)
                .font(.system(size: 14, design: .monospaced))

            TextField(appState.thinking ? "Jugnu is working…" : "Ask Jugnu anything…", text: $text)
                .textFieldStyle(.plain)
                .font(Theme.text(13))
                .foregroundStyle(Theme.ink)
                .focused($focused)
                .disabled(appState.thinking)
                .onSubmit(send)

            if appState.thinking {
                ThinkingDots()
                    .transition(.opacity)
            } else {
                Button(action: send) {
                    Image(systemName: "arrow.right")
                        .font(.system(size: 13, weight: .medium))
                }
                .buttonStyle(PressableButtonStyle())
                .foregroundStyle(canSend ? Theme.clay : Theme.inkFaint)
                .disabled(!canSend)
                .transition(.opacity)
            }
        }
        .padding(10)
        .background(Theme.panel)
        .clipShape(RoundedRectangle(cornerRadius: Theme.radiusMedium))
        .overlay(
            RoundedRectangle(cornerRadius: Theme.radiusMedium)
                .stroke(focused ? Theme.borderStrong : Theme.border, lineWidth: 1)
        )
        .shadow(color: Color.black.opacity(focused ? 0.22 : 0.12), radius: focused ? 14 : 8, y: 4)
        .animation(Theme.Motion.snappy, value: focused)
        .animation(Theme.Motion.settle, value: appState.thinking)
        .opacity(appState.thinking ? 0.75 : 1)
        .padding(.horizontal, Theme.Spacing.lg)
        .padding(.bottom, 14)
        .onAppear { focused = true }
    }

    private var canSend: Bool {
        !appState.thinking && !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private func send() {
        guard canSend else { return }
        appState.send(text)
        text = ""
    }
}

/// Scales the button down slightly on press — a small bit of tactile
/// feedback that the plain macOS button style doesn't give you for free.
private struct PressableButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? 0.88 : 1)
            .animation(Theme.Motion.snappy, value: configuration.isPressed)
    }
}

/// A three-dot "Jugnu is composing" indicator, in place of the send arrow
/// while a request is in flight.
private struct ThinkingDots: View {
    @State private var phase: CGFloat = 0

    var body: some View {
        HStack(spacing: 3) {
            ForEach(0..<3, id: \.self) { i in
                Circle()
                    .fill(Theme.clay)
                    .frame(width: 4.5, height: 4.5)
                    .opacity(dotOpacity(for: i))
            }
        }
        .frame(width: 24, height: 13)
        .onAppear {
            withAnimation(.linear(duration: 1.1).repeatForever(autoreverses: false)) {
                phase = 1
            }
        }
    }

    private func dotOpacity(for index: Int) -> Double {
        let shifted = (phase + CGFloat(index) * 0.33).truncatingRemainder(dividingBy: 1)
        return 0.35 + 0.65 * Double(sin(shifted * .pi))
    }
}

#Preview {
    InputBarView(appState: AppState())
        .background(Theme.windowBackground)
}
