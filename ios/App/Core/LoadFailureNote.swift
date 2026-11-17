import SwiftUI

/// A load that failed, said in place: the hub's one-line reason and Try again. A screen shows it instead of an
/// empty list, or above what it had when a refresh failed, so nothing goes blank or stale without saying so.
struct LoadFailureNote: View {
    let message: String
    var retry: (() async -> Void)?
    @State private var busy = false

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(Theme.warn)
            VStack(alignment: .leading, spacing: 6) {
                Text(message).font(.subheadline).fixedSize(horizontal: false, vertical: true)
                if let retry {
                    Button(busy ? "Trying…" : "Try again") {
                        Task { busy = true; await retry(); busy = false }
                    }
                    .font(.subheadline.weight(.semibold))
                    .disabled(busy)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(12)
        .background(Theme.warn.opacity(0.12), in: RoundedRectangle(cornerRadius: 12))
        .accessibilityElement(children: .combine)
    }
}

extension View {
    /// The failure note pinned above this screen's content when `failure` is set. What was loaded before stays
    /// visible under it, so a failed refresh says so instead of showing stale data as current.
    func loadFailure(_ failure: String?, retry: (() async -> Void)? = nil) -> some View {
        safeAreaInset(edge: .top, spacing: 0) {
            if let failure {
                LoadFailureNote(message: failure, retry: retry)
                    .padding(.horizontal).padding(.vertical, 8)
                    .background(.bar)
            }
        }
    }
}

extension HomeStore {
    /// An action that didn't go through: a short banner with the hub's reason. Silent when it was cancelled.
    func failed(_ title: String, _ error: Error) {
        guard let line = HubFailure.line(error) else { return }
        show(title, line, isError: true)
    }
}

/// A tap that changes something on the hub: a banner with the hub's reason when it doesn't go through.
@MainActor @discardableResult
func hubAct<T>(_ title: String, _ work: () async throws -> T) async -> T? {
    do {
        return try await work()
    } catch {
        HomeStore.shared.failed(title, error)
        return nil
    }
}
