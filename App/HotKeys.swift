import AppKit
import Carbon.HIToolbox
import Combine

/// Jugnu's keyboard shortcuts.
///
///   ⌥Space (global, configurable)   open Jugnu from any app, attaching the
///                                   text selected there, if any. Pressed
///                                   while Jugnu is in front, hides it.
///   hold ⌥ (while Jugnu is in front) talk: Jugnu listens while Option is
///                                   held and sends what you said when you
///                                   let go.
///
/// The global shortcut uses Carbon's RegisterEventHotKey: no Accessibility
/// or Input Monitoring permission, and the keystroke doesn't also reach the
/// app in front. Hold-to-talk uses a local event monitor, so it only ever
/// sees keys pressed in Jugnu's own window -- holding Option in other apps
/// (Option-drag, special characters) is never affected.
@MainActor
final class HotKeys: ObservableObject {
    static let shared = HotKeys()

    /// Set when the shortcut couldn't be registered -- usually because
    /// another app (ChatGPT, Raycast...) already uses it. Shown in Settings.
    @Published private(set) var problem: String?

    private var ref: EventHotKeyRef?
    private var handler: EventHandlerRef?
    private let talk = HoldOptionToTalk()

    private init() {}

    func install() {
        if handler == nil {
            var events = [EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))]
            InstallEventHandler(GetApplicationEventTarget(), { _, _, _ in
                Task { @MainActor in HotKeys.shared.openPressed() }
                return noErr
            }, events.count, &events, nil, &handler)
        }
        reload()
        talk.install()
    }

    /// Re-registers the shortcut from Settings.
    func reload() {
        if let ref { UnregisterEventHotKey(ref) }
        ref = nil
        problem = nil
        let preset = SettingsStore.shared.openHotkey
        guard let (keyCode, modifiers) = Self.combo(preset) else { return }
        var newRef: EventHotKeyRef?
        let status = RegisterEventHotKey(keyCode, modifiers, EventHotKeyID(signature: OSType(0x4A474E55), id: 1),
                                         GetApplicationEventTarget(), 0, &newRef)
        if status == noErr, let newRef {
            ref = newRef
        } else {
            problem = "\(preset.label) is already used by another app (ChatGPT and Raycast use it by default). "
                + "Pick another shortcut, or change it in that app."
        }
    }

    private static func combo(_ preset: SettingsStore.HotkeyPreset) -> (UInt32, UInt32)? {
        let space = UInt32(kVK_Space), j = UInt32(kVK_ANSI_J)
        switch preset {
        case .optionSpace: return (space, UInt32(optionKey))
        case .controlSpace: return (space, UInt32(controlKey))
        case .optionShiftSpace: return (space, UInt32(optionKey | shiftKey))
        case .controlShiftSpace: return (space, UInt32(controlKey | shiftKey))
        case .commandShiftSpace: return (space, UInt32(cmdKey | shiftKey))
        case .commandShiftJ: return (j, UInt32(cmdKey | shiftKey))
        case .off: return nil
        }
    }

    private func openPressed() {
        if WindowManager.shared.isChatFrontmost {
            WindowManager.shared.hideChat()
        } else {
            // Reads the selection from the app in front before Jugnu takes
            // focus, then opens with it attached (or just opens).
            AppState.shared.captureSelection()
        }
    }
}

/// Hold Option in Jugnu's window to talk; let go to send.
@MainActor
private final class HoldOptionToTalk {
    private var monitor: Any?
    private var optionDown = false
    private var talking = false
    private var holdTimer: Timer?
    private var releaseWatch: Timer?

    /// How long Option must be held alone before it counts as "talk" -- long
    /// enough that Option-Return, Option-arrows and ⌥-letter never trigger it.
    private static let holdThreshold: TimeInterval = 0.3

    func install() {
        guard monitor == nil else { return }
        monitor = NSEvent.addLocalMonitorForEvents(matching: [.flagsChanged, .keyDown, .leftMouseDown, .rightMouseDown]) {
            [weak self] event in
            self?.handle(event)
            return event
        }
    }

    private func handle(_ event: NSEvent) {
        guard SettingsStore.shared.holdOptionToTalk else { return }
        switch event.type {
        case .flagsChanged:
            let mods = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
                .subtracting([.capsLock, .numericPad, .function])
            if mods == [.option] {
                // A fresh press starts a hold. (Keyed off the timer rather than
                // a remembered "down" flag, which could stick if the release
                // happened in another app.)
                guard !talking, holdTimer == nil, WindowManager.shared.isChatFrontmost else { return }
                optionDown = true
                holdTimer = Timer.scheduledTimer(withTimeInterval: Self.holdThreshold, repeats: false) { _ in
                    Task { @MainActor in self.startTalking() }
                }
            } else if !mods.contains(.option) {
                optionDown = false
                cancelHold()
                if talking { finishTalking() }
            } else if !talking {
                cancelHold()  // another modifier joined: it's a shortcut, not a hold
            }
        case .keyDown, .leftMouseDown, .rightMouseDown:
            if optionDown && !talking { cancelHold() }  // ⌥-letter, Option-click...
        default:
            break
        }
    }

    private func cancelHold() {
        holdTimer?.invalidate()
        holdTimer = nil
    }

    private func startTalking() {
        holdTimer = nil
        guard optionDown, NSEvent.modifierFlags.contains(.option), WindowManager.shared.isChatFrontmost else { return }
        talking = true
        NotificationCenter.default.post(name: .jugnuStartDictation, object: nil)
        // The release can land somewhere else -- the first-use microphone
        // prompt takes focus, or the user clicks away -- so also watch the
        // key's real state, which needs no permission and no focus.
        releaseWatch?.invalidate()
        releaseWatch = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { _ in
            Task { @MainActor in
                if !NSEvent.modifierFlags.contains(.option) { self.finishTalking() }
            }
        }
    }

    private func finishTalking() {
        releaseWatch?.invalidate()
        releaseWatch = nil
        optionDown = false
        guard talking else { return }
        talking = false
        NotificationCenter.default.post(name: .jugnuStopDictationAndSend, object: nil)
    }
}

extension Notification.Name {
    static let jugnuStartDictation = Notification.Name("jugnuStartDictation")
    static let jugnuStopDictationAndSend = Notification.Name("jugnuStopDictationAndSend")
}
