import AppKit
import Combine
import Foundation

/// A live picture of one macOS session's screen, straight from MacBridge --
/// what the Desk window shows while Jugnu works in its own account.
///
/// Frames are long-polled: each request waits for the next frame rather
/// than asking on a timer, so a still screen costs nothing and a busy one
/// arrives as fast as it changes. None of this touches a model: it's JPEG
/// over loopback, no API calls and no plan usage.
///
/// The same connection carries input the other way when the user takes
/// over -- a click in the window becomes a click on that desktop.
@MainActor
final class DeskFeed: ObservableObject {
    enum State: Equatable {
        case idle, connecting, live
        case stopped(String)

        var isLive: Bool { self == .live }
    }

    struct Frame: Identifiable, Equatable {
        let id: Int
        let at: Date
        let image: NSImage
    }

    @Published private(set) var state: State = .idle
    @Published private(set) var live: NSImage?
    /// The last few minutes, so the user can scrub back to the moment
    /// something went wrong instead of trying to catch it live.
    @Published private(set) var history: [Frame] = []
    /// nil while following the live feed; otherwise an index into `history`.
    @Published var replayIndex: Int?
    /// While true, clicks and keys in the window are sent to that desktop.
    @Published var controlling = false
    /// Set when the window is buried, minimised or the app is in the
    /// background. Capturing a second macOS session costs that session CPU
    /// and this one compositing, so it stops the moment nobody is looking.
    @Published var paused = false {
        didSet { if paused != oldValue { pausedChanged() } }
    }

    /// ~2 minutes at 6fps. Frames are small (about 70KB), so this is a few
    /// tens of megabytes at worst and is dropped when the window closes.
    private static let historyLimit = 720

    private(set) var port = 8766
    /// How wide a frame to ask for. Matching the window instead of always
    /// asking for 1100px means a small Desk window costs a small capture.
    var requestedWidth = 900
    private var task: Task<Void, Never>?
    private var seq = 0
    private var bounds = CGRect(x: 0, y: 0, width: 1512, height: 982)
    private var pointPerPixel = CGSize(width: 1, height: 1)
    private var frameSize = CGSize(width: 1, height: 1)

    var shown: NSImage? {
        if let replayIndex, history.indices.contains(replayIndex) { return history[replayIndex].image }
        return live
    }

    var isReplaying: Bool { replayIndex != nil }

    // MARK: Connecting

    func start(port: Int) {
        stop()
        self.port = port
        seq = 0
        state = .connecting
        task = Task { [weak self] in await self?.follow() }
    }

    func stop() {
        task?.cancel()
        task = nil
        controlling = false
        replayIndex = nil
        let port = self.port
        Task.detached { _ = try? await DeskFeed.post("/screen/stream/stop", [:], port: port, timeout: 5) }
        state = .idle
    }

    private func follow() async {
        var failures = 0
        while !Task.isCancelled {
            do {
                if paused {
                    try await Task.sleep(nanoseconds: 400_000_000)
                    continue
                }
                if !state.isLive {
                    let info = try await Self.post("/screen/stream/start",
                                                   ["maxWidth": requestedWidth, "fps": 5,
                                                    "quality": 0.5],
                                                   port: port, timeout: 20)
                    apply(info)
                    state = .live
                }
                let payload = try await Self.post("/screen/stream/frame", ["since": seq, "waitMs": 1500],
                                                  port: port, timeout: 30)
                failures = 0
                guard let base64 = payload["base64"] as? String,
                      let data = Data(base64Encoded: base64),
                      let image = NSImage(data: data) else { continue }   // unchanged frame
                apply(payload)
                live = image
                append(Frame(id: seq, at: Date(), image: image))
            } catch is CancellationError {
                return
            } catch {
                failures += 1
                if failures >= 4 {
                    state = .stopped(Self.explain(error, port: port))
                    return
                }
                state = .connecting
                try? await Task.sleep(nanoseconds: 800_000_000)
            }
        }
    }

    private func pausedChanged() {
        guard paused else { return }
        // Tell the other session to stop capturing rather than just
        // ignoring its frames: the cost is over there, not here.
        let port = self.port
        state = .idle
        Task.detached { _ = try? await DeskFeed.post("/screen/stream/stop", [:], port: port, timeout: 5) }
    }

    private func append(_ frame: Frame) {
        history.append(frame)
        if history.count > Self.historyLimit { history.removeFirst(history.count - Self.historyLimit) }
    }

    private func apply(_ payload: [String: Any]) {
        if let value = payload["seq"] as? Int { seq = value }
        if let rect = payload["bounds"] as? [String: Any] {
            bounds = CGRect(x: rect["x"] as? Double ?? 0, y: rect["y"] as? Double ?? 0,
                            width: rect["width"] as? Double ?? bounds.width,
                            height: rect["height"] as? Double ?? bounds.height)
        }
        if let width = payload["width"] as? Double, let height = payload["height"] as? Double, width > 0, height > 0 {
            frameSize = CGSize(width: width, height: height)
        } else if let width = payload["width"] as? Int, let height = payload["height"] as? Int, width > 0, height > 0 {
            frameSize = CGSize(width: Double(width), height: Double(height))
        }
        pointPerPixel = CGSize(width: payload["pointPerPixelX"] as? Double ?? pointPerPixel.width,
                               height: payload["pointPerPixelY"] as? Double ?? pointPerPixel.height)
    }

    private static func explain(_ error: Error, port: Int) -> String {
        let account = port == 8766 ? "The Jugnu account" : "Your account"
        if (error as NSError).domain == NSURLErrorDomain {
            return "\(account) isn't answering. It may be logged out, or MacBridge isn't running there."
        }
        return error.localizedDescription
    }

    // MARK: Taking over

    /// Where a point in the displayed image lands on that desktop.
    func desktopPoint(view point: CGPoint, in viewSize: CGSize) -> CGPoint? {
        guard let image = shown, image.size.width > 0, image.size.height > 0 else { return nil }
        let scale = min(viewSize.width / image.size.width, viewSize.height / image.size.height)
        guard scale > 0 else { return nil }
        let drawn = CGSize(width: image.size.width * scale, height: image.size.height * scale)
        let origin = CGPoint(x: (viewSize.width - drawn.width) / 2, y: (viewSize.height - drawn.height) / 2)
        let inImage = CGPoint(x: (point.x - origin.x) / scale, y: (point.y - origin.y) / scale)
        guard inImage.x >= 0, inImage.y >= 0, inImage.x <= image.size.width, inImage.y <= image.size.height else {
            return nil
        }
        // The image is a scaled copy of the display, so pixels map back to
        // global points through the ratio MacBridge reported.
        return CGPoint(x: bounds.minX + inImage.x * pointPerPixel.width,
                       y: bounds.minY + inImage.y * pointPerPixel.height)
    }

    func click(at point: CGPoint, right: Bool = false, clicks: Int = 1) {
        send("/mouse/click", ["x": point.x, "y": point.y, "button": right ? "right" : "left", "count": clicks])
    }

    func move(to point: CGPoint) {
        send("/mouse/move", ["x": point.x, "y": point.y, "duration": 0])
    }

    func drag(from: CGPoint, to: CGPoint) {
        send("/mouse/drag", ["fromX": from.x, "fromY": from.y, "toX": to.x, "toY": to.y, "duration": 0.15])
    }

    func scroll(at point: CGPoint, dx: Double, dy: Double) {
        send("/mouse/scroll", ["x": point.x, "y": point.y, "dx": dx, "dy": dy])
    }

    func type(_ text: String) {
        send("/keyboard/type", ["text": text])
    }

    func press(key: String, modifiers: [String] = []) {
        send("/keyboard/press", modifiers.isEmpty ? ["key": key] : ["key": key, "modifiers": modifiers])
    }

    private func send(_ path: String, _ args: [String: Any]) {
        let port = self.port
        Task.detached { _ = try? await DeskFeed.post(path, args, port: port, timeout: 10) }
    }

    // MARK: HTTP

    private static func post(_ path: String, _ args: [String: Any], port: Int,
                             timeout: TimeInterval) async throws -> [String: Any] {
        var request = URLRequest(url: URL(string: "http://127.0.0.1:\(port)\(path)")!)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = timeout
        request.httpBody = try JSONSerialization.data(withJSONObject: ["args": args])
        let (data, _) = try await URLSession.shared.data(for: request)
        let json = try JSONSerialization.jsonObject(with: data) as? [String: Any] ?? [:]
        if json["ok"] as? Bool == false, let message = json["error"] as? String {
            throw NSError(domain: "MacBridge", code: 1, userInfo: [NSLocalizedDescriptionKey: message])
        }
        return json
    }
}
