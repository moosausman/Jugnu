import SwiftUI

enum FireflyState: Equatable {
    case idle
    case thinking
    case error
}

/// Jugnu's mascot: a small glowing body with two soft wings. It's the one
/// thing in the app that moves on its own, and its rhythm is the state
/// indicator -- a slow hover at rest, a quick wing-beat while working, and
/// nearly still (and red) on an error.
///
/// Driven by one TimelineView clock instead of stacked `repeatForever`
/// animations, so switching state never leaves an old animation fighting
/// the new one, and nothing runs while the window is hidden.
struct FireflyView: View {
    var state: FireflyState
    var size: CGFloat = 22

    private var glow: Color {
        switch state {
        case .idle: return Color(hex: 0xF2B872)
        case .thinking: return Color(hex: 0xFFD49A)
        case .error: return Theme.danger
        }
    }

    private var bodyColor: Color {
        switch state {
        case .idle: return Color(hex: 0xFFDCA6)
        case .thinking: return Color(hex: 0xFFF0D4)
        case .error: return Color(hex: 0xE8B3AA)
        }
    }

    /// (bob seconds, wing seconds, bob amount, wing degrees)
    private var rhythm: (bob: Double, wing: Double, lift: Double, flap: Double) {
        switch state {
        case .idle: return (2.6, 1.9, 0.10, 10)
        case .thinking: return (1.0, 0.30, 0.14, 16)
        case .error: return (3.0, 3.0, 0.02, 2)
        }
    }

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30.0)) { timeline in
            let t = timeline.date.timeIntervalSinceReferenceDate
            let r = rhythm
            let bob = sin(t * 2 * .pi / r.bob)
            let wing = sin(t * 2 * .pi / r.wing)
            let pulse = 0.5 + 0.5 * sin(t * 2 * .pi / (r.bob * 0.9))

            ZStack {
                if size >= 28 {
                    Motes(t: t * (state == .thinking ? 2.2 : 1), size: size, glow: glow,
                          visibility: state == .error ? 0.25 : 1)
                }
                Circle()
                    .fill(RadialGradient(colors: [glow.opacity(0.22), glow.opacity(0)],
                                         center: .center, startRadius: 0, endRadius: size * 0.75))
                    .frame(width: size * 1.5, height: size * 1.5)
                    .opacity(0.55 + 0.35 * pulse)
                Circle()
                    .fill(RadialGradient(stops: [
                        .init(color: glow.opacity(0.95), location: 0),
                        .init(color: glow.opacity(0.35), location: 0.45),
                        .init(color: glow.opacity(0), location: 1),
                    ], center: .center, startRadius: 0, endRadius: size * 0.46))
                    .frame(width: size * 0.92, height: size * 0.92)
                    .scaleEffect(1 + 0.06 * pulse)
                    .opacity(0.7 + 0.3 * pulse)
                HStack(spacing: size * 0.1) {
                    Ellipse()
                        .fill(Color.white.opacity(0.55))
                        .frame(width: size * 0.4, height: size * 0.16)
                        .rotationEffect(.degrees(-4 - r.flap * (0.5 + 0.5 * wing)), anchor: .trailing)
                    Circle()
                        .fill(bodyColor)
                        .frame(width: size * 0.15, height: size * 0.15)
                    Ellipse()
                        .fill(Color.white.opacity(0.55))
                        .frame(width: size * 0.4, height: size * 0.16)
                        .rotationEffect(.degrees(4 + r.flap * (0.5 + 0.5 * wing)), anchor: .leading)
                }
            }
            .frame(width: size, height: size)
            .offset(y: -CGFloat(bob) * size * r.lift)
        }
        .frame(width: size, height: size)
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }
}

/// Three faint motes circling a large firefly, each with a short trail.
private struct Motes: View {
    let t: Double
    let size: CGFloat
    let glow: Color
    let visibility: Double

    private static let orbits: [(rx: Double, ry: Double, period: Double, phase: Double)] = [
        (0.78, 0.46, 6.9, 0.0), (0.58, 0.72, 9.3, 2.1), (0.92, 0.60, 12.7, 4.3),
    ]

    var body: some View {
        Canvas { ctx, canvas in
            let cx = canvas.width / 2, cy = canvas.height / 2
            for orbit in Self.orbits {
                for echo in 0..<4 {
                    let a = (t - Double(echo) * 0.055) / orbit.period * 2 * .pi + orbit.phase
                    let x = cx + CGFloat(cos(a) * orbit.rx) * size * 0.5
                    let y = cy + CGFloat(sin(a) * orbit.ry) * size * 0.5
                    let fade = 1.0 - Double(echo) / 4
                    let r = size * 0.03 * CGFloat(0.45 + 0.55 * fade)
                    ctx.fill(Path(ellipseIn: CGRect(x: x - r, y: y - r, width: r * 2, height: r * 2)),
                             with: .color(glow.opacity(0.5 * fade * fade * visibility)))
                }
            }
        }
        .frame(width: size * 1.9, height: size * 1.9)
    }
}

#Preview {
    HStack(spacing: 30) {
        FireflyView(state: .idle, size: 56)
        FireflyView(state: .thinking, size: 56)
        FireflyView(state: .error, size: 18)
    }
    .padding(40)
    .background(Theme.windowBackground)
}
