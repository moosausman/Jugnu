import SwiftUI
import AppKit

extension Notification.Name {
    /// Posted once, the first time WindowAccessor sees the real NSWindow
    /// behind whichever view it's attached to. AppDelegate listens for
    /// this to hand MenuBarController a window reference it can trust,
    /// instead of guessing from NSApp.windows.
    static let jugnuMainWindowResolved = Notification.Name("jugnuMainWindowResolved")
}

/// Drop this into a view hierarchy (as `.background(WindowAccessor())`,
/// optionally with a `configure` closure) to get a one-time callback --
/// and a `.jugnuMainWindowResolved` notification -- with the real
/// NSWindow once SwiftUI has attached the view to it. This is more
/// reliable than reading `NSApp.windows.first` from an AppDelegate, which
/// can race the window's creation and, in an app with more than one
/// window, isn't even guaranteed to return the right one.
///
/// Recommended: add `.background(WindowAccessor())` to ContentView's root
/// view. Not required -- AppDelegate falls back to polling NSApp.windows
/// on its own -- but this is the belt-and-suspenders, glitch-free path.
struct WindowAccessor: NSViewRepresentable {
    var configure: (NSWindow) -> Void = { _ in }

    func makeNSView(context: Context) -> NSView {
        let view = NSView()
        DispatchQueue.main.async {
            if let window = view.window {
                configure(window)
                NotificationCenter.default.post(name: .jugnuMainWindowResolved, object: window)
            }
        }
        return view
    }

    func updateNSView(_ nsView: NSView, context: Context) {}
}
