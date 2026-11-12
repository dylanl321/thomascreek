import Foundation

/// Server-sent events from GET /v1/events. Reconnects with a growing delay and reports whether it's connected,
/// so cards can say "Live" or "3 min ago" honestly.
@MainActor
final class EventStream {
    private var task: Task<Void, Never>?
    private let onEvent: (HubEvent) -> Void
    private let onState: (Bool) -> Void

    init(onEvent: @escaping (HubEvent) -> Void, onState: @escaping (Bool) -> Void) {
        self.onEvent = onEvent; self.onState = onState
    }

    func start() {
        guard task == nil else { return }
        task = Task { [weak self] in
            var delay: Double = 1
            while !Task.isCancelled {
                do {
                    let req = try await HubClient.shared.authorizedRequest("/v1/events")
                    let (bytes, resp) = try await HubClient.streamSession.bytes(for: req)
                    guard (resp as? HTTPURLResponse)?.statusCode == 200 else { throw URLError(.badServerResponse) }
                    self?.onState(true)
                    delay = 1
                    var dataLines: [String] = []
                    for try await line in bytes.lines {
                        if line.hasPrefix(":") { continue }          // keepalive comment
                        if line.hasPrefix("data:") {
                            dataLines.append(String(line.dropFirst(5)).trimmingCharacters(in: .whitespaces))
                        } else if line.isEmpty, !dataLines.isEmpty {
                            self?.dispatch(dataLines.joined(separator: "\n")); dataLines = []
                        }
                        // `bytes.lines` drops blank lines, so also dispatch each complete single-line event.
                        if dataLines.count == 1, let first = dataLines.first, first.hasSuffix("}") {
                            self?.dispatch(first); dataLines = []
                        }
                    }
                } catch {}
                self?.onState(false)
                try? await Task.sleep(for: .seconds(delay))
                delay = min(delay * 2, 30)
            }
        }
    }

    func stop() { task?.cancel(); task = nil }

    private func dispatch(_ json: String) {
        guard let e = try? HubClient.decoder.decode(HubEvent.self, from: Data(json.utf8)) else { return }
        onEvent(e)
    }
}
