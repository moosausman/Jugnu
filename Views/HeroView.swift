import SwiftUI

struct HeroView: View {
    @ObservedObject var appState: AppState

    @State private var didCopy = false
    @State private var copyHovering = false

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
    /// daily_review.py spoke up unprompted.
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

    /// Short answers read well centered -- they look composed, like a
    /// headline. Anything longer needs to be left-aligned: centered text
    /// gives every line a different starting x, so the eye has to hunt for
    /// the start of each one, which is exactly the cost you don't want to
    /// pay on the content people read most.
    private var isShortAnswer: Bool {
        (lastAssistant?.count ?? 0) < 90
    }

    /// Renders the answer as Markdown when it parses, since models
    /// habitually emit **bold**, `code` and bullet lists, and showing that
    /// punctuation raw is noise. Falls back to plain text.
    private var renderedAnswer: AttributedString {
        let raw = lastAssistant ?? "Jugnu is listening."
        if let parsed = try? AttributedString(
            markdown: raw,
            options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        ) {
            return parsed
        }
        return AttributedString(raw)
    }

    var body: some View {
        VStack(spacing: 14) {
            firefly
            eyebrow
            answer
            if let error = appState.errorMessage { errorBanner(error) }
        }
        .padding(.horizontal, Theme.Spacing.xl)
        .padding(.vertical, Theme.Spacing.md)
        .textSelection(.enabled)
        .animation(Theme.Motion.settle, value: lastAssistantMessage)
        .animation(Theme.Motion.settle, value: lastUser)
        .animation(Theme.Motion.settle, value: appState.errorMessage)
    }

    // MARK: - Firefly

    // Shrunk from 56 and given a fixed, smaller frame. It previously sat in
    // a pair of expanding Spacers that pushed it into the middle of an
    // otherwise empty window -- the mascot took roughly as much vertical
    // space as the answer did, on a panel whose whole job is showing the
    // answer. It still leads the eye; it just doesn't dominate any more.
    private var firefly: some View {
        ZStack(alignment: .topTrailing) {
            FireflyView(state: fireflyState, size: 38)

            if let badge = originBadge {
                Image(systemName: badge.icon)
                    .font(.system(size: 8, weight: .bold))
                    .foregroundStyle(Theme.windowBackground)
                    .padding(4)
                    .background(Circle().fill(badge.tint))
                    .offset(x: 3, y: -2)
                    .transition(.scale.combined(with: .opacity))
            }
        }
        .padding(.top, 2)
    }

    // MARK: - Eyebrow

    @ViewBuilder
    private var eyebrow: some View {
        if let assistant = lastAssistantMessage, assistant.origin != .reply {
            eyebrowPill(eyebrowLabel(for: assistant.origin),
                        tint: Theme.clay, background: Theme.clay.opacity(0.12))
                .id("eyebrow-origin-\(assistant.id)")
        } else if let text = lastUser {
            eyebrowPill(text, tint: Theme.inkFaint, background: Theme.panel.opacity(0.6))
                .id("eyebrow-\(text)")
        } else {
            Text("say something to begin")
                .font(Theme.text(11.5))
                .foregroundStyle(Theme.inkFaint)
        }
    }

    private func eyebrowPill(_ label: String, tint: Color, background: Color) -> some View {
        Text(label)
            .font(Theme.text(11, weight: .medium))
            .foregroundStyle(tint)
            .lineLimit(1)
            .truncationMode(.tail)
            .padding(.horizontal, 10)
            .padding(.vertical, 4)
            .background(Capsule().fill(background))
            .transition(.opacity.combined(with: .move(edge: .top)))
    }

    // MARK: - Answer

    // The important fix. This was `.lineLimit(6)` on a non-scrolling Text,
    // so any answer longer than six lines was silently cut off -- no
    // ellipsis, no scrollbar, no indication that there was more. The only
    // way to read a long reply was to open the History sheet, which nothing
    // told you. Jugnu's whole output is this view; truncating it by default
    // made the app feel broken on exactly the answers worth reading.
    //
    // Now it scrolls, wraps, and keeps a copy button next to it.
    private var answer: some View {
        ScrollViewReader { proxy in
            ScrollView(.vertical, showsIndicators: true) {
                Text(renderedAnswer)
                    .font(Theme.text(isShortAnswer ? 19 : 14.5,
                                     weight: isShortAnswer ? .semibold : .regular))
                    .lineSpacing(Theme.bodyLineSpacing)
                    .foregroundStyle(Theme.ink)
                    .multilineTextAlignment(isShortAnswer ? .center : .leading)
                    .frame(maxWidth: .infinity,
                           alignment: isShortAnswer ? .center : .leading)
                    .fixedSize(horizontal: false, vertical: true)
                    .id("answer-body")
            }
            .frame(maxHeight: .infinity)
            .onChange(of: lastAssistant) { _, _ in
                // A new answer starts at the top, not wherever the previous
                // one was scrolled to.
                proxy.scrollTo("answer-body", anchor: .top)
            }
        }
        .overlay(alignment: .topTrailing) { copyButton }
    }

    @ViewBuilder
    private var copyButton: some View {
        if let text = lastAssistant, !text.isEmpty {
            Button {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(text, forType: .string)
                withAnimation(Theme.Motion.snappy) { didCopy = true }
                Task {
                    try? await Task.sleep(nanoseconds: 1_400_000_000)
                    withAnimation(Theme.Motion.snappy) { didCopy = false }
                }
            } label: {
                Image(systemName: didCopy ? "checkmark" : "doc.on.doc")
                    .font(.system(size: 10, weight: .medium))
                    .foregroundStyle(didCopy ? Theme.clay : Theme.inkFaint)
                    .padding(5)
                    .background(
                        Circle().fill(Theme.panel.opacity(copyHovering || didCopy ? 0.9 : 0))
                    )
            }
            .buttonStyle(.plain)
            .onHover { hovering in
                withAnimation(Theme.Motion.snappy) { copyHovering = hovering }
            }
            .help(didCopy ? "Copied" : "Copy this answer")
            .offset(x: 4, y: -2)
        }
    }

    // MARK: - Error

    private func errorBanner(_ error: String) -> some View {
        HStack(alignment: .top, spacing: 7) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(size: 10))
                .foregroundStyle(Theme.danger)
                .padding(.top, 1)
            Text(error)
                .font(Theme.text(11.5))
                .foregroundStyle(Theme.danger)
                .multilineTextAlignment(.leading)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 11)
        .padding(.vertical, 8)
        .background(RoundedRectangle(cornerRadius: Theme.radiusSmall).fill(Theme.danger.opacity(0.12)))
        .transition(.opacity.combined(with: .scale(scale: 0.97)))
    }

    private var fireflyState: FireflyState {
        if appState.errorMessage != nil { return .error }
        return appState.thinking ? .thinking : .idle
    }
}

#Preview {
    HeroView(appState: AppState())
        .frame(width: 420, height: 380)
        .background(Theme.windowBackground)
}
