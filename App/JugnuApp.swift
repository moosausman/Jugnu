import SwiftUI

@main
struct JugnuApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        // Jugnu's windows are created by WindowManager, so the conversation
        // and the backend outlive any window being closed. This scene only
        // exists to carry the menu bar commands; "Settings…" is rerouted to
        // Jugnu's own Settings window below, so it never opens.
        Settings { EmptyView() }
            .commands { JugnuCommands(appState: AppState.shared) }
    }
}

/// Jugnu's menus while it's the frontmost app.
struct JugnuCommands: Commands {
    @ObservedObject var appState: AppState

    var body: some Commands {
        CommandGroup(replacing: .appSettings) {
            Button("Settings…") { WindowManager.shared.showSettings() }
                .keyboardShortcut(",", modifiers: .command)
        }
        CommandGroup(replacing: .newItem) {
            Button("New Conversation") { appState.clear() }
                .keyboardShortcut("n", modifiers: .command)
                .disabled(appState.thinking || appState.messages.isEmpty)
        }
        CommandGroup(before: .toolbar) {
            Button("Activity") { WindowManager.shared.toggleActivity() }
                .keyboardShortcut("j", modifiers: .command)
        }
        CommandMenu("Conversation") {
            Button("Stop") { appState.cancelRun() }
                .keyboardShortcut(".", modifiers: .command)
                .disabled(!appState.thinking)
            Button("Daily Review") {
                WindowManager.shared.showChat()
                appState.requestDailyReview()
            }
            .disabled(appState.thinking)
        }
        CommandGroup(replacing: .help) {}
    }
}
