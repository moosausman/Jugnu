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
        // 360x420 was sized for the old layout, where the answer was capped
        // at six lines and anything longer was simply cut off -- so the
        // window never needed room for more. Now that answers scroll and
        // wrap, the constraint is reading comfort: 400pt of width is about
        // 60 characters at the body size, which is a sensible measure, and
        // the extra height buys roughly four more lines before scrolling.
        // Still a quick-access panel, not a document window.
        static let min = CGSize(width: 400, height: 520)
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

        // Springs, for anything that ARRIVES rather than merely changes.
        // Eased curves decelerate into place with a mathematical evenness
        // the eye reads as mechanical; a spring overshoots a touch and
        // settles, which reads as physical. Used for the answer appearing,
        // history rows entering, and button presses.
        //
        // Damping is high enough that nothing visibly wobbles -- the goal
        // is weight, not bounce.

        /// Content arriving on screen: an answer, a row, a badge.
        static let arrive = Animation.spring(response: 0.42, dampingFraction: 0.78)

        /// Direct response to a press or hover. Faster, tighter.
        static let press = Animation.spring(response: 0.26, dampingFraction: 0.68)

        /// Per-line stagger for a multi-line reveal.
        static func arriveStaggered(_ index: Int, step: Double = 0.028, cap: Int = 14) -> Animation {
            arrive.delay(Double(min(index, cap)) * step)
        }
    }

    // MARK: - Elevation
    // Two-layer shadows. A single shadow reads as a sticker; a tight dark
    // contact shadow plus a wide soft ambient one is how real depth looks,
    // and it's what separates a panel that sits ON the background from one
    // that floats above it.

    struct Elevation: ViewModifier {
        var contact: CGFloat
        var ambient: CGFloat
        var opacity: Double

        func body(content: Content) -> some View {
            content
                .shadow(color: .black.opacity(opacity * 0.55), radius: contact, y: contact * 0.45)
                .shadow(color: .black.opacity(opacity * 0.35), radius: ambient, y: ambient * 0.35)
        }
    }

    // MARK: - Type
    //
    // Two families, used for different jobs.
    //
    // Everything used to be `mono`, including paragraphs of explanation in
    // Settings and Jugnu's actual answers. Monospace is a poor choice for
    // prose -- uniform advance widths remove the word-shape cues people read
    // by, so it scans noticeably slower and looks like terminal output rather
    // than something written for you.
    //
    // `text` is for anything meant to be READ: answers, descriptions, labels.
    // `mono` stays for anything meant to be INSPECTED, where character
    // alignment and unambiguous glyphs matter: model tags, file paths, shell
    // commands, tool names. Keeping the monospace accent there preserves the
    // app's character without paying for it in legibility.

    /// Prose. Use for anything the user reads as language.
    static func text(_ size: CGFloat, weight: Font.Weight = .regular) -> Font {
        .system(size: size, weight: weight)
    }

    /// Technical values. Use for model tags, paths, commands, tool names.
    static func mono(_ size: CGFloat, weight: Font.Weight = .medium) -> Font {
        .system(size: size, weight: weight, design: .monospaced)
    }

    // MARK: - Reading measure
    // Line spacing that suits body text at the sizes used here. Tight
    // leading is fine for a label and actively unpleasant for a paragraph.
    static let bodyLineSpacing: CGFloat = 4.5
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

extension View {
    /// Layered depth. `level` 1 is a resting panel, 2 a raised control,
    /// 3 something actively floating (focused input, hovered card).
    func elevation(_ level: Int) -> some View {
        switch level {
        case 1:  return AnyView(modifier(Theme.Elevation(contact: 4,  ambient: 12, opacity: 0.5)))
        case 2:  return AnyView(modifier(Theme.Elevation(contact: 7,  ambient: 20, opacity: 0.6)))
        default: return AnyView(modifier(Theme.Elevation(contact: 11, ambient: 30, opacity: 0.7)))
        }
    }
}
