import SwiftUI

/// The one moment Jugnu stops and asks. Sits above the input; the oldest
/// request is answered first.
struct ApprovalStackView: View {
    @ObservedObject var appState: AppState

    var body: some View {
        if let approval = appState.approvals.first {
            ApprovalCardView(approval: approval, remaining: appState.approvals.count - 1,
                             keyboardShortcuts: true) { decision, note in
                appState.respond(to: approval, decision: decision, note: note)
            }
            .id(approval.id)
            .padding(.horizontal, 12)
            .padding(.bottom, 4)
            .transition(.opacity)
        }
    }
}

struct ApprovalCardView: View {
    let approval: ApprovalRequest
    let remaining: Int
    /// Only the card above the input takes ⌘Return, so a shortcut can never
    /// answer a different request than the one in front of you. Deny is
    /// always a click.
    var keyboardShortcuts = false
    let onDecision: (String, String) -> Void

    @State private var note = ""
    @State private var showNote = false

    private var tint: Color {
        approval.risk == "high" ? Theme.danger : Theme.clay
    }

    private var title: String {
        let tool = approval.tool.replacingOccurrences(of: "_", with: " ")
        return tool.isEmpty ? "Allow this action?" : "Allow \(tool)?"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Image(systemName: "hand.raised.fill")
                    .font(.system(size: 10.5))
                    .foregroundStyle(tint)
                Text(title)
                    .font(Theme.text(12.5, weight: .semibold))
                    .foregroundStyle(Theme.ink)
                    .lineLimit(1)
                Spacer(minLength: 4)
                if remaining > 0 {
                    Text("+\(remaining) more")
                        .font(Theme.text(10.5))
                        .foregroundStyle(Theme.inkFaint)
                }
                if !approval.reversible {
                    Text("Can't be undone")
                        .font(Theme.text(10.5, weight: .medium))
                        .foregroundStyle(Theme.danger)
                }
            }

            Text(approval.summary)
                .font(Theme.mono(11))
                .foregroundStyle(Theme.ink.opacity(0.9))
                .textSelection(.enabled)
                .lineLimit(5)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 9)
                .padding(.vertical, 7)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: Theme.radiusSmall).fill(Theme.well))

            let why = ([approval.why] + approval.reasons).filter { !$0.isEmpty }
            if !why.isEmpty {
                Text(why.joined(separator: " · "))
                    .font(Theme.text(11))
                    .foregroundStyle(Theme.inkDim)
                    .lineLimit(3)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if showNote {
                TextField("What should Jugnu do instead?", text: $note)
                    .textFieldStyle(.plain)
                    .font(Theme.text(12))
                    .padding(.horizontal, 9)
                    .padding(.vertical, 6)
                    .background(RoundedRectangle(cornerRadius: Theme.radiusSmall).fill(Theme.well))
            }

            HStack(spacing: 6) {
                Button("Deny") { onDecision("deny", note) }
                    .buttonStyle(JugnuButtonStyle(kind: .secondary, compact: true))
                Button { showNote.toggle() } label: {
                    Image(systemName: showNote ? "text.bubble.fill" : "text.bubble")
                        .font(.system(size: 11))
                }
                .buttonStyle(.plain)
                .foregroundStyle(Theme.inkFaint)
                .help("Add a note")
                Spacer()
                if approval.allowTaskScope {
                    Button("Allow for task") { onDecision("approve_task", note) }
                        .buttonStyle(JugnuButtonStyle(kind: .secondary, compact: true))
                        .help("Also allow this kind of action for the rest of this task")
                }
                if keyboardShortcuts {
                    Button("Allow") { onDecision("approve", note) }
                        .buttonStyle(JugnuButtonStyle(kind: .primary, compact: true))
                        .keyboardShortcut(.return, modifiers: .command)
                        .help("Allow (⌘Return)")
                } else {
                    Button("Allow") { onDecision("approve", note) }
                        .buttonStyle(JugnuButtonStyle(kind: .primary, compact: true))
                }
            }
        }
        .padding(11)
        .background(
            RoundedRectangle(cornerRadius: Theme.radiusMedium + 2, style: .continuous)
                .fill(Theme.panelRaised)
        )
        .overlay(
            RoundedRectangle(cornerRadius: Theme.radiusMedium + 2, style: .continuous)
                .stroke(tint.opacity(0.35), lineWidth: 1)
        )
        .shadow(color: .black.opacity(0.25), radius: 10, y: 3)
    }
}
