import AppKit

/// Draws Jugnu's firefly mascot -- the same "glowing body with two
/// wings" silhouette as FireflyView -- as a small NSImage suitable for
/// an NSStatusItem. Status bar icons are template images (a shape drawn
/// in black, tinted by the system to match light/dark menu bars and
/// menu-item highlighting) rather than SwiftUI views, so this is a flat
/// silhouette rather than FireflyView's glow/gradient treatment.
///
/// `flutter` toggles between two wing angles -- MenuBarController
/// alternates between them on a timer while Jugnu is thinking, which
/// reads as a small wing-beat in the menu bar without needing an actual
/// animated image format.
enum FireflyStatusIcon {
    private static let canvasSize = NSSize(width: 18, height: 18)

    static func image(for state: FireflyState, flutter: Bool, badge: Bool = false) -> NSImage {
        let image = NSImage(size: canvasSize, flipped: false) { rect in
            draw(in: rect, state: state, flutter: flutter)
            if badge {
                // Something is waiting for the user's OK.
                ((state == .error) ? NSColor.systemRed : NSColor.black).setFill()
                NSBezierPath(ovalIn: NSRect(x: rect.maxX - 5.5, y: rect.maxY - 5.5, width: 5, height: 5)).fill()
            }
            return true
        }
        // Error gets its own warm color so it stands out even though the
        // rest of the icon is a monochrome template image; idle/thinking
        // stay template so AppKit tints them like every other menu bar icon.
        image.isTemplate = (state != .error)
        return image
    }

    private static func draw(in rect: NSRect, state: FireflyState, flutter: Bool) {
        let color: NSColor = (state == .error) ? .systemRed : .black

        let bodyRadius = rect.width * 0.11
        let bodyCenter = NSPoint(x: rect.midX, y: rect.midY)

        let restAngle: CGFloat = 16
        let flutterAngle: CGFloat = 32
        let wingAngle = (state == .thinking && flutter) ? flutterAngle : restAngle

        let wingWidth = rect.width * 0.42
        let wingHeight = rect.height * 0.17
        let wingOffset = bodyRadius + wingWidth * 0.4

        drawWing(
            center: NSPoint(x: bodyCenter.x - wingOffset, y: bodyCenter.y),
            width: wingWidth, height: wingHeight,
            degrees: wingAngle, color: color
        )
        drawWing(
            center: NSPoint(x: bodyCenter.x + wingOffset, y: bodyCenter.y),
            width: wingWidth, height: wingHeight,
            degrees: -wingAngle, color: color
        )

        color.setFill()
        let bodyRect = NSRect(
            x: bodyCenter.x - bodyRadius, y: bodyCenter.y - bodyRadius,
            width: bodyRadius * 2, height: bodyRadius * 2
        )
        NSBezierPath(ovalIn: bodyRect).fill()
    }

    private static func drawWing(center: NSPoint, width: CGFloat, height: CGFloat, degrees: CGFloat, color: NSColor) {
        var transform = AffineTransform.identity
        transform.translate(x: center.x, y: center.y)
        transform.rotate(byDegrees: degrees)

        let localRect = NSRect(x: -width / 2, y: -height / 2, width: width, height: height)
        let path = NSBezierPath(ovalIn: localRect)
        path.transform(using: transform)

        color.withAlphaComponent(0.8).setFill()
        path.fill()
    }
}
