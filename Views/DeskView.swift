import AppKit
import SwiftUI

/// Jugnu's desk: a live view of the Mac session Jugnu works in, so you can
/// watch it and step in when it fumbles.
///
/// Three things a screen-sharing window can't do, because it doesn't know
/// what the work is:
///   * it captions what Jugnu is doing right now, from the run's own events
///   * it keeps the last couple of minutes, so you can scrub back to the
///     moment something went wrong, with each tool call marked on the line
///   * taking over pauses Jugnu instead of fighting it for the pointer, and
///     letting go hands the task back
struct DeskView: View {
    @ObservedObject var appState: AppState
    @StateObject private var feed = DeskFeed()
    @State private var account = "agent"

    private var run: RunActivity? {
        appState.runs.first { $0.isActive && (account != "agent" || $0.session != "primary") }
            ?? appState.activeRun
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            screen
            timeline
        }
        .background(Theme.windowBackground)
        .onAppear { feed.start(port: account == "agent" ? 8766 : 8765) }
        .onDisappear { feed.stop() }
        // A live view of another macOS session is the most expensive thing
        // Jugnu does. It runs while you're watching and stops when you
        // aren't: another window in front, the app in the background, or
        // the window minimised.
        .onReceive(NotificationCenter.default.publisher(for: NSWindow.didChangeOcclusionStateNotification)) { note in
            if let window = note.object as? NSWindow, window.title == "Jugnu's Desk" {
                feed.paused = !window.occlusionState.contains(.visible)
            }
        }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didResignActiveNotification)) { _ in
            feed.paused = true
        }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in
            feed.paused = false
        }
        .onChange(of: account) { _, new in
            feed.start(port: new == "agent" ? 8766 : 8765)
        }
        .onChange(of: feed.controlling) { _, taken in
            // Polite handover: Jugnu stops at its next step rather than
            // wrestling the pointer, and picks up again when you let go.
            guard let id = run?.id else { return }
            taken ? appState.pauseRun(id) : appState.resumeRun(id)
        }
    }

    // MARK: Header

    private var header: some View {
        HStack(spacing: 8) {
            FireflyView(state: feed.controlling ? .idle : (run != nil ? .thinking : .idle), size: 15)
            Text("Jugnu's desk").font(Theme.text(12.5, weight: .semibold)).foregroundStyle(Theme.ink)

            Picker("", selection: $account) {
                Text("Jugnu account").tag("agent")
                Text("Your screen").tag("primary")
            }
            .labelsHidden()
            .fixedSize()
            .controlSize(.small)

            statusChip
            Spacer(minLength: 8)

            if run != nil {
                Button(feed.controlling ? "Give it back" : "Take over") { feed.controlling.toggle() }
                    .buttonStyle(JugnuButtonStyle(kind: feed.controlling ? .primary : .secondary, compact: true))
                Button("Stop task") { appState.cancelRun(run?.id) }
                    .buttonStyle(JugnuButtonStyle(kind: .destructive, compact: true))
            } else {
                Button(feed.controlling ? "Done" : "Control") { feed.controlling.toggle() }
                    .buttonStyle(JugnuButtonStyle(kind: feed.controlling ? .primary : .secondary, compact: true))
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(Theme.panel)
        .overlay(alignment: .bottom) { Rectangle().fill(Theme.border).frame(height: 1) }
        .zIndex(1)
    }

    @ViewBuilder
    private var statusChip: some View {
        switch feed.state {
        case .live where feed.isReplaying:
            chip("Replay", Theme.clay)
        case .live:
            chip("Live", Theme.success)
        case .connecting:
            chip("Connecting…", Theme.inkFaint)
        case .idle where feed.paused:
            chip("Paused", Theme.inkFaint)
        case .idle:
            chip("Off", Theme.inkFaint)
        case .stopped:
            chip("Not answering", Theme.danger)
        }
    }

    private func chip(_ text: String, _ tint: Color) -> some View {
        HStack(spacing: 4) {
            Circle().fill(tint).frame(width: 5, height: 5)
            Text(text).font(Theme.text(10.5, weight: .medium)).foregroundStyle(tint)
        }
        .padding(.horizontal, 6)
        .padding(.vertical, 2)
        .background(Capsule().fill(tint.opacity(0.12)))
    }

    // MARK: The screen itself

    private var screen: some View {
        GeometryReader { geometry in
            ZStack {
                Color.black
                if let image = feed.shown {
                    Image(nsImage: image)
                        .resizable()
                        .interpolation(.medium)
                        .aspectRatio(contentMode: .fit)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                } else if case .stopped(let why) = feed.state {
                    message(why)
                } else {
                    message(account == "agent"
                            ? "Waiting for the Jugnu account…" : "Waiting for your screen…")
                }
                // Only while you're driving: an AppKit view in the stack
                // paints over SwiftUI siblings (it hid part of the caption),
                // and it has no job when Jugnu is the one working.
                if feed.controlling && !feed.isReplaying {
                    DeskSurface(feed: feed, size: geometry.size)
                }
            }
            .frame(width: geometry.size.width, height: geometry.size.height)
            .onChange(of: geometry.size.width) { _, width in
                feed.requestedWidth = max(480, min(1400, Int(width * 1.5)))
            }
            .overlay {
                // Laid out as its own stack rather than two aligned
                // overlays: an overlay sized the caption to one line and
                // let the rest spill onto the picture.
                VStack(spacing: 0) {
                    controlBanner
                    Spacer(minLength: 0)
                    HStack(spacing: 0) {
                        caption
                        Spacer(minLength: 0)
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func message(_ text: String) -> some View {
        Text(text)
            .font(Theme.text(12))
            .foregroundStyle(Theme.inkFaint)
            .multilineTextAlignment(.center)
            .padding(24)
    }

    /// What Jugnu is doing, in words, over the picture.
    @ViewBuilder
    private var caption: some View {
        if let run {
            VStack(alignment: .leading, spacing: 3) {
                Text(run.objective.isEmpty ? run.goal : run.objective)
                    .font(Theme.text(12, weight: .medium))
                    .foregroundStyle(Theme.ink)
                    .lineLimit(1)
                Text(detail(of: run))
                    .font(Theme.text(11))
                    .foregroundStyle(Theme.inkDim)
                    .lineLimit(1)
            }
            .padding(9)
            .background(RoundedRectangle(cornerRadius: 9, style: .continuous).fill(Color.black.opacity(0.78)))
            .overlay(RoundedRectangle(cornerRadius: 9, style: .continuous).stroke(Theme.border, lineWidth: 1))
            .padding(10)
        }
    }

    private func detail(of run: RunActivity) -> String {
        var parts: [String] = []
        if !run.tasks.isEmpty {
            parts.append("step \(min(run.completedTasks + 1, run.tasks.count)) of \(run.tasks.count)")
        }
        if let tool = run.tools.last {
            parts.append(tool.summary.isEmpty ? tool.tool : tool.summary)
        } else if let line = appState.progressLine {
            parts.append(line)
        }
        return parts.joined(separator: " · ")
    }

    @ViewBuilder
    private var controlBanner: some View {
        if feed.controlling {
            Text(run == nil ? "You're driving this desktop"
                 : "You're driving — Jugnu is paused and will carry on when you hand it back")
                .font(Theme.text(11, weight: .medium))
                .foregroundStyle(Color(hex: 0x1A120C))
                .padding(.horizontal, 10)
                .padding(.vertical, 4)
                .background(Capsule().fill(Theme.clay))
                .padding(.top, 8)
        }
    }

    // MARK: Replay

    private var timeline: some View {
        HStack(spacing: 10) {
            Button(feed.isReplaying ? "Back to live" : "Live") {
                feed.replayIndex = nil
            }
            .buttonStyle(JugnuButtonStyle(kind: feed.isReplaying ? .primary : .secondary, compact: true))
            .disabled(!feed.isReplaying)
            .opacity(feed.isReplaying ? 1 : 0.5)

            Slider(value: Binding(
                get: { Double(feed.replayIndex ?? max(0, feed.history.count - 1)) },
                set: { value in
                    let index = Int(value.rounded())
                    feed.replayIndex = index >= feed.history.count - 1 ? nil : index
                }
            ), in: 0...Double(max(1, feed.history.count - 1)))
            .disabled(feed.history.count < 2)
            // A GeometryReader would take every spare point of height here,
            // so the marks sit in a fixed strip under the track.
            .overlay(alignment: .bottom) { marks.frame(height: 8) }

            Text(scrubLabel)
                .font(Theme.mono(10.5))
                .foregroundStyle(Theme.inkFaint)
                .frame(width: 62, alignment: .trailing)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 7)
        .background(Theme.panel)
        .overlay(alignment: .top) { Rectangle().fill(Theme.border).frame(height: 1) }
    }

    /// One tick per tool call, so scrubbing lands on the moment it acted.
    @ViewBuilder
    private var marks: some View {
        GeometryReader { geometry in
            let frames = feed.history
            if let first = frames.first?.at, let last = frames.last?.at, last > first {
                let span = last.timeIntervalSince(first)
                ForEach(run?.tools ?? []) { tool in
                    let offset = tool.started.timeIntervalSince(first)
                    if offset >= 0, offset <= span {
                        Rectangle()
                            .fill(tool.ok == false ? Theme.danger : Theme.clay)
                            .frame(width: 2, height: 7)
                            .offset(x: geometry.size.width * (offset / span), y: 1)
                    }
                }
            }
        }
        .allowsHitTesting(false)
    }

    private var scrubLabel: String {
        guard let index = feed.replayIndex, feed.history.indices.contains(index),
              let last = feed.history.last?.at else { return "live" }
        let seconds = last.timeIntervalSince(feed.history[index].at)
        return seconds < 1 ? "now" : String(format: "-%.0fs", seconds)
    }
}

/// Catches clicks, scrolls and keys while you have control, and forwards
/// them to that desktop through MacBridge.
private struct DeskSurface: NSViewRepresentable {
    @ObservedObject var feed: DeskFeed
    let size: CGSize

    func makeNSView(context: Context) -> InputView {
        let view = InputView()
        view.feed = feed
        view.viewSize = size
        return view
    }

    func updateNSView(_ view: InputView, context: Context) {
        view.feed = feed
        view.viewSize = size
        if feed.controlling, view.window?.firstResponder !== view {
            view.window?.makeFirstResponder(view)
        }
    }

    final class InputView: NSView {
        weak var feed: DeskFeed?
        var viewSize: CGSize = .zero
        private var dragStart: CGPoint?

        override var isFlipped: Bool { true }           // match the screen's top-left origin
        override var acceptsFirstResponder: Bool { true }
        override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
        override func resetCursorRects() { addCursorRect(bounds, cursor: .pointingHand) }

        private func point(_ event: NSEvent) -> CGPoint? {
            let local = convert(event.locationInWindow, from: nil)
            return feed?.desktopPoint(view: local, in: bounds.size)
        }

        override func mouseDown(with event: NSEvent) {
            dragStart = point(event)
        }

        override func mouseUp(with event: NSEvent) {
            guard let end = point(event) else { return }
            if let start = dragStart, hypot(end.x - start.x, end.y - start.y) > 6 {
                feed?.drag(from: start, to: end)
            } else {
                feed?.click(at: end, clicks: min(event.clickCount, 3))
            }
            dragStart = nil
        }

        override func rightMouseUp(with event: NSEvent) {
            if let p = point(event) { feed?.click(at: p, right: true) }
        }

        override func mouseMoved(with event: NSEvent) {
            if let p = point(event) { feed?.move(to: p) }
        }

        override func scrollWheel(with event: NSEvent) {
            guard let p = point(event) else { return }
            feed?.scroll(at: p, dx: Double(event.scrollingDeltaX.rounded()),
                         dy: Double(event.scrollingDeltaY.rounded()))
        }

        override func keyDown(with event: NSEvent) {
            guard let feed else { return }
            let modifiers = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
            var names: [String] = []
            if modifiers.contains(.command) { names.append("cmd") }
            if modifiers.contains(.option) { names.append("option") }
            if modifiers.contains(.control) { names.append("control") }
            if modifiers.contains(.shift) { names.append("shift") }

            if let special = Self.specialKeys[event.keyCode] {
                feed.press(key: special, modifiers: names)
                return
            }
            let characters = event.charactersIgnoringModifiers ?? ""
            if names.contains(where: { $0 != "shift" }), let first = characters.first {
                feed.press(key: String(first), modifiers: names)
            } else if let text = event.characters, !text.isEmpty {
                feed.type(text)
            }
        }

        private static let specialKeys: [UInt16: String] = [
            36: "return", 48: "tab", 49: "space", 51: "delete", 53: "escape",
            123: "left", 124: "right", 125: "down", 126: "up",
            115: "home", 119: "end", 116: "pageup", 121: "pagedown", 117: "forwarddelete",
        ]
    }
}
