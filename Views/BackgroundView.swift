import SwiftUI

/// The app's backdrop: a dark ground with warm light moving slowly through
/// it, like a firefly's glow carrying through dusk air.
///
/// Built in layers, back to front:
///   1. Real window vibrancy (the window is configured non-opaque, so
///      without a material behind everything that configuration is inert).
///   2. A base gradient, near-opaque, so this reads as frosted glass at the
///      edges rather than a see-through window.
///   3. Three drifting bloom orbs on independent elliptical paths — the
///      layer that makes the surface feel alive rather than decorated.
///   4. A caustic sweep: a wide diagonal band of light that drifts across,
///      the way light moves over a dark surface.
///   5. Film grain, so it reads as material rather than a digital gradient.
///   6. A hairline bevel along the top edge.
///
/// Everything is driven by ONE TimelineView clock rather than a dozen
/// independent `withAnimation(.repeatForever)` state animations. That
/// matters: repeating state animations each own a timer, drift out of phase
/// with each other, and keep running when the window is hidden. A single
/// timeline is one tick, everything stays in phase by construction, and it
/// suspends when the view isn't rendering.
///
/// The whole field reacts to what Jugnu is doing — see `intensity`.
struct BackgroundView: View {
    var state: FireflyState = .idle

    /// How energetic the backdrop is. Thinking lifts and quickens it;
    /// an error cools it and drains most of the warmth out.
    private var intensity: Double {
        switch state {
        case .idle:     return 1.0
        case .thinking: return 1.65
        case .error:    return 0.55
        }
    }

    private var warm: Color {
        state == .error ? Theme.danger : Theme.clay
    }

    /// Seconds per full cycle of the slowest orb. Thinking speeds the whole
    /// field up proportionally rather than restarting it.
    private var timeScale: Double {
        switch state {
        case .idle:     return 1.0
        case .thinking: return 2.1
        case .error:    return 0.6
        }
    }

    var body: some View {
        ZStack {
            VisualEffectBackground(material: .hudWindow, blendingMode: .behindWindow)

            LinearGradient(
                colors: [Theme.windowBackground.opacity(0.94), Theme.deepPanel.opacity(0.96)],
                startPoint: .topLeading,
                endPoint: .bottomTrailing
            )

            // 12fps, not 30. Every orb below moves on a 13-29 second
            // period; at that speed 12fps is indistinguishable from 30 and
            // costs less than half as much. This is an app that sits open
            // in the menu bar all day -- idle cost is the number that
            // matters, not peak smoothness.
            TimelineView(.animation(minimumInterval: 1.0 / 12.0)) { timeline in
                let t = timeline.date.timeIntervalSinceReferenceDate * timeScale

                GeometryReader { geo in
                    let w = geo.size.width, h = geo.size.height

                    ZStack {
                        // --- drifting blooms -----------------------------
                        // Each orb travels its own ellipse at its own rate,
                        // with the periods deliberately non-harmonic so the
                        // composition never visibly loops.
                        bloom(warm.opacity(0.135 * intensity),
                              radius: max(w, h) * 0.62,
                              x: w * (0.50 + 0.17 * cos(t / 17.0)),
                              y: h * (0.30 + 0.10 * sin(t / 13.0)))

                        bloom(warm.opacity(0.115 * intensity),
                              radius: max(w, h) * 0.55,
                              x: w * (0.24 + 0.15 * sin(t / 23.0)),
                              y: h * (0.66 + 0.12 * cos(t / 19.0)))

                        // Third orb is cooler and dimmer — a hint of
                        // complementary colour keeps the warm ones from
                        // flattening into a single orange wash.
                        bloom(Color(hex: 0x6E86B8).opacity(0.080 * intensity),
                              radius: max(w, h) * 0.50,
                              x: w * (0.80 + 0.13 * cos(t / 29.0)),
                              y: h * (0.78 + 0.11 * sin(t / 21.0)))

                        // --- caustic sweep --------------------------------
                        // A soft diagonal band crossing the window on a long
                        // period. Reads as light moving across a surface;
                        // plusLighter keeps it additive so it never muddies
                        // what's underneath.
                        // Softness comes from the gradient's own stops
                        // rather than a .blur(radius: 34) pass. A blur that
                        // size, over a layer 2.4x the window, re-rendered
                        // every frame, was the most expensive thing on
                        // screen -- and visually indistinguishable from
                        // simply ramping the stops.
                        LinearGradient(
                            stops: [
                                .init(color: .clear, location: 0.00),
                                .init(color: Color.white.opacity(0.010 * intensity), location: 0.30),
                                .init(color: Color.white.opacity(0.028 * intensity), location: 0.50),
                                .init(color: Color.white.opacity(0.010 * intensity), location: 0.70),
                                .init(color: .clear, location: 1.00),
                            ],
                            startPoint: .topLeading, endPoint: .bottomTrailing
                        )
                        .frame(width: w * 2.0, height: h * 2.0)
                        .rotationEffect(.degrees(28))
                        .offset(x: w * (-0.6 + 1.2 * fract(t / 41.0)),
                                y: h * (-0.2 + 0.4 * fract(t / 53.0)))
                        .blendMode(.plusLighter)
                    }
                    .animation(Theme.Motion.settle, value: state)
                }
            }
            // No .drawingGroup(): with the blur removed these are plain
            // gradients that Core Animation composites on the GPU directly.
            // Forcing an offscreen rasterization of the whole window every
            // frame was costing more than it saved.

            GrainOverlay()
        }
        .overlay(alignment: .top) {
            LinearGradient(
                colors: [Color.white.opacity(0.16), Color.white.opacity(0)],
                startPoint: .top, endPoint: .bottom
            )
            .frame(height: 1)
        }
        .ignoresSafeArea()
    }

    private func bloom(_ color: Color, radius: CGFloat, x: CGFloat, y: CGFloat) -> some View {
        RadialGradient(colors: [color, color.opacity(0)],
                       center: .center, startRadius: 0, endRadius: radius)
            .frame(width: radius * 2, height: radius * 2)
            .position(x: x, y: y)
            .blendMode(.plusLighter)
    }

    /// Fractional part, for looping a value in 0..<1 without a modulo on a
    /// growing timestamp losing precision.
    private func fract(_ v: Double) -> Double { v - v.rounded(.down) }
}

/// A fixed, precomputed speckle of near-invisible dots -- the difference
/// between a flat gradient and a surface that feels like it has grain to
/// it, the same trick behind film-grain overlays in most polished dark
/// UIs (it hides gradient banding as a side effect, too).
///
/// Positions come from a deterministic sine-hash rather than
/// `Double.random`, so the field is identical every launch instead of
/// subtly re-rolling the texture each time, and the two passes below are
/// built once as static constants rather than regenerated per frame -- so
/// several hundred points cost two Canvas fill calls total, not hundreds
/// of individual draws.
private struct GrainOverlay: View {
    private static func hash(_ seed: Double) -> Double {
        let v = sin(seed) * 43758.5453
        return v - v.rounded(.down)
    }

    private static func points(count: Int, seedOffset: Double) -> [(x: Double, y: Double)] {
        (0..<count).map { i in
            let n = (Double(i) + seedOffset) * 12.9898
            return (x: hash(n), y: hash(n * 1.618))
        }
    }

    // Unit-space (0...1) positions, scaled to the real canvas size at
    // draw time -- one field that looks right at any window size.
    private static let fine = points(count: 500, seedOffset: 0)
    private static let bright = points(count: 140, seedOffset: 777)

    private static func path(_ pts: [(x: Double, y: Double)], in size: CGSize) -> Path {
        var path = Path()
        for p in pts {
            path.addEllipse(in: CGRect(x: p.x * size.width, y: p.y * size.height, width: 1, height: 1))
        }
        return path
    }

    var body: some View {
        Canvas { context, size in
            context.fill(Self.path(Self.fine, in: size), with: .color(.white.opacity(0.02)))
            context.fill(Self.path(Self.bright, in: size), with: .color(.white.opacity(0.05)))
        }
        .allowsHitTesting(false)
        .blendMode(.plusLighter)
    }
}

#Preview {
    BackgroundView(state: .thinking)
        .frame(width: Theme.WindowSize.min.width, height: Theme.WindowSize.min.height)
}
