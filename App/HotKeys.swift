import AppKit
import Carbon.HIToolbox

/// System-wide shortcuts (Settings › General › Shortcuts):
///
///   open       tap: show or hide Jugnu.  hold: talk -- Jugnu listens while
///              the keys are held and sends what you said when you let go.
///   selection  opens Jugnu with the text selected in the frontmost app
///              attached, ready for "summarize", "reply", "translate"...
///
/// Uses Carbon's RegisterEventHotKey, which needs no Accessibility or Input
/// Monitoring permission and consumes the keystroke so it doesn't also
/// type into the app in front.
@MainActor
final class HotKeys {
    static let shared = HotKeys()

    private enum ID: UInt32 { case open = 1, selection = 2 }

    private var refs: [EventHotKeyRef] = []
    private var handler: EventHandlerRef?
    private var pressedAt: Date?
    private var holdTimer: Timer?
    private var talking = false

    /// How long the open shortcut has to be held before it means "talk".
    private static let holdThreshold: TimeInterval = 0.35

    private init() {}

    func install() {
        guard handler == nil else { return }
        var events = [EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed)),
                      EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyReleased))]
        InstallEventHandler(GetApplicationEventTarget(), { _, event, _ in
            var hotKey = EventHotKeyID()
            GetEventParameter(event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID),
                              nil, MemoryLayout<EventHotKeyID>.size, nil, &hotKey)
            let pressed = GetEventKind(event) == UInt32(kEventHotKeyPressed)
            let id = hotKey.id
            Task { @MainActor in HotKeys.shared.handle(id: id, pressed: pressed) }
            return noErr
        }, events.count, &events, nil, &handler)
        reload()
    }

    /// Re-registers both shortcuts from Settings.
    func reload() {
        refs.forEach { UnregisterEventHotKey($0) }
        refs.removeAll()
        let settings = SettingsStore.shared
        register(settings.openHotkey, id: .open)
        if settings.selectionHotkey != settings.openHotkey {
            register(settings.selectionHotkey, id: .selection)
        }
    }

    private func register(_ preset: SettingsStore.HotkeyPreset, id: ID) {
        guard let (keyCode, modifiers) = Self.combo(preset) else { return }
        var ref: EventHotKeyRef?
        let status = RegisterEventHotKey(keyCode, modifiers, EventHotKeyID(signature: OSType(0x4A474E55), id: id.rawValue),
                                         GetApplicationEventTarget(), 0, &ref)
        if status == noErr, let ref {
            refs.append(ref)
        } else {
            print("[HotKeys] couldn't register \(preset.label) (it may be taken by another app): \(status)")
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

    // MARK: - Actions

    private func handle(id: UInt32, pressed: Bool) {
        switch ID(rawValue: id) {
        case .open: pressed ? openPressed() : openReleased()
        case .selection: if pressed { AppState.shared.captureSelection() }
        case .none: break
        }
    }

    private func openPressed() {
        pressedAt = Date()
        holdTimer?.invalidate()
        holdTimer = Timer.scheduledTimer(withTimeInterval: Self.holdThreshold, repeats: false) { _ in
            Task { @MainActor in HotKeys.shared.startTalking() }
        }
    }

    private func startTalking() {
        guard pressedAt != nil else { return }
        talking = true
        WindowManager.shared.showChat()
        NotificationCenter.default.post(name: .jugnuStartDictation, object: nil)
    }

    private func openReleased() {
        holdTimer?.invalidate()
        holdTimer = nil
        pressedAt = nil
        if talking {
            talking = false
            NotificationCenter.default.post(name: .jugnuStopDictationAndSend, object: nil)
        } else {
            WindowManager.shared.toggleChat()
        }
    }
}

extension Notification.Name {
    static let jugnuStartDictation = Notification.Name("jugnuStartDictation")
    static let jugnuStopDictationAndSend = Notification.Name("jugnuStopDictationAndSend")
}
