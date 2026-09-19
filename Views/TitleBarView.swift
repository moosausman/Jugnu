import SwiftUI

struct TitleBarView: View {
    @ObservedObject var appState: AppState
    @Binding var showHistory: Bool

    @State private var statusHovering = false
    @State private var historyHovering = false

    private var lastAssistantMessage: ChatMessage? {
        appState.messages.last(where: { $0.role == .assistant })
    }

    /// Same badge HeroView puts on its (much bigger) firefly for an
    /// unprompted nudge/digest, scaled down to fit this one. Keeps the
    /// signal visible here too -- e.g. while history is open and the
    /// hero isn't in view.
    private var originBadge: (icon: String, tint: Color, help: String)? {
        switch lastAssistantMessage?.origin {
        case .nudge: return ("bolt.fill", Theme.clay, "Jugnu checked in unprompted")
        case .digest: return ("doc.text.fill", Theme.clay, "Daily digest")
        case .reply, nil: return nil
        }
    }

    /// AppState already tracks plannerModelName, agentModelName, and
    /// toolCount -- reported once on "ready" and otherwise unused anywhere
    /// in the UI. Surfacing them here costs nothing new to build and means
    /// there's always an answer to "what is this actually running," the
    /// same way Anthropic's own tools stay upfront about which model is
    /// doing the work rather than treating it as a hidden implementation
    /// detail. Falls back to a plain description before "ready" arrives.
    private var statusHelpText: String {
        var lines: [String] = []
        if !appState.plannerModelName.isEmpty || !appState.agentModelName.isEmpty {
            let planner = appState.plannerModelName.isEmpty ? "—" : appState.plannerModelName
            let agent = appState.agentModelName.isEmpty ? "—" : appState.agentModelName
            lines.append("Planner: \(planner)")
            lines.append("Agent: \(agent)")
        }
        if appState.toolCount > 0 {
            lines.append("\(appState.toolCount) tool\(appState.toolCount == 1 ? "" : "s") available")
        }
        return lines.isEmpty ? "Tap for conversation history" : lines.joined(separator: "\n")
    }

    var body: some View {
        HStack(spacing: 10) {
            ZStack(alignment: .topTrailing) {
                FireflyView(state: fireflyState, size: 18)

                if let badge = originBadge {
                    Image(systemName: badge.icon)
                        .font(.system(size: 6, weight: .bold))
                        .foregroundStyle(Theme.windowBackground)
                        .padding(2.5)
                        .background(Circle().fill(badge.tint))
                        .offset(x: 3, y: -2)
                        .transition(.scale.combined(with: .opacity))
                        .help(badge.help)
                }
            }
            .animation(Theme.Motion.settle, value: lastAssistantMessage)

            HStack(spacing: 6) {
                // A small status dot that pulses while working. Colour and
                // motion carry the state at a glance, before the word next
                // to it is read.
                StatusDot(state: fireflyState)
                Text("Jugnu")
                    .font(Theme.text(12.5, weight: .semibold))
                    .foregroundStyle(Theme.ink)
                Text("·")
                    .font(Theme.text(12))
                    .foregroundStyle(Theme.inkFaint)
                // The status label is the one place a long agent/tool name
                // can appear ("whatsapp: open the chat with..."), and it
                // used to push the history button off the edge. Bounded and
                // truncated, with the full text in the tooltip.
                Text(appState.statusLabel.lowercased())
                    .font(Theme.text(12))
                    .foregroundStyle(appState.errorMessage != nil ? Theme.danger : Theme.inkDim)
                    .lineLimit(1)
                    .truncationMode(.tail)
                    .frame(maxWidth: 170, alignment: .leading)
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(Capsule().fill(Theme.panel.opacity(statusHovering ? 0.7 : 0)))
            .contentShape(Rectangle())
            .onTapGesture { withAnimation(Theme.Motion.snappy) { showHistory.toggle() } }
            .onHover { hovering in
                withAnimation(Theme.Motion.snappy) { statusHovering = hovering }
            }
            .help(statusHelpText)

            Spacer()

            Button(action: { withAnimation(Theme.Motion.snappy) { showHistory.toggle() } }) {
                Image(systemName: "clock.arrow.circlepath")
                    .font(.system(size: 13))
            }
            .buttonStyle(.plain)
            .foregroundStyle(historyHovering ? Theme.ink : Theme.inkDim)
            .scaleEffect(historyHovering ? 1.08 : 1)
            .animation(Theme.Motion.snappy, value: historyHovering)
            .onHover { historyHovering = $0 }
            .keyboardShortcut("y", modifiers: .command)
            .help("Conversation history (⌘Y)")
        }
        .padding(.trailing, Theme.Spacing.lg)
        .padding(.top, 13)
        // Clears the native traffic lights, which sit at the window's
        // top-left in a titlebarAppearsTransparent window. 72pt total was
        // measurably more than they occupy and left a visible dead gutter
        // between them and the app name.
        .padding(.leading, 74)
        .frame(height: 28)
    }

    private var fireflyState: FireflyState {
        if appState.errorMessage != nil { return .error }
        return appState.thinking ? .thinking : .idle
    }
}

#Preview {
    TitleBarView(appState: AppState(), showHistory: .constant(false))
        .background(Theme.windowBackground)
}


/// Three-state indicator: steady when idle, pulsing while thinking, dim and
/// still on an error. Small enough to ignore, present enough to answer
/// "is it doing anything?" without reading.
private struct StatusDot: View {
    let state: FireflyState

    private var color: Color {
        switch state {
        case .idle: return Theme.clay.opacity(0.75)
        case .thinking: return Theme.clay
        case .error: return Theme.danger
        }
    }

    var body: some View {
        Group {
            if state == .thinking {
                // Only spin a clock while there is actually something to
                // animate. This previously ran at 30fps permanently to
                // compute a pulse that was zero in every state but this one.
                TimelineView(.animation(minimumInterval: 1.0 / 20.0)) { timeline in
                    let t = timeline.date.timeIntervalSinceReferenceDate
                    let pulse = 0.5 + 0.5 * sin(t * 3.4)
                    dot(pulse: pulse)
                }
            } else {
                dot(pulse: 0)
            }
        }
        .frame(width: 8, height: 8)
        .animation(Theme.Motion.settle, value: state)
    }

    private func dot(pulse: Double) -> some View {
        Circle()
            .fill(color)
            .frame(width: 5.5, height: 5.5)
            .scaleEffect(1 + 0.30 * pulse)
            .shadow(color: color.opacity(0.35 + 0.45 * pulse), radius: 2 + 3 * pulse)
    }
}
