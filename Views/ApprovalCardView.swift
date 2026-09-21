import SwiftUI

/// The one moment Jugnu stops and asks: a consequential action it wants to
/// take, exactly what it is, why it needs a yes, and what can't be undone.
/// Stacks above the input bar; the oldest request is answered first.
struct ApprovalStackView: View {
    @ObservedObject var appState: AppState

    var body: some View {
        if let approval = appState.approvals.first {
            ApprovalCardView(approval: approval, remaining: appState.approvals.count - 1) { decision, note in
                withAnimation(Theme.Motion.settle) {
                    appState.respond(to: approval, decision: decision, note: note)
                }
            }
            .id(approval.id)
            .transition(.move(edge: .bottom).combined(with: .opacity))
            .padding(.horizontal, Theme.Spacing.lg)
            .padding(.bottom, 8)
        }
    }
}

struct ApprovalCardView: View {
    let approval: ApprovalRequest
    let remaining: Int
    let onDecision: (String, String) -> Void

    @State private var note = ""
    @State private var showNote = false

    private var riskColor: Color {
        switch approval.risk {
        case "high": return Theme.danger
        case "medium": return Theme.clay
        default: return Theme.inkDim
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack(spacing: 7) {
                Image(systemName: "hand.raised.fill")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(riskColor)
                Text("Jugnu needs your OK")
                    .font(Theme.text(12, weight: .semibold))
                    .foregroundStyle(Theme.ink)
                Spacer()
                Text(approval.risk.uppercased())
                    .font(Theme.mono(9.5))
                    .foregroundStyle(riskColor)
                    .padding(.horizontal, 6).padding(.vertical, 2)
                    .background(Capsule().fill(riskColor.opacity(0.14)))
                if remaining > 0 {
                    Text("+\(remaining)")
                        .font(Theme.mono(9.5))
                        .foregroundStyle(Theme.inkFaint)
                }
            }

            Text(approval.summary)
                .font(Theme.mono(11))
                .foregroundStyle(Theme.ink)
                .textSelection(.enabled)
                .lineLimit(6)
                .fixedSize(horizontal: false, vertical: true)
                .padding(8)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: Theme.radiusSmall).fill(Color.black.opacity(0.22)))

            let why = ([approval.why] + approval.reasons).filter { !$0.isEmpty }
            if !why.isEmpty {
                Text(why.joined(separator: " · "))
                    .font(Theme.text(10.5))
                    .foregroundStyle(Theme.inkDim)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !approval.reversible {
                Label("Can't be undone", systemImage: "exclamationmark.triangle")
                    .font(Theme.text(10.5, weight: .medium))
                    .foregroundStyle(Theme.danger.opacity(0.9))
            }

            if showNote {
                TextField("Tell Jugnu what to do instead (optional)", text: $note)
                    .textFieldStyle(.plain)
                    .font(Theme.text(11.5))
                    .padding(7)
                    .background(RoundedRectangle(cornerRadius: Theme.radiusSmall).fill(Theme.panelSolid))
            }

            HStack(spacing: 8) {
                Button("Deny") { onDecision("deny", note) }
                    .buttonStyle(ApprovalButtonStyle(kind: .secondary))
                    .keyboardShortcut(.escape, modifiers: [])
                Button(showNote ? "Hide note" : "Add note") {
                    withAnimation(Theme.Motion.snappy) { showNote.toggle() }
                }
                .buttonStyle(.plain)
                .font(Theme.text(11))
                .foregroundStyle(Theme.inkFaint)
                Spacer()
                if approval.allowTaskScope {
                    Button("Allow for this task") { onDecision("approve_task", note) }
                        .buttonStyle(ApprovalButtonStyle(kind: .secondary))
                        .help("Also allow this kind of action for the rest of this task")
                }
                Button("Allow") { onDecision("approve", note) }
                    .buttonStyle(ApprovalButtonStyle(kind: .primary))
                    .keyboardShortcut(.return, modifiers: [.command])
            }
        }
        .padding(12)
        .background(
            RoundedRectangle(cornerRadius: Theme.radiusMedium)
                .fill(Theme.panelSolid)
                .overlay(RoundedRectangle(cornerRadius: Theme.radiusMedium).stroke(riskColor.opacity(0.45), lineWidth: 1))
        )
        .elevation(3)
    }
}

private struct ApprovalButtonStyle: ButtonStyle {
    enum Kind { case primary, secondary }
    let kind: Kind

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(Theme.text(11.5, weight: .semibold))
            .padding(.horizontal, 11)
            .padding(.vertical, 5)
            .foregroundStyle(kind == .primary ? Theme.windowBackground : Theme.ink)
            .background(Capsule().fill(kind == .primary ? Theme.clay : Color.white.opacity(0.08)))
            .scaleEffect(configuration.isPressed ? 0.95 : 1)
            .animation(Theme.Motion.press, value: configuration.isPressed)
    }
}
