import SwiftUI
import AppKit

struct ContentView: View {
    @StateObject private var appState = AppState()
    @State private var showHistory = false

    var body: some View {
        ZStack {
            BackgroundView()

            VStack(spacing: 0) {
                TitleBarView(appState: appState, showHistory: $showHistory)
                HeroView(appState: appState)
                InputBarView(appState: appState)
            }

            if showHistory {
                HistoryView(appState: appState, isPresented: $showHistory)
                    .transition(.move(edge: .top).combined(with: .opacity))
            }
        }
        .frame(minWidth: Theme.WindowSize.min.width, minHeight: Theme.WindowSize.min.height)
        .background(
            WindowAccessor { window in
                window.titlebarAppearsTransparent = true
                window.titleVisibility = .hidden
                window.isMovableByWindowBackground = true
                window.minSize = NSSize(width: Theme.WindowSize.min.width, height: Theme.WindowSize.min.height)
                window.isOpaque = false
                window.backgroundColor = .clear
                // Every color in Theme is a hardcoded dark hex, but nothing
                // told AppKit that -- so a system running in Light Mode would
                // still draw native chrome (tooltips, the traffic-light
                // surround, menu highlighting) in a mismatched appearance
                // around Jugnu's own dark UI. Forcing it keeps the window
                // internally consistent regardless of system appearance.
                window.appearance = NSAppearance(named: .darkAqua)

                // Floating, not the default .normal level: this is meant to
                // behave like a quick-access panel (Spotlight/Raycast-style)
                // that stays reachable above whatever else is on screen, and
                // .canJoinAllSpaces/.fullScreenAuxiliary let it follow you to
                // whichever Space -- including over a full-screen app -- is
                // active, instead of staying pinned to the one it launched
                // on. Importantly, .floating (level 3) sits well below the
                // real system menu bar (.mainMenu is 24, .statusBar is 25)
                // and every other piece of system chrome, so this can never
                // cover or intercept clicks meant for the actual menu bar --
                // only a level at or above those would do that.
                //
                // If the menu bar was genuinely unreachable with this window
                // frontmost before, this is the standard fix for that class
                // of bug. One caveat: this file set doesn't include an
                // App/AppDelegate entry point, which is the more usual place
                // `.level`/`NSApp.presentationOptions` get set -- if
                // something there also touches either of those after this
                // closure runs, it can override what's set here.
                window.level = .floating
                window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]

                // AppKit remembers size + position across launches under this
                // name, with zero extra bookkeeping required.
                window.setFrameAutosaveName("JugnuMainWindow")
            }
        )
        .onAppear(perform: launchBackend)
    }

    // MARK: - Backend

    private func launchBackend() {
        guard let scriptPath = Bundle.main.path(forResource: "bridge", ofType: "py", inDirectory: "PythonBackend") else {
            appState.errorMessage =
                "Couldn't find bridge.py in the app bundle. Make sure the " +
                "PythonBackend folder was added as a folder reference (blue " +
                "icon) in Xcode, not a group."
            appState.statusLabel = "error"
            return
        }
        let workingDirectory = (scriptPath as NSString).deletingLastPathComponent
        appState.launch(bridgeScriptPath: scriptPath, workingDirectory: workingDirectory)
    }
}
