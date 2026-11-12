import SwiftUI

struct LaunchView: View {
    var body: some View {
        ZStack {
            Color("LaunchBackground").ignoresSafeArea()
            ProgressView().tint(.white)
        }
    }
}

/// First open: the app looks for the hub on home Wi-Fi and registers itself. Nothing to scan or type.
struct JoinHomeView: View {
    @Environment(HomeStore.self) private var store
    var error: String? = nil

    var body: some View {
        VStack(spacing: 20) {
            Spacer()
            Image("HomeHeader").resizable().scaledToFill().frame(height: 220).clipped()
                .clipShape(RoundedRectangle(cornerRadius: 28, style: .continuous))
            VStack(spacing: 8) {
                Text("Thomas Creek").font(.system(.largeTitle, design: .rounded, weight: .bold))
                Text(message).font(.body).foregroundStyle(Theme.dim).multilineTextAlignment(.center)
            }
            if store.phase == .enrolling || store.phase == .findingHome {
                ProgressView().padding(.top, 4)
            }
            Spacer()
            PrimaryButton(title: "Try again", symbol: "arrow.clockwise") {
                Task { await store.start() }
            }
            .opacity(store.phase == .enrolling ? 0 : 1)
        }
        .padding(24)
        .frame(maxWidth: 520)
        .frame(maxWidth: .infinity)
        .background(Backdrop())
        .task { await store.start() }
    }

    private var message: String {
        if let error { return "Something went wrong joining the house: \(error)" }
        switch store.phase {
        case .enrolling: return "Found the house. Setting up this \(UIDevice.current.model)…"
        default: return "Looking for the house. Make sure this \(UIDevice.current.model) is on home Wi-Fi, and allow Local Network if iOS asks."
        }
    }
}

/// Enrolled, waiting for Dylan to say whose phone this is.
struct PendingView: View {
    @Environment(HomeStore.self) private var store

    var body: some View {
        VStack(spacing: 18) {
            Spacer()
            Image(systemName: "hourglass").font(.system(size: 54, weight: .semibold)).foregroundStyle(Theme.accent)
                .symbolEffect(.pulse)
            Text("Almost there").font(.system(.largeTitle, design: .rounded, weight: .bold))
            Text("Dylan just got a notification to say whose \(UIDevice.current.model) this is. It opens up as soon as he does.")
                .multilineTextAlignment(.center).foregroundStyle(Theme.dim)
            Spacer()
        }
        .padding(28)
        .background(Backdrop())
        .task {
            while !Task.isCancelled && store.phase == .pending {
                await store.loadMe()
                try? await Task.sleep(for: .seconds(5))
            }
            if store.phase == .ready { await store.refreshAll() }
        }
    }
}

/// A child tried something outside their limits: turn it into a request to a parent.
struct AskParentSheet: View {
    @Environment(HomeStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let ask: HomeStore.AskParent
    @State private var sent = false

    var body: some View {
        VStack(spacing: 18) {
            Image(systemName: sent ? "paperplane.fill" : "hand.raised.fill").font(.system(size: 44))
                .foregroundStyle(Theme.kid).symbolEffect(.bounce, value: sent)
            Text(sent ? "Asked!" : "Ask a parent?").font(Theme.kidFont(28))
            Text(sent ? "You'll see the answer here." : ask.what).multilineTextAlignment(.center).foregroundStyle(Theme.dim)
            if !sent {
                PrimaryButton(title: "Ask", symbol: "paperplane.fill", color: Theme.kid) {
                    Task {
                        try? await HubClient.shared.send("/v1/requests", ["kind": ask.kind, "text": ask.what])
                        sent = true
                        try? await Task.sleep(for: .seconds(1.4))
                        dismiss()
                    }
                }
                Button("Never mind") { dismiss() }.foregroundStyle(Theme.dim)
            }
        }
        .padding(28)
        .presentationDetents([.medium])
    }
}
