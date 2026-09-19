import SwiftUI

/// Design tokens for Jugnu. Centralizing colors, spacing, motion, and type
/// here keeps every view drawing from the same warm, "firefly at dusk"
/// palette instead of ad-hoc literals scattered across files.
enum Theme {

    // MARK: - Surfaces

    static let windowBackground = Color(hex: 0x171310)
    static let deepPanel = Color(hex: 0x201A15)
    static let panel = Color(hex: 0x2B231C).opacity(0.82)
    static let panelSolid = Color(hex: 0x2B231C)

    // MARK: - Borders

    static let border = Color.white.opacity(0.08)
    static let borderStrong = Color(hex: 0xE0824F).opacity(0.55)

    // MARK: - Ink (text)

    static let ink = Color(hex: 0xF7EFE3)
    static let inkDim = ink.opacity(0.68)
    static let inkFaint = ink.opacity(0.42)

    // MARK: - Accent

    static let clay = Color(hex: 0xE0824F)
    static let danger = Color(hex: 0xE2685C)

    // MARK: - Radii

    static let radiusSmall: CGFloat = 8
    static let radiusMedium: CGFloat = 12
    static let radiusLarge: CGFloat = 20

    // MARK: - Window
    // The window's min size used to live as two separate hardcoded
    // 420x480 literals -- one in ContentView's `.frame(minWidth:
    // minHeight:)`, one in its WindowAccessor's `window.minSize` -- with
    // nothing keeping them in sync if either changed. One source of
    // truth now. Shrunk from 420x480: this is a quick-access panel that
    // lives a click below a menu bar icon, not a full app window, and
    // read closer to a document window than a petite utility at the old
    // size.

    enum WindowSize {
        static let min = CGSize(width: 360, height: 420)
    }

    // MARK: - Spacing
    // One scale, used everywhere, instead of one-off padding values — so
    // rhythm stays consistent as the app grows.

    enum Spacing {
        static let xs: CGFloat = 4
        static let sm: CGFloat = 8
        static let md: CGFloat = 12
        static let lg: CGFloat = 16
        static let xl: CGFloat = 24
    }

    // MARK: - Motion
    // Three speeds: `snappy` for direct responses to input (focus, press,
    // hover, toggles), `settle` for content that arrives on its own (new
    // messages, state changes), and `breathe` for ambient motion that
    // isn't a response to anything -- the background's slow bloom pulse,
    // meant to be felt more than seen. Deliberately restrained — nothing
    // bounces or overshoots, and ambient motion in particular has to stay
    // slow enough to read as "alive" rather than "animating."

    enum Motion {
        static let snappy = Animation.easeOut(duration: 0.16)
        static let settle = Animation.easeInOut(duration: 0.32)
        static let breathe = Animation.easeInOut(duration: 6)
    }

    // MARK: - Type

    static func mono(_ size: CGFloat, weight: Font.Weight = .medium) -> Font {
        .system(size: size, weight: weight, design: .monospaced)
    }
}

// If your project already defines `Color(hex:)` elsewhere, delete this
// extension so you don't end up with a duplicate-declaration error.
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
