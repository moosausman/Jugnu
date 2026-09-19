import SwiftUI

enum FireflyState: Equatable {
    case idle
    case thinking
    case error
}

/// Jugnu's mascot and sole recurring visual motif — a small glowing body
/// with two soft wings. Its motion is the app's only indicator of state, so
/// each state is tuned to read as a distinct, unmistakable rhythm rather
/// than a louder or quieter version of the same animation.
///
/// The glow is three layered pieces, not one: a large, very soft outer
/// bloom that breathes more than anything else on screen, the original mid
/// halo, and a tiny bright specular point at the body's center. One
/// RadialGradient reads as "a colored circle behind an icon"; three, at
/// different radii and intensities, reads as something actually emitting
/// light.
struct FireflyView: View {
    var state: FireflyState
    var size: CGFloat = 22

    @State private var bobOffset: CGFloat = 0
    @State private var haloScale: CGFloat = 1
    @State private var haloOpacity: Double = 0.6
    @State private var leftWingAngle: Double = -4
    @State private var rightWingAngle: Double = 4

    private var glowColor: Color {
        switch state {
        case .idle: return Color(hex: 0xF2B872)
        case .thinking: return Color(hex: 0xFFD9A0)
        case .error: return Theme.danger
        }
    }

    private var bodyColor: Color {
        switch state {
        case .idle: return Color(hex: 0xFFDCA6)
        case .thinking: return Color(hex: 0xFFF1D6)
        case .error: return Color(hex: 0xE8B3AA)
        }
    }

    var body: some View {
        ZStack {
            // Outer bloom -- large and faint, derived from the same
            // haloScale/haloOpacity the mid halo below already animates
            // (exaggerated further) rather than a second timer of its
            // own, so there's nothing new to keep in sync with the
            // existing per-state rhythms in animate(for:). Kept to a
            // modest 1.4x/1.5x exaggeration rather than something more
            // dramatic -- FireflyView renders as small as size 18 (the
            // title bar), and a much larger bloom there would bleed into
            // neighboring text.
            Circle()
                .fill(
                    RadialGradient(
                        colors: [glowColor.opacity(0.22), glowColor.opacity(0)],
                        center: .center,
                        startRadius: 0,
                        endRadius: size * 0.85
                    )
                )
                .frame(width: size * 1.4, height: size * 1.4)
                .scaleEffect(1 + (haloScale - 1) * 1.5)
                .opacity(haloOpacity * 0.45)

            Circle()
                .fill(
                    RadialGradient(
                        gradient: Gradient(stops: [
                            .init(color: glowColor.opacity(0.95), location: 0),
                            .init(color: glowColor.opacity(0.35), location: 0.45),
                            .init(color: glowColor.opacity(0), location: 1),
                        ]),
                        center: .center,
                        startRadius: 0,
                        endRadius: size * 0.46
                    )
                )
                .frame(width: size * 0.92, height: size * 0.92)
                .scaleEffect(haloScale)
                .opacity(haloOpacity)

            // Specular core -- a single soft bright point sitting under
            // where the body circle renders. Mostly covered by that
            // circle in the HStack below; what actually shows is just
            // its blurred edge peeking past it, which is the point -- an
            // internal glow rather than a visible second shape.
            Circle()
                .fill(Color.white.opacity(0.85))
                .frame(width: size * 0.06, height: size * 0.06)
                .blur(radius: size * 0.035)

            HStack(spacing: size * 0.1) {
                Ellipse()
                    .fill(Color.white.opacity(0.55))
                    .frame(width: size * 0.4, height: size * 0.16)
                    .rotationEffect(.degrees(leftWingAngle), anchor: .trailing)

                Circle()
                    .fill(bodyColor)
                    .frame(width: size * 0.15, height: size * 0.15)

                Ellipse()
                    .fill(Color.white.opacity(0.55))
                    .frame(width: size * 0.4, height: size * 0.16)
                    .rotationEffect(.degrees(rightWingAngle), anchor: .leading)
            }
        }
        .frame(width: size, height: size)
        .offset(y: bobOffset)
        .background(MoteSwarm(state: state, size: size, glow: glowColor))
        .onAppear { animate(for: state) }
        .onChange(of: state) { newState in animate(for: newState) }
    }

    private func animate(for state: FireflyState) {
        switch state {
        case .idle:
            withAnimation(.easeInOut(duration: 2.4).repeatForever(autoreverses: true)) {
                bobOffset = -4
            }
            withAnimation(.easeInOut(duration: 2.25).repeatForever(autoreverses: true)) {
                haloScale = 1.05
                haloOpacity = 0.85
            }
            withAnimation(.easeInOut(duration: 1.8).repeatForever(autoreverses: true)) {
                leftWingAngle = -14
                rightWingAngle = 14
            }

        case .thinking:
            // Faster than idle, but a controlled hum rather than a flutter —
            // busy without feeling anxious.
            withAnimation(.easeInOut(duration: 1.1).repeatForever(autoreverses: true)) {
                bobOffset = -6
            }
            withAnimation(.easeInOut(duration: 0.62).repeatForever(autoreverses: true)) {
                haloScale = 1.15
                haloOpacity = 1
            }
            withAnimation(.easeInOut(duration: 0.34).repeatForever(autoreverses: true)) {
                leftWingAngle = -20
                rightWingAngle = 20
            }

        case .error:
            withAnimation(.easeInOut(duration: 1.7).repeatForever(autoreverses: true)) {
                bobOffset = -1
            }
            withAnimation(.easeInOut(duration: 1.4).repeatForever(autoreverses: true)) {
                haloScale = 0.98
                haloOpacity = 0.68
            }
            withAnimation(.easeInOut(duration: 0.4)) {
                leftWingAngle = -4
                rightWingAngle = 4
            }
        }
    }
}

#Preview {
    FireflyView(state: .thinking, size: 80)
        .padding(40)
        .background(Theme.windowBackground)
}


/// Three small motes orbiting the firefly — the detail that turns a glowing
/// icon into something that reads as *alive in a space* rather than a shape
/// with a pulse on it.
///
/// Sits BEHIND the firefly (used as a `.background`), so the motes pass
/// behind the body rather than over it, which is what sells the depth. Each
/// one rides its own ellipse at its own period, and each leaves a short
/// fading trail built from a handful of progressively smaller, dimmer
/// echoes along the path it just travelled.
///
/// Driven by a single TimelineView, like BackgroundView, rather than
/// per-mote repeating state animations: one clock, no phase drift, and it
/// suspends when the view stops rendering.
private struct MoteSwarm: View {
    let state: FireflyState
    let size: CGFloat
    let glow: Color

    /// Orbit definitions: (radiusX, radiusY, period, phase).
    /// Non-harmonic periods so the arrangement never visibly repeats.
    private static let orbits: [(rx: Double, ry: Double, period: Double, phase: Double)] = [
        (0.78, 0.46, 6.9,  0.0),
        (0.58, 0.72, 9.3,  2.1),
        (0.92, 0.60, 12.7, 4.3),
    ]

    private var speed: Double {
        switch state {
        case .idle: return 1.0
        case .thinking: return 2.3   // they scatter and hurry while working
        case .error: return 0.35     // almost still
        }
    }

    private var visibility: Double {
        state == .error ? 0.3 : 1.0
    }

    private let trailCount = 4

    var body: some View {
        // 24fps: the motes move fast enough to need more than the
        // background's 12, but this is a 40pt area -- 30 buys nothing the
        // eye can see here.
        TimelineView(.animation(minimumInterval: 1.0 / 24.0)) { timeline in
            let t = timeline.date.timeIntervalSinceReferenceDate * speed
            Canvas { ctx, canvas in
                let cx = canvas.width / 2, cy = canvas.height / 2
                for orbit in Self.orbits {
                    for echo in 0..<trailCount {
                        // Each echo samples the orbit slightly in the past,
                        // so the trail traces the real path rather than a
                        // straight smear behind the mote.
                        let lag = Double(echo) * 0.055
                        let a = (t - lag) / orbit.period * 2 * .pi + orbit.phase
                        let x = cx + CGFloat(cos(a) * orbit.rx) * size * 0.5
                        let y = cy + CGFloat(sin(a) * orbit.ry) * size * 0.5

                        let fade = 1.0 - Double(echo) / Double(trailCount)
                        let r = size * 0.032 * CGFloat(0.45 + 0.55 * fade)
                        let rect = CGRect(x: x - r, y: y - r, width: r * 2, height: r * 2)

                        ctx.fill(
                            Path(ellipseIn: rect),
                            with: .color(glow.opacity(0.55 * fade * fade * visibility))
                        )
                    }
                }
            }
            .blendMode(.plusLighter)
            .blur(radius: size * 0.012)
        }
        .frame(width: size * 1.9, height: size * 1.9)
        .allowsHitTesting(false)
        .animation(Theme.Motion.settle, value: state)
    }
}
