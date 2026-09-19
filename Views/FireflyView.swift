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
