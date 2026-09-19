import Foundation

/// Launches bridge.py as a long-lived subprocess and communicates with it
/// using one JSON object per line over stdin/stdout.
final class BackendBridge {

    var onEvent: ((IncomingEvent) -> Void)?
    var onLaunchFailure: ((String) -> Void)?

    private var process: Process?
    private var stdinHandle: FileHandle?
    private var stdoutHandle: FileHandle?
    private var stderrHandle: FileHandle?

    private var stdoutBuffer = Data()
    private let newline = Data([0x0A])
    private let bridgeScriptPath: String

    init(bridgeScriptPath: String) {
        self.bridgeScriptPath = bridgeScriptPath
    }

    /// - Parameter settingsEnvironment: `SettingsStore.backendEnvironment`.
    ///   Passed in and handed straight to the child rather than being
    ///   written into this process first -- see `backendEnvironment(_:_:)`.
    func start(workingDirectory: String, settingsEnvironment: [String: String] = [:]) {
        guard process?.isRunning != true else { return }

        guard FileManager.default.fileExists(atPath: bridgeScriptPath) else {
            onLaunchFailure?(
                "Couldn't find bridge.py at \(bridgeScriptPath)."
            )
            return
        }

        guard FileManager.default.fileExists(atPath: workingDirectory) else {
            onLaunchFailure?(
                "Couldn't find the backend directory at \(workingDirectory)."
            )
            return
        }

        guard let pythonPath = Self.resolvedPythonPath() else {
            onLaunchFailure?(
                "Couldn't find a Python runtime for the Jugnu backend."
            )
            return
        }

        let childProcess = Process()
        childProcess.executableURL = URL(fileURLWithPath: pythonPath)

        childProcess.arguments = [
            bridgeScriptPath
        ]

        childProcess.currentDirectoryURL =
            URL(fileURLWithPath: workingDirectory)

        childProcess.environment =
            Self.backendEnvironment(
                workingDirectory: workingDirectory,
                settings: settingsEnvironment
            )

        let stdinPipe = Pipe()
        let stdoutPipe = Pipe()
        let stderrPipe = Pipe()

        childProcess.standardInput = stdinPipe
        childProcess.standardOutput = stdoutPipe
        childProcess.standardError = stderrPipe

        stdinHandle = stdinPipe.fileHandleForWriting
        stdoutHandle = stdoutPipe.fileHandleForReading
        stderrHandle = stderrPipe.fileHandleForReading

        stdoutHandle?.readabilityHandler = { [weak self] handle in
            let data = handle.availableData

            guard !data.isEmpty else {
                return
            }

            self?.consume(data)
        }

        stderrHandle?.readabilityHandler = { handle in
            let data = handle.availableData

            guard !data.isEmpty else {
                return
            }

            FileHandle.standardError.write(data)
        }

        childProcess.terminationHandler = { [weak self] terminatedProcess in
            DispatchQueue.main.async {
                self?.handleTermination(of: terminatedProcess)
            }
        }

        do {
            try childProcess.run()
            process = childProcess
        } catch {
            cleanUpHandles()

            onLaunchFailure?(
                "Couldn't launch the Python backend: \(error.localizedDescription)"
            )
        }
    }

    /// Sends send, clear, ping, daily_review, or quit requests.
    func send(_ request: OutgoingRequest) {
        guard
            let stdinHandle,
            let data = try? JSONEncoder().encode(request)
        else {
            return
        }

        var line = data
        line.append(newline)

        do {
            try stdinHandle.write(contentsOf: line)
        } catch {
            DispatchQueue.main.async { [weak self] in
                self?.onLaunchFailure?(
                    "Couldn't send a request to the backend: \(error.localizedDescription)"
                )
            }
        }
    }

    /// Stops the backend, sending a graceful "quit" first and waiting
    /// briefly for it to land before force-terminating. The actual wait
    /// -- and the force-terminate fallback -- now happens off the
    /// calling thread. This used to be a direct `exited.wait(timeout:)`
    /// call right here, which blocks synchronously: harmless the one
    /// time this ran before (right as the whole app was already
    /// quitting), but a real problem now that anything calls stop()
    /// interactively (see AppState.restartBackend()) -- a synchronous
    /// 1-second block here would freeze Jugnu's entire UI every time.
    /// `completion` fires after the process has actually exited and
    /// handles are cleaned up; wait for it before starting a new process
    /// on the same stdin/stdout rather than racing the two.
    func stop(completion: (() -> Void)? = nil) {
        guard
            let runningProcess = process,
            runningProcess.isRunning
        else {
            cleanUpHandles()
            process = nil
            completion?()
            return
        }

        let exited = DispatchSemaphore(value: 0)

        let existingTerminationHandler =
            runningProcess.terminationHandler

        runningProcess.terminationHandler = { process in
            existingTerminationHandler?(process)
            exited.signal()
        }

        send(
            OutgoingRequest(
                type: "quit",
                id: nil,
                text: nil
            )
        )

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            if exited.wait(timeout: .now() + 1.0) == .timedOut,
               runningProcess.isRunning {
                runningProcess.terminate()
                _ = exited.wait(timeout: .now() + 0.5)
            }

            DispatchQueue.main.async {
                self?.process = nil
                self?.cleanUpHandles()
                completion?()
            }
        }
    }

    private func consume(_ data: Data) {
        stdoutBuffer.append(data)

        while let range = stdoutBuffer.range(of: newline) {
            let line = stdoutBuffer.subdata(
                in: stdoutBuffer.startIndex..<range.lowerBound
            )

            stdoutBuffer.removeSubrange(
                stdoutBuffer.startIndex..<range.upperBound
            )

            guard !line.isEmpty else {
                continue
            }

            do {
                let event = try JSONDecoder().decode(
                    IncomingEvent.self,
                    from: line
                )

                DispatchQueue.main.async { [weak self] in
                    self?.onEvent?(event)
                }
            } catch {
                let rawLine =
                    String(
                        data: line,
                        encoding: .utf8
                    ) ?? "<non-UTF-8 data>"

                let message =
                    "[BackendBridge] Ignored invalid event: \(rawLine)\n"

                FileHandle.standardError.write(
                    message.data(using: .utf8) ?? Data()
                )
            }
        }
    }

    private func handleTermination(
        of terminatedProcess: Process
    ) {
        guard process === terminatedProcess else {
            return
        }

        let exitCode =
            terminatedProcess.terminationStatus

        process = nil
        cleanUpHandles()

        guard exitCode != 0 else {
            return
        }

        onLaunchFailure?(
            "Backend process exited unexpectedly (code \(exitCode))."
        )
    }

    private func cleanUpHandles() {
        stdoutHandle?.readabilityHandler = nil
        stderrHandle?.readabilityHandler = nil

        try? stdinHandle?.close()
        try? stdoutHandle?.close()
        try? stderrHandle?.close()

        stdinHandle = nil
        stdoutHandle = nil
        stderrHandle = nil

        stdoutBuffer.removeAll(
            keepingCapacity: false
        )
    }

    /// Builds the child process's environment.
    ///
    /// `settings` is layered on top of what Jugnu itself inherited, and the
    /// hardcoded fallbacks below only fill gaps neither supplied — which is
    /// exactly the precedence the old code produced, just without the side
    /// effect that made it work.
    ///
    /// That side effect: SettingsStore used to `setenv()` every value —
    /// including the API key it had just read out of the Keychain — into
    /// *this* process, and this function then picked them up incidentally
    /// via `ProcessInfo.processInfo.environment`. It worked, but it meant
    /// Jugnu held the user's API key in its own environment block for the
    /// entire lifetime of the app, where a crash report, a diagnostic dump,
    /// `ps -E`, or any other child process would pick it up. Storing the key
    /// in the Keychain instead of UserDefaults (see KeychainStore) was the
    /// whole point, and this quietly undid a good part of it.
    ///
    /// Now the values travel directly from SettingsStore to the one process
    /// that actually needs them, and never touch this one.
    private static func backendEnvironment(
        workingDirectory: String,
        settings: [String: String]
    ) -> [String: String] {

        var environment =
            ProcessInfo.processInfo.environment

        for (key, value) in settings {
            environment[key] = value
        }

        // Python emits protocol messages immediately.
        environment["PYTHONUNBUFFERED"] = "1"

        environment["JUGNU_ROOT"] =
            environment["JUGNU_ROOT"] ?? workingDirectory

        environment["JUGNU_PROVIDER"] =
            environment["JUGNU_PROVIDER"] ?? "api"

        environment["JUGNU_API_BASE_URL"] =
            environment["JUGNU_API_BASE_URL"]
            ?? "https://generativelanguage.googleapis.com/v1beta/openai"

        environment["JUGNU_API_CHAT_MODEL"] =
            environment["JUGNU_API_CHAT_MODEL"]
            ?? "gemini-3.1-flash-lite"

        environment["JUGNU_EMBED_PROVIDER"] =
            environment["JUGNU_EMBED_PROVIDER"]
            ?? "local"

        if environment["JUGNU_API_KEY"] == nil,
           let legacyKey =
                environment["Jugnu_API_KEY"],
           !legacyKey.isEmpty {

            environment["JUGNU_API_KEY"] =
                legacyKey
        }

        return environment
    }

    // MARK: - Python Runtime

    private static func resolvedPythonPath() -> String? {

        // 1. Release app:
        //    Jugnu.app/Contents/Resources/PythonBackend/PythonEnv/bin/python
        if let resourceURL = Bundle.main.resourceURL {

            let bundledPython =
                resourceURL
                    .appendingPathComponent(
                        "PythonBackend",
                        isDirectory: true
                    )
                    .appendingPathComponent(
                        "PythonEnv",
                        isDirectory: true
                    )
                    .appendingPathComponent(
                        "bin",
                        isDirectory: true
                    )
                    .appendingPathComponent(
                        "python",
                        isDirectory: false
                    )
                    .path

            if FileManager.default.isExecutableFile(
                atPath: bundledPython
            ) {
                return bundledPython
            }
        }

        // 2. Development override.
        if let configuredPython =
            ProcessInfo.processInfo.environment[
                "JUGNU_PYTHON_PATH"
            ],
            FileManager.default.isExecutableFile(
                atPath: configuredPython
            ) {
            return configuredPython
        }

        // 3. Development fallbacks.
        let candidates = [
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
            "/usr/bin/python3"
        ]

        return candidates.first {
            FileManager.default.isExecutableFile(
                atPath: $0
            )
        }
    }
}
