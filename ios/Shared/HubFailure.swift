import Foundation
import os

/// Rule 8 in AGENTS.md: a failed hub call says what happened. The hub sends a short reason with every refusal;
/// this turns any failure into the one line a screen shows, and keeps quiet background calls in the log.
enum HubFailure {
    static let log = Logger(subsystem: "me.thomascreek.app", category: "hub")

    /// The line to show for a failed call: the hub's own reason when it sent one. Nil for a cancelled call,
    /// which is not a failure (the screen went away or a newer load replaced it).
    static func line(_ error: Error) -> String? {
        if error is CancellationError { return nil }
        if (error as? URLError)?.code == .cancelled { return nil }
        if let hub = error as? HubClient.HubError {
            if hub == .cancelled { return nil }
            if let text = hub.errorDescription, !text.isEmpty, !text.contains("NSURL"), !text.contains("Error Domain") {
                return text
            }
        }
        if error is DecodingError { return "Home sent something this app version can't read. Update the app." }
        return "That didn't load. Try again in a moment."
    }
}

/// A load someone is waiting on: the value, or nil and the line to show (nil too when it was only cancelled).
func hubLoad<T>(_ work: () async throws -> T) async -> (value: T?, failure: String?) {
    do {
        return (try await work(), nil)
    } catch {
        return (nil, HubFailure.line(error))
    }
}

/// A background refresh or a report nobody is waiting on: quiet on screen, written to the log.
@discardableResult
func hubQuiet<T>(_ what: String, _ work: () async throws -> T) async -> T? {
    do {
        return try await work()
    } catch {
        if let line = HubFailure.line(error) {
            HubFailure.log.info("\(what, privacy: .public) failed: \(line, privacy: .public)")
        }
        return nil
    }
}
