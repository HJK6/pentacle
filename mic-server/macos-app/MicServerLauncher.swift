// MicServerLauncher.swift
//
// Native arm64 launcher for MicServer.app. Replaces the bash + helper combo
// with a single binary that:
//   1. Requests microphone permission via AVCaptureDevice (triggers macOS
//      prompt on first launch; cached afterwards in TCC.db).
//   2. Execs the Python mic_server.py via posix_spawn so the python child
//      inherits this binary's TCC responsibility (com.bartimaeus.mic-server).
//
// Build:
//   swiftc -O -target arm64-apple-macos11 -o mic-server-launcher MicServerLauncher.swift
//
// CFBundleExecutable in Info.plist must be "mic-server-launcher".

import AVFoundation
import Foundation
import Darwin

let HOME_DIR = FileManager.default.homeDirectoryForCurrentUser.path
let CWD_PATH = ProcessInfo.processInfo.environment["MIC_SERVER_ROOT"] ?? HOME_DIR + "/repos/mic-listener"
let LOG_PATH = CWD_PATH + "/mic_server.log"
let PYTHON_PATH = ProcessInfo.processInfo.environment["MIC_SERVER_PYTHON"] ?? CWD_PATH + "/.venv/bin/python"
let SCRIPT_PATH = CWD_PATH + "/mic_server.py"

func logLine(_ msg: String) {
    let ts = ISO8601DateFormatter().string(from: Date())
    let line = "[\(ts)] [launcher] \(msg)\n"
    if let data = line.data(using: .utf8) {
        if let fh = FileHandle(forWritingAtPath: LOG_PATH) {
            fh.seekToEndOfFile()
            fh.write(data)
            try? fh.close()
        } else {
            // create if missing
            FileManager.default.createFile(atPath: LOG_PATH, contents: data)
        }
    }
}

logLine("=== MicServer.app launching mic_server (Swift launcher) ===")

// Respect an explicit supported mode; preserve ON when no mode was configured.
let rawMode = (ProcessInfo.processInfo.environment["MIC_SERVER_START_MODE"] ?? "on")
    .trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
let startMode = rawMode.isEmpty ? "on" : rawMode
if !["off", "on", "clipboard", "meeting"].contains(startMode) {
    logLine("Unsupported MIC_SERVER_START_MODE; refusing startup")
    exit(78)
}
setenv("MIC_SERVER_START_MODE", startMode, 1)
logLine("startup mode = \(startMode)")

// OFF is a file-transcription startup. Do not query or request microphone permission.
if startMode != "off" {
    // Step 1: ensure mic permission
    let initial = AVCaptureDevice.authorizationStatus(for: .audio)
    logLine("mic auth initial = \(initial.rawValue)")

    if initial == .notDetermined {
        let sem = DispatchSemaphore(value: 0)
        AVCaptureDevice.requestAccess(for: .audio) { granted in
            logLine("mic auth granted = \(granted)")
            sem.signal()
        }
        // Allow up to 5 minutes for the user to respond. The .app stays alive while waiting.
        _ = sem.wait(timeout: .now() + 300)
    }

    let final = AVCaptureDevice.authorizationStatus(for: .audio)
    logLine("mic auth final = \(final.rawValue)")

    if final != .authorized {
        logLine("WARNING: mic permission not authorized — server will start but capture will be silent until granted via System Settings → Privacy & Security → Microphone")
    }

} else {
    logLine("OFF startup: microphone permission and capture are not requested")
}

// Step 2: exec python mic_server.py. We use execv so this Swift process is
// REPLACED by python, preserving the parent (.app bundle) and TCC chain.
// posix_spawn would create a new child; execv keeps PID=our PID and ensures
// python inherits MicServer.app's TCC responsibility identity.

logLine("execv → \(PYTHON_PATH) \(SCRIPT_PATH)")

// chdir so mic_server.py finds its sibling files
_ = CWD_PATH.withCString { chdir($0) }

// The selected mode is passed unchanged to the Python startup contract.

let argv: [UnsafeMutablePointer<CChar>?] = [
    strdup(PYTHON_PATH),
    strdup(SCRIPT_PATH),
    nil,
]

// Redirect stdout/stderr to the log file before execv so python's output is captured.
if let logFD = open(LOG_PATH, O_WRONLY | O_APPEND | O_CREAT, 0o644) as Int32?, logFD >= 0 {
    dup2(logFD, 1)
    dup2(logFD, 2)
    close(logFD)
}

let result = execv(PYTHON_PATH, argv)

// execv only returns on failure
logLine("execv FAILED: result=\(result) errno=\(errno) (\(String(cString: strerror(errno))))")
exit(1)
