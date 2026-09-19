//
//  MacBridgeLauncher.swift
//  Jugnu
//

import AppKit

/// Auto-launches the MacBridge helper app -- the separate swiftc-built bundle
/// that exposes calendar, contacts, files, shell, mail, Safari, keyboard,
/// mouse, accessibility and WhatsApp over localhost:8765 -- so it doesn't have
/// to be started by hand before using Jugnu.
///
/// Roughly 130 of Jugnu's 141 tools are thin HTTP wrappers over that server
/// (see PythonBackend/tools/macbridge.py), so if it isn't running, most of
/// what Jugnu can do simply fails at the first tool call.
enum MacBridgeLauncher {

    /// Where MacBridge.app is expected to live, most specific source first:
    ///
    ///  1. `JUGNU_MACBRIDGE_PATH` -- an explicit override, for a checkout in
    ///     a different location or a second build being tested.
    ///  2. `/Applications/MacBridge.app` -- the normal install location.
    ///  3. `~/Applications/MacBridge.app` -- per-user install.
    ///  4. `~/Desktop/Jarvis/MacBridge/MacBridge.app` -- where it currently
    ///     lives on the original development machine. Kept last so existing
    ///     setups keep working untouched, but it is a fallback now rather
    ///     than the only answer.
    ///
    /// This used to be a single hardcoded `static let appPath` pointing at
    /// #4, which meant the project could not run on any machine but one --
    /// and the comment above it just said "UPDATE THIS", i.e. edit the source
    /// to build elsewhere.
    static var searchPaths: [String] {
        var paths: [String] = []
        if let override = ProcessInfo.processInfo.environment["JUGNU_MACBRIDGE_PATH"],
           !override.isEmpty {
            paths.append((override as NSString).expandingTildeInPath)
        }
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        paths.append("/Applications/MacBridge.app")
        paths.append("\(home)/Applications/MacBridge.app")
        paths.append("\(home)/Desktop/Jarvis/MacBridge/MacBridge.app")
        return paths
    }

    /// The first search path that actually exists, or nil.
    static func resolvedPath() -> String? {
        searchPaths.first { FileManager.default.fileExists(atPath: $0) }
    }

    static func launchIfNeeded() {
        // Match on bundle identifier, not display name. The old check also
        // accepted any process whose localizedName happened to be
        // "MacBridge", and compared bundleURL.path against one hardcoded
        // string -- so a copy running from anywhere else counted as "already
        // running" for the name check while failing the path check.
        let alreadyRunning = NSWorkspace.shared.runningApplications.contains {
            $0.bundleIdentifier == "com.jarvis.macbridge"
        }
        guard !alreadyRunning else {
            print("[jugnu] MacBridge already running, skipping launch")
            return
        }

        guard let appPath = resolvedPath() else {
            print("""
            [jugnu] MacBridge.app not found. Looked in:
            \(searchPaths.map { "  - \($0)" }.joined(separator: "\n"))
            Build it with MacBridge/build.sh, or set JUGNU_MACBRIDGE_PATH to \
            wherever it lives. Most of Jugnu's tools route through it and will \
            fail until it's running.
            """)
            return
        }

        let config = NSWorkspace.OpenConfiguration()
        config.activates = false  // don't steal focus from Jugnu's window
        config.hides = true       // launch in the background

        NSWorkspace.shared.openApplication(
            at: URL(fileURLWithPath: appPath),
            configuration: config
        ) { _, error in
            if let error {
                print("[jugnu] failed to launch MacBridge at \(appPath): \(error.localizedDescription)")
            } else {
                print("[jugnu] MacBridge launched from \(appPath)")
            }
        }
    }
}
