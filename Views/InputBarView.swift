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
        .padding(11)
        .background(Theme.panel)
        .clipShape(RoundedRectangle(cornerRadius: Theme.radiusMedium))
        // Two strokes: a constant hairline, plus a warm ring that fades in
        // on focus and slowly breathes while it's there. A static accent
        // border says "this is selected"; one that breathes says the app is
        // awake and waiting for you.
        .overlay(
            RoundedRectangle(cornerRadius: Theme.radiusMedium)
                .stroke(Theme.border, lineWidth: 1)
        )
        .overlay {
            if focused {
                TimelineView(.animation(minimumInterval: 1.0 / 20.0)) { timeline in
                    let t = timeline.date.timeIntervalSinceReferenceDate
                    let pulse = 0.5 + 0.5 * sin(t * 1.5)
                    RoundedRectangle(cornerRadius: Theme.radiusMedium)
                        .stroke(Theme.clay.opacity(0.35 + 0.30 * pulse), lineWidth: 1.2)
                        .shadow(color: Theme.clay.opacity(0.18 + 0.16 * pulse), radius: 9)
                }
                .transition(.opacity)
            }
        }
        .elevation(focused ? 3 : 1)
        .animation(Theme.Motion.press, value: focused)
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
            // Spring rather than ease: a press should feel like pushing
            // something with mass, which means a little overshoot coming
            // back. Also dips brightness on the way down, so the feedback
            // survives even where the scale change is subtle.
            .scaleEffect(configuration.isPressed ? 0.82 : 1)
            .brightness(configuration.isPressed ? -0.12 : 0)
            .animation(Theme.Motion.press, value: configuration.isPressed)
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
                    .frame(width: 5, height: 5)
                    .scaleEffect(0.62 + 0.38 * dotWave(for: i))
                    .opacity(0.35 + 0.65 * dotWave(for: i))
                    .shadow(color: Theme.clay.opacity(0.5 * dotWave(for: i)), radius: 3)
            }
        }
        .frame(width: 24, height: 13)
        .onAppear {
            withAnimation(.linear(duration: 1.1).repeatForever(autoreverses: false)) {
                phase = 1
            }
        }
    }

    /// 0...1 travelling wave. Each dot samples the same curve a third of a
    /// cycle apart, so brightness and size move along the row rather than
    /// all three blinking together.
    private func dotWave(for index: Int) -> Double {
        let shifted = (phase + CGFloat(index) * 0.33).truncatingRemainder(dividingBy: 1)
        return Double(sin(shifted * .pi))
    }
}

#Preview {
    InputBarView(appState: AppState())
        .background(Theme.windowBackground)
}
