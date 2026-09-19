import SwiftUI

extension Notification.Name {
    static let jugnuClearConversation = Notification.Name("jugnuClearConversation")
}
@main
struct JugnuApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    
    var body: some Scene {
        WindowGroup {
            ContentView()
        }
        .windowStyle(.hiddenTitleBar)
        .windowResizability(.contentSize)
        .defaultSize(width: Theme.WindowSize.min.width, height: Theme.WindowSize.min.height)
        .commands {
            // Single-window app by design — no reason to offer Cmd+N.
            CommandGroup(replacing: .newItem) {}
            
            CommandMenu("Conversation") {
                Button("Clear Conversation") {
                    NotificationCenter.default.post(name: .jugnuClearConversation, object: nil)
                }
                .keyboardShortcut("k", modifiers: .command)
            }
        }

        // Standard SwiftUI preferences window -- this alone wires up
        // Cmd+, and the app menu's "Settings…" item automatically.
        // MenuBarController's right-click menu also gets an explicit
        // "Settings…" entry, since Jugnu being an accessory (no Dock
        // icon) app means the app-menu route is less obviously
        // discoverable than it would be for a regular app.
        Settings {
            SettingsRootView()
        }
    }
}
