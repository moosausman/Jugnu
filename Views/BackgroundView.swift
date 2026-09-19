import SwiftUI

/// The app's backdrop. A quiet, deliberately minimal surface — a dark ground
/// with a single soft bloom of warmth, evoking a firefly's glow at dusk
/// rather than competing for attention with anything drawn on top of it.
///
/// Sits on real vibrancy rather than a flat opaque rectangle: the window is
/// already configured non-opaque with a clear background (see
/// WindowAccessor's use in ContentView), so without a material behind the
/// gradient that configuration was inert. The gradient stays high-opacity
/// on top of it -- this is a hint of frosted glass at the edges, not a
/// see-through window.
///
/// Three small additions beyond the original gradient: a near-invisible
/// grain (see GrainOverlay below) so the surface reads as material rather
/// than a flat digital gradient, a hairline highlight along the very top
/// edge for a bit of glass-bevel depth, and a very slow breathing pulse on
/// the warm bloom -- the same idle rhythm FireflyView already gives the
/// mascot, applied here so the whole window feels like one living thing
/// rather than a static backdrop behind an animated icon.
struct BackgroundView: View {
    @State private var bloomScale: CGFloat = 1
    @State private var bloomOpacity: Double = 1

    var body: some View {
        ZStack {
            VisualEffectBackground(material: .hudWindow, blendingMode: .behindWindow)

            LinearGradient(
                colors: [Theme.windowBackground.opacity(0.92), Theme.deepPanel.opacity(0.94)],
                startPoint: .topLeading,
                endPoint: .bottomTrailing
            )

            RadialGradient(
                colors: [Theme.clay.opacity(0.12), Theme.clay.opacity(0)],
                center: UnitPoint(x: 0.5, y: 0.28),
                startRadius: 0,
                endRadius: 360
            )
            .scaleEffect(bloomScale)
            .opacity(bloomOpacity)

            GrainOverlay()
        }
        .overlay(alignment: .top) {
            LinearGradient(
                colors: [Color.white.opacity(0.14), Color.white.opacity(0)],
                startPoint: .top, endPoint: .bottom
            )
            .frame(height: 1)
        }
        .ignoresSafeArea()
        .onAppear {
            withAnimation(Theme.Motion.breathe.repeatForever(autoreverses: true)) {
                bloomScale = 1.08
                bloomOpacity = 0.8
            }
        }
    }
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
    BackgroundView()
        .frame(width: Theme.WindowSize.min.width, height: Theme.WindowSize.min.height)
}
