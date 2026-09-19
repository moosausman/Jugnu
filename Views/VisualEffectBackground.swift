import SwiftUI
import AppKit

/// A thin bridge onto NSVisualEffectView, giving BackgroundView real
/// vibrancy -- a true frosted blur of whatever is behind the window --
/// instead of only an opaque gradient sitting in front of it.
///
/// ContentView already configures the window as non-opaque with a clear
/// background (see WindowAccessor's use there): that's the setup for
/// exactly this, and without a material view behind the gradient, that
/// configuration was doing nothing. `.hudWindow` is also always rendered
/// dark by AppKit regardless of the system's appearance, which happens to
/// match Jugnu's palette without any extra plumbing.
struct VisualEffectBackground: NSViewRepresentable {
    var material: NSVisualEffectView.Material = .hudWindow
    var blendingMode: NSVisualEffectView.BlendingMode = .behindWindow

    func makeNSView(context: Context) -> NSVisualEffectView {
        let view = NSVisualEffectView()
        view.material = material
        view.blendingMode = blendingMode
        view.state = .active
        view.isEmphasized = false
        return view
    }

    func updateNSView(_ nsView: NSVisualEffectView, context: Context) {
        nsView.material = material
        nsView.blendingMode = blendingMode
    }
}
