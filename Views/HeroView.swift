import SwiftUI

struct HeroView: View {
    @ObservedObject var appState: AppState

    private var lastUser: String? {
        appState.messages.last(where: { $0.role == .user })?.content
    }

    private var lastAssistantMessage: ChatMessage? {
        appState.messages.last(where: { $0.role == .assistant })
    }

    private var lastAssistant: String? {
        lastAssistantMessage?.content
    }

    /// Small badge shown on the firefly when the most recent assistant
    /// message wasn't a reply to anything -- proactive.py or
    /// daily_review.py spoke up unprompted. Reuses Theme.clay (the
    /// existing accent color) rather than inventing a new one, since
    /// Theme.swift isn't part of this batch.
    private var originBadge: (icon: String, tint: Color)? {
        switch lastAssistantMessage?.origin {
        case .nudge: return ("bolt.fill", Theme.clay)
        case .digest: return ("doc.text.fill", Theme.clay)
        case .reply, nil: return nil
        }
    }

    private func eyebrowLabel(for origin: ChatMessage.Origin) -> String {
        switch origin {
        case .nudge: return "jugnu checked in"
        case .digest: return "daily digest"
        case .reply: return ""
        }
    }

    var body: some View {
        VStack(spacing: 16) {
            Spacer(minLength: 0)

            ZStack(alignment: .topTrailing) {
                FireflyView(state: fireflyState, size: 56)

                if let badge = originBadge {
                    Image(systemName: badge.icon)
                        .font(.system(size: 9, weight: .bold))
                        .foregroundStyle(Theme.windowBackground)
                        .padding(5)
                        .background(Circle().fill(badge.tint))
                        .offset(x: 4, y: -2)
                        .transition(.scale.combined(with: .opacity))
                }
            }
            .padding(.bottom, 4)

            // Unprompted messages replace the "last thing you said" eyebrow
            // with a label of their own -- showing your last question above
            // a nudge/digest would wrongly imply it was a direct reply to it.
            //
            // Sentence case, not all-caps: it's already carrying weight via
            // color, size, and the pill background, and one of these two
            // branches is echoing the user's own typed words back at them --
            // shouting those in caps would be a strange thing to do to
            // someone's own sentence.
            if let assistant = lastAssistantMessage, assistant.origin != .reply {
                Text(eyebrowLabel(for: assistant.origin))
                    .font(.system(size: 10.5, weight: .semibold, design: .monospaced))
                    .tracking(0.4)
                    .foregroundStyle(Theme.clay)
                    .lineLimit(1)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 4)
                    .background(Capsule().fill(Theme.clay.opacity(0.12)))
                    .transition(.opacity.combined(with: .move(edge: .top)))
                    .id("eyebrow-origin-\(assistant.id)")
            } else if let eyebrow = lastUser {
                Text(eyebrow)
                    .font(.system(size: 10.5, weight: .semibold, design: .monospaced))
                    .tracking(0.2)
                    .foregroundStyle(Theme.inkFaint)
                    .lineLimit(1)
                    .truncationMode(.tail)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 4)
                    .background(Capsule().fill(Theme.panel.opacity(0.6)))
                    .transition(.opacity.combined(with: .move(edge: .top)))
                    .id("eyebrow-\(eyebrow)")
            } else {
                Text("say something to begin")
                    .font(.system(size: 10.5, weight: .medium, design: .monospaced))
                    .foregroundStyle(Theme.inkFaint)
            }

            Text(lastAssistant ?? "Jugnu is listening.")
                .font(.system(size: 20, weight: .semibold, design: .monospaced))
                .lineSpacing(3)
                .foregroundStyle(Theme.ink)
                .multilineTextAlignment(.center)
                .lineLimit(6)
                .fixedSize(horizontal: false, vertical: true)
                .transition(.opacity.combined(with: .move(edge: .bottom)))
                .id("assistant-\(lastAssistant ?? "")")

            if let error = appState.errorMessage {
                Text(error)
                    .font(.system(size: 13))
                    .foregroundStyle(Theme.danger)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 8)
                    .background(RoundedRectangle(cornerRadius: Theme.radiusSmall).fill(Theme.danger.opacity(0.1)))
                    .transition(.opacity.combined(with: .scale(scale: 0.97)))
            }

            Spacer(minLength: 0)
        }
        .padding(.horizontal, Theme.Spacing.xl)
        .textSelection(.enabled)
        .animation(Theme.Motion.settle, value: lastAssistantMessage)
        .animation(Theme.Motion.settle, value: lastUser)
        .animation(Theme.Motion.settle, value: appState.errorMessage)
    }

    private var fireflyState: FireflyState {
        if appState.errorMessage != nil { return .error }
        return appState.thinking ? .thinking : .idle
    }
}

#Preview {
    HeroView(appState: AppState())
        .frame(width: 420, height: 300)
        .background(Theme.windowBackground)
}
