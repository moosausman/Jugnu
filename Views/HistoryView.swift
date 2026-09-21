import SwiftUI

struct HistoryView: View {
    @ObservedObject var appState: AppState
    @Binding var isPresented: Bool

    @State private var backHovering = false
    @State private var clearHovering = false
    @State private var confirmingClear = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Button(action: { withAnimation(Theme.Motion.settle) { isPresented = false } }) {
                    HStack(spacing: 4) {
                        Image(systemName: "chevron.left")
                            .font(.system(size: 11, weight: .semibold))
                        Text("Back")
                            .font(Theme.text(12.5, weight: .medium))
                    }
                }
                .buttonStyle(.plain)
                .foregroundStyle(Theme.clay)
                .opacity(backHovering ? 1 : 0.82)
                .onHover { hovering in
                    withAnimation(Theme.Motion.snappy) { backHovering = hovering }
                }

                Spacer()

                if !appState.messages.isEmpty {
                    // A whole conversation is a lot to lose to one misclick --
                    // this asks first rather than acting immediately, the way
                    // any other irreversible, destructive action should.
                    Button(action: { confirmingClear = true }) {
                        Text("Clear")
                            .font(Theme.text(12, weight: .medium))
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(clearHovering ? Theme.danger : Theme.inkFaint)
                    .onHover { hovering in
                        withAnimation(Theme.Motion.snappy) { clearHovering = hovering }
                    }
                    .confirmationDialog(
                        "Clear this conversation?",
                        isPresented: $confirmingClear,
                        titleVisibility: .visible
                    ) {
                        Button("Clear History", role: .destructive) {
                            withAnimation(Theme.Motion.settle) { appState.clear() }
                        }
                        Button("Cancel", role: .cancel) {}
                    } message: {
                        Text("This removes everything Jugnu remembers from this conversation. It can't be undone.")
                    }
                }
            }
            .padding(.horizontal, Theme.Spacing.lg)
            .padding(.top, 56)
            .padding(.bottom, 14)

            Divider().overlay(Theme.border)

            ScrollView {
                LazyVStack(alignment: .leading, spacing: 14) {
                    if appState.messages.isEmpty {
                        VStack(spacing: 12) {
                            FireflyView(state: .idle, size: 40)
                            Text("nothing said yet")
                                .font(Theme.text(12))
                                .foregroundStyle(Theme.inkFaint)
                        }
                        .frame(maxWidth: .infinity)
                        .padding(.top, 48)
                    }
                    ForEach(appState.messages) { message in
                        HistoryRow(message: message)
                            .transition(
                                .asymmetric(
                                    insertion: .opacity.combined(with: .move(edge: .bottom)),
                                    removal: .opacity
                                )
                            )
                    }
                }
                .padding(Theme.Spacing.lg)
                .animation(Theme.Motion.settle, value: appState.messages.count)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .background(Theme.panel)
    }
}

private struct HistoryRow: View {
    let message: ChatMessage

    private var who: String {
        switch message.role {
        case .user: return "you"
        case .assistant: return "jugnu"
        case .tool: return "tool"
        }
    }

    private var whoColor: Color {
        message.role == .user ? Theme.inkDim : Theme.clay
    }

    private var textColor: Color {
        switch message.role {
        case .user: return Theme.inkDim
        case .tool: return Theme.inkFaint
        case .assistant: return Theme.ink
        }
    }

    /// Same badge HeroView shows over the firefly for a nudge/digest --
    /// here there's no avatar to pin it to, so it sits under the "jugnu"
    /// label as an icon + word instead. Always nil for non-assistant
    /// rows, since only assistant messages ever carry a non-.reply
    /// origin (see AppState.handle).
    private var originGlyph: (icon: String, label: String)? {
        guard message.role == .assistant else { return nil }
        switch message.origin {
        case .nudge: return ("bolt.fill", "nudge")
        case .digest: return ("doc.text.fill", "digest")
        case .background: return ("tray.and.arrow.down.fill", "task")
        case .reply: return nil
        }
    }

    /// Reply bubbles keep the existing 0.07 clay tint; nudges/digests
    /// get bumped to 0.13 so an unprompted message is visible at a
    /// glance while scanning the scrollback, not just on close reading.
    private var bubbleTint: Color {
        switch message.role {
        case .user: return Color.white.opacity(0.03)
        case .assistant: return Theme.clay.opacity(originGlyph == nil ? 0.07 : 0.13)
        case .tool: return Color.white.opacity(0.015)
        }
    }

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            VStack(alignment: .leading, spacing: 3) {
                Text(who)
                    .font(Theme.text(11.5, weight: .semibold))
                    .foregroundStyle(whoColor)

                if let glyph = originGlyph {
                    HStack(spacing: 3) {
                        Image(systemName: glyph.icon)
                            .font(.system(size: 7.5, weight: .bold))
                        Text(glyph.label)
                            .font(Theme.text(9, weight: .semibold))
                    }
                    .foregroundStyle(Theme.clay)
                }
            }
            .frame(width: 46, alignment: .leading)

            Text(message.content)
                // Tool calls stay monospace -- they're JSON you inspect.
                // Conversation is prose and reads as prose.
                .font(message.role == .tool ? Theme.mono(10.5) : Theme.text(12.5))
                .lineSpacing(message.role == .tool ? 1 : 3)
                .foregroundStyle(textColor)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .background(RoundedRectangle(cornerRadius: Theme.radiusSmall).fill(bubbleTint))
    }
}

#Preview {
    HistoryView(appState: AppState(), isPresented: .constant(true))
}
