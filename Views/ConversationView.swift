import SwiftUI
import AppKit

/// The conversation as a transcript: your messages as quiet bubbles on the
/// right, Jugnu's as rendered Markdown on the left. Tool calls stay out of
/// it -- they're in Activity.
struct ConversationView: View {
    @ObservedObject var appState: AppState

    private var messages: [ChatMessage] {
        appState.messages.filter { $0.role != .tool && !$0.content.isEmpty }
    }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 16) {
                    ForEach(messages) { message in
                        MessageRow(message: message).id(message.id)
                    }
                    if appState.thinking {
                        ThinkingRow(line: appState.progressLine)
                    }
                    if let notice = appState.noticeMessage {
                        NoticeRow(symbol: "info.circle", text: notice, tint: Theme.inkDim) {
                            appState.noticeMessage = nil
                        }
                    }
                    if let notice = appState.securityNotice {
                        NoticeRow(symbol: "shield.lefthalf.filled", text: notice, tint: Theme.clay) {
                            appState.securityNotice = nil
                        }
                    }
                    if let error = appState.errorMessage {
                        NoticeRow(symbol: "exclamationmark.triangle.fill", text: error, tint: Theme.danger) {
                            appState.errorMessage = nil
                        }
                    }
                    Color.clear.frame(height: 1).id(Self.bottom)
                }
                .padding(.horizontal, 18)
                .padding(.top, 8)
                .padding(.bottom, 12)
            }
            .scrollIndicators(.automatic)
            .overlay {
                if messages.isEmpty && !appState.thinking && appState.errorMessage == nil {
                    EmptyState(appState: appState)
                }
            }
            .onAppear { proxy.scrollTo(Self.bottom, anchor: .bottom) }
            .onChange(of: appState.messages.count) { _, _ in
                // A long answer is read from its first line, not its last.
                if let last = messages.last, last.role == .assistant, last.content.count > 600 {
                    proxy.scrollTo(last.id, anchor: .top)
                } else {
                    withAnimation(Theme.Motion.settle) { proxy.scrollTo(Self.bottom, anchor: .bottom) }
                }
            }
            .onChange(of: appState.thinking) { _, thinking in
                if thinking { withAnimation(Theme.Motion.settle) { proxy.scrollTo(Self.bottom, anchor: .bottom) } }
            }
        }
        .mask(
            // Content fades out under the header instead of cutting off hard.
            VStack(spacing: 0) {
                LinearGradient(colors: [.clear, .black], startPoint: .top, endPoint: .bottom).frame(height: 14)
                Color.black
            }
        )
    }

    private static let bottom = "conversation-bottom"
}

// MARK: - Rows

private struct MessageRow: View {
    let message: ChatMessage
    @State private var hovering = false
    @State private var copied = false
    @State private var expanded = false

    /// Long pasted prompts collapse to a few lines instead of filling the panel.
    private var isLong: Bool {
        message.content.count > 480 || message.content.split(separator: "\n", omittingEmptySubsequences: false).count > 10
    }

    private var origin: (symbol: String, label: String)? {
        switch message.origin {
        case .nudge: return ("bolt.fill", "Checked in")
        case .digest: return ("sun.max.fill", "Daily review")
        case .background: return ("tray.and.arrow.down.fill", "Background task")
        case .reply: return nil
        }
    }

    var body: some View {
        if message.role == .user {
            VStack(alignment: .trailing, spacing: 4) {
                HStack {
                    Spacer(minLength: 48)
                    Text(message.content)
                        .font(Theme.text(13.5))
                        .lineSpacing(2)
                        .foregroundStyle(Theme.ink)
                        .lineLimit(isLong && !expanded ? 8 : nil)
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 7)
                        .background(RoundedRectangle(cornerRadius: 14, style: .continuous).fill(Theme.panelRaised))
                }
                if isLong {
                    Button(expanded ? "Show less" : "Show more") { expanded.toggle() }
                        .buttonStyle(.plain)
                        .font(Theme.text(11.5, weight: .medium))
                        .foregroundStyle(Theme.inkFaint)
                        .padding(.trailing, 6)
                }
            }
        } else {
            VStack(alignment: .leading, spacing: 6) {
                if let origin {
                    Label(origin.label, systemImage: origin.symbol)
                        .font(Theme.text(11, weight: .medium))
                        .foregroundStyle(Theme.clay)
                        .labelStyle(.titleAndIcon)
                }
                MarkdownText(source: message.content)
                    .textSelection(.enabled)
                HStack(spacing: 2) {
                    IconButton(symbol: copied ? "checkmark" : "doc.on.doc", size: 11,
                               help: copied ? "Copied" : "Copy", tint: copied ? Theme.clay : Theme.inkFaint) {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(message.content, forType: .string)
                        copied = true
                        DispatchQueue.main.asyncAfter(deadline: .now() + 1.4) { copied = false }
                    }
                }
                .padding(.leading, -7)
                .opacity(hovering || copied ? 1 : 0)
            }
            .padding(.bottom, -8)
            .contentShape(Rectangle())
            .onHover { hovering = $0 }
        }
    }
}

private struct ThinkingRow: View {
    let line: String?

    var body: some View {
        HStack(spacing: 8) {
            FireflyView(state: .thinking, size: 18)
            Text(line?.isEmpty == false ? line! : "Thinking…")
                .font(Theme.text(12.5))
                .foregroundStyle(Theme.inkDim)
                .lineLimit(2)
                .truncationMode(.tail)
                .id(line)
                .transition(.opacity)
        }
        .animation(Theme.Motion.settle, value: line)
        .padding(.leading, -2)
    }
}

private struct NoticeRow: View {
    let symbol: String
    let text: String
    let tint: Color
    let dismiss: () -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: symbol)
                .font(.system(size: 11))
                .foregroundStyle(tint)
                .padding(.top, 1.5)
            Text(text)
                .font(Theme.text(12))
                .foregroundStyle(Theme.ink.opacity(0.85))
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
            IconButton(symbol: "xmark", size: 9, help: "Dismiss", tint: Theme.inkFaint, action: dismiss)
                .padding(.top, -4)
                .padding(.trailing, -6)
        }
        .padding(.horizontal, 11)
        .padding(.vertical, 8)
        .background(RoundedRectangle(cornerRadius: Theme.radiusSmall + 2, style: .continuous).fill(tint.opacity(0.1)))
    }
}

private struct EmptyState: View {
    @ObservedObject var appState: AppState

    private let suggestions = [
        ("music.note", "Play some lo-fi"),
        ("calendar", "What's on my calendar today?"),
        ("sun.max", "Summarize my day"),
    ]

    var body: some View {
        VStack(spacing: 14) {
            FireflyView(state: appState.statusLabel == "starting" ? .thinking : .idle, size: 54)
                .padding(.bottom, 4)
            VStack(spacing: 4) {
                Text(appState.statusLabel == "starting" ? "Waking up…" : "What can I do for you?")
                    .font(Theme.text(15, weight: .semibold))
                    .foregroundStyle(Theme.ink)
                Text("Ask a question or hand me a task.")
                    .font(Theme.text(12))
                    .foregroundStyle(Theme.inkFaint)
            }
            VStack(spacing: 6) {
                ForEach(suggestions, id: \.1) { symbol, text in
                    SuggestionButton(symbol: symbol, text: text) { appState.send(text) }
                        .disabled(appState.statusLabel != "ready")
                }
            }
            .padding(.top, 6)
        }
        .padding(.bottom, 20)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

private struct SuggestionButton: View {
    let symbol: String
    let text: String
    let action: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            HStack(spacing: 7) {
                Image(systemName: symbol).font(.system(size: 11)).foregroundStyle(Theme.clay.opacity(0.9))
                Text(text).font(Theme.text(12)).foregroundStyle(hovering ? Theme.ink : Theme.inkDim)
            }
            .padding(.horizontal, 11)
            .padding(.vertical, 6)
            .background(Capsule().fill(hovering ? Theme.panelRaised : Theme.panel))
            .overlay(Capsule().stroke(Theme.border, lineWidth: 1))
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
    }
}
