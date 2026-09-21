import SwiftUI

/// Design tokens for Jugnu: one warm, dark palette, one type scale, one
/// spacing scale. Every view draws from here instead of ad-hoc literals.
enum Theme {

    // MARK: - Surfaces

    /// The window itself. Sits over real vibrancy, so it's near-opaque rather than solid.
    static let windowBackground = Color(hex: 0x15120F)
    /// Cards, the input field, grouped rows.
    static let panel = Color(hex: 0x201B17)
    static let panelSolid = Color(hex: 0x201B17)
    /// Hover and pressed fills on top of `panel`.
    static let panelRaised = Color(hex: 0x2A241F)
    /// Wells: code blocks, text fields inside cards.
    static let well = Color.black.opacity(0.28)

    // MARK: - Lines

    static let border = Color.white.opacity(0.07)
    static let borderStrong = Color.white.opacity(0.13)

    // MARK: - Ink

    static let ink = Color(hex: 0xF3ECE4)
    static let inkDim = ink.opacity(0.62)
    static let inkFaint = ink.opacity(0.38)

    // MARK: - Accent and status

    static let clay = Color(hex: 0xE58A55)
    static let danger = Color(hex: 0xE8685C)
    static let success = Color(hex: 0x76C28E)

    // MARK: - Shape

    static let radiusSmall: CGFloat = 7
    static let radiusMedium: CGFloat = 11
    static let radiusLarge: CGFloat = 16

    // MARK: - Window

    enum WindowSize {
        /// What the chat panel opens at: a quick panel, not a document window.
        static let initial = CGSize(width: 380, height: 540)
        static let min = CGSize(width: 340, height: 420)
        static let max = CGSize(width: 760, height: 1100)
        static let settings = CGSize(width: 560, height: 520)
    }

    // MARK: - Spacing

    enum Spacing {
        static let xs: CGFloat = 4
        static let sm: CGFloat = 8
        static let md: CGFloat = 12
        static let lg: CGFloat = 16
        static let xl: CGFloat = 24
    }

    // MARK: - Motion
    // Deliberately little of it. The firefly is the one thing that moves on
    // its own; everything else only fades when its content changes.

    enum Motion {
        /// Direct responses to input: hover, toggles, showing a field.
        static let snappy = Animation.easeOut(duration: 0.14)
        /// Content changing on its own: a new message, a panel opening.
        static let settle = Animation.easeInOut(duration: 0.22)
    }

    // MARK: - Type
    // `text` for anything read as language; `mono` for things inspected
    // character by character (model tags, paths, tool names).

    static func text(_ size: CGFloat, weight: Font.Weight = .regular) -> Font {
        .system(size: size, weight: weight)
    }

    static func mono(_ size: CGFloat, weight: Font.Weight = .regular) -> Font {
        .system(size: size, weight: weight, design: .monospaced)
    }

    static let bodyLineSpacing: CGFloat = 3
}

extension Color {
    init(hex: UInt32, opacity: Double = 1) {
        self.init(
            .sRGB,
            red: Double((hex >> 16) & 0xFF) / 255,
            green: Double((hex >> 8) & 0xFF) / 255,
            blue: Double(hex & 0xFF) / 255,
            opacity: opacity
        )
    }
}

// MARK: - Shared controls

/// Small borderless icon button used in headers: dims at rest, brightens on hover.
struct IconButton: View {
    let symbol: String
    var size: CGFloat = 13
    var help: String = ""
    var tint: Color = Theme.inkDim
    let action: () -> Void

    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: size, weight: .regular))
                .frame(width: 26, height: 24)
                .foregroundStyle(hovering ? Theme.ink : tint)
                .background(RoundedRectangle(cornerRadius: 6).fill(hovering ? Color.white.opacity(0.07) : .clear))
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .help(help)
    }
}

/// Filled accent button (primary) or quiet tinted button (secondary).
struct JugnuButtonStyle: ButtonStyle {
    enum Kind { case primary, secondary, destructive }
    var kind: Kind = .secondary
    var compact = false

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(Theme.text(compact ? 11.5 : 12, weight: .medium))
            .padding(.horizontal, compact ? 10 : 12)
            .padding(.vertical, compact ? 4 : 5.5)
            .foregroundStyle(foreground)
            .background(RoundedRectangle(cornerRadius: 7).fill(background))
            .opacity(configuration.isPressed ? 0.75 : 1)
    }

    private var foreground: Color {
        switch kind {
        case .primary: return Color(hex: 0x1A120C)
        case .secondary: return Theme.ink
        case .destructive: return Theme.danger
        }
    }

    private var background: Color {
        switch kind {
        case .primary: return Theme.clay
        case .secondary: return Color.white.opacity(0.08)
        case .destructive: return Theme.danger.opacity(0.12)
        }
    }
}
