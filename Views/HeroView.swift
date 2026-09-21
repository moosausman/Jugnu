import SwiftUI

struct HeroView: View {
    @ObservedObject var appState: AppState

    @State private var didCopy = false
    @State private var revealed = false
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
        case .background: return ("tray.and.arrow.down.fill", Theme.clay)
        case .reply, nil: return nil
        }
    }

    private func eyebrowLabel(for origin: ChatMessage.Origin) -> String {
        switch origin {
        case .nudge: return "jugnu checked in"
        case .digest: return "daily digest"
        case .background: return "background task"
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

    /// Height the scroll content should fill so a short answer can centre
    /// itself. Read from the enclosing geometry rather than hardcoded, so
    /// it stays correct at any window size.
    @State private var availableAnswerHeight: CGFloat = 0

    var body: some View {
        VStack(spacing: 14) {
            firefly
            eyebrow
            answer
            if appState.thinking, let line = appState.progressLine, !line.isEmpty {
                progressRow(line)
            }
            if let notice = appState.securityNotice { securityBanner(notice) }
            if let error = appState.errorMessage { errorBanner(error) }
        }
        .padding(.horizontal, Theme.Spacing.xl)
        .padding(.vertical, Theme.Spacing.md)
        .textSelection(.enabled)
        .animation(Theme.Motion.settle, value: lastAssistantMessage)
        .animation(Theme.Motion.settle, value: lastUser)
        .animation(Theme.Motion.settle, value: appState.errorMessage)
        .animation(Theme.Motion.settle, value: appState.progressLine)
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
                // Rendered per line rather than as one Text so each line can
                // arrive on its own short delay. A whole answer fading in at
                // once reads as a screen redraw; lines landing in sequence
                // reads as something being said. The stagger is small (28ms)
                // and capped, so a 40-line answer still completes fast -- the
                // effect is a ripple, not a queue you wait on.
                VStack(alignment: isShortAnswer ? .center : .leading,
                       spacing: Theme.bodyLineSpacing) {
                    ForEach(Array(answerLines.enumerated()), id: \.offset) { index, line in
                        Text(markdown(line))
                            .font(Theme.text(isShortAnswer ? 19 : 14.5,
                                             weight: isShortAnswer ? .semibold : .regular))
                            .foregroundStyle(Theme.ink)
                            .multilineTextAlignment(isShortAnswer ? .center : .leading)
                            .frame(maxWidth: .infinity,
                                   alignment: isShortAnswer ? .center : .leading)
                            .fixedSize(horizontal: false, vertical: true)
                            .opacity(revealed ? 1 : 0)
                            .offset(y: revealed ? 0 : 9)
                            .blur(radius: revealed ? 0 : 2.5)
                            .animation(Theme.Motion.arriveStaggered(index), value: revealed)
                    }
                }
                .id("answer-body")
                .padding(.trailing, 20)   // clear of the copy button
                // A short answer centres itself in whatever height is
                // available; a long one starts at the top and scrolls.
                // Without this the layout only looked composed at the
                // minimum window size -- resize taller and a one-line reply
                // clung to the top edge with a large void under it.
                .frame(minHeight: isShortAnswer ? availableAnswerHeight : 0,
                       alignment: isShortAnswer ? .center : .top)
            }
            .frame(maxHeight: .infinity)
            .background(
                GeometryReader { geo in
                    Color.clear.onAppear { availableAnswerHeight = geo.size.height }
                        .onChange(of: geo.size.height) { _, h in availableAnswerHeight = h }
                }
            )
            .onChange(of: lastAssistant) { _, _ in
                proxy.scrollTo("answer-body", anchor: .top)
                // Drop to hidden without animation, then let the staggered
                // animation above carry it back in. Animating the reset too
                // would make lines fade out and back rather than arrive.
                var t = Transaction(); t.disablesAnimations = true
                withTransaction(t) { revealed = false }
                DispatchQueue.main.async { revealed = true }
            }
            .onAppear { revealed = true }
        }
        .overlay(alignment: .topTrailing) { copyButton }
        // While Jugnu is working, a slow warm sheen travels down the answer
        // area. It's the "something is happening here" signal in the place
        // the user is already looking, rather than only in the input bar.
        .overlay {
            if appState.thinking { ThinkingSheen() }
        }
    }

    /// Split for the staggered reveal. Blank lines are preserved so
    /// paragraph breaks survive, but collapsed to a single spacer row.
    private var answerLines: [String] {
        let raw = lastAssistant ?? "Jugnu is listening."
        let lines = raw.components(separatedBy: .newlines)
        return lines.isEmpty ? [raw] : lines
    }

    private func markdown(_ line: String) -> AttributedString {
        (try? AttributedString(
            markdown: line,
            options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        )) ?? AttributedString(line)
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

    // MARK: - Progress

    /// One quiet line of what Jugnu is doing right now on a longer task --
    /// enough to know it's moving, without turning the chat into a log.
    private func progressRow(_ line: String) -> some View {
        HStack(spacing: 6) {
            Image(systemName: "arrow.triangle.2.circlepath")
                .font(.system(size: 9, weight: .semibold))
                .foregroundStyle(Theme.clay)
            Text(line)
                .font(Theme.text(11))
                .foregroundStyle(Theme.inkDim)
                .lineLimit(2)
                .truncationMode(.tail)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .transition(.opacity)
        .id(line)
    }

    private func securityBanner(_ notice: String) -> some View {
        HStack(alignment: .top, spacing: 7) {
            Image(systemName: "shield.lefthalf.filled")
                .font(.system(size: 10))
                .foregroundStyle(Theme.clay)
                .padding(.top, 1)
            Text(notice)
                .font(Theme.text(11))
                .foregroundStyle(Theme.inkDim)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
            Button { appState.securityNotice = nil } label: {
                Image(systemName: "xmark").font(.system(size: 8, weight: .bold))
            }
            .buttonStyle(.plain)
            .foregroundStyle(Theme.inkFaint)
        }
        .padding(.horizontal, 11)
        .padding(.vertical, 7)
        .background(RoundedRectangle(cornerRadius: Theme.radiusSmall).fill(Theme.clay.opacity(0.10)))
        .transition(.opacity)
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


/// A slow warm sheen that travels down whatever it overlays, while Jugnu is
/// working. Deliberately low-contrast and slow — a sign of life in the
/// place the eye is already resting, not a progress bar demanding attention.
private struct ThinkingSheen: View {
    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30.0)) { timeline in
            let t = timeline.date.timeIntervalSinceReferenceDate
            let phase = (t / 2.6) - (t / 2.6).rounded(.down)

            GeometryReader { geo in
                LinearGradient(
                    colors: [.clear, Theme.clay.opacity(0.13), .clear],
                    startPoint: .top, endPoint: .bottom
                )
                .frame(height: geo.size.height * 0.45)
                .offset(y: geo.size.height * (-0.45 + 1.45 * phase))
                .blur(radius: 14)
            }
        }
        .blendMode(.plusLighter)
        .allowsHitTesting(false)
        .transition(.opacity)
    }
}
