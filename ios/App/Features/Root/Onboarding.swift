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
    @State private var tries = 0
    @State private var typing = false
    @State private var address = ""

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
            if typing {
                VStack(spacing: 10) {
                    TextField("Hub address, like 10.0.0.5:8443", text: $address)
                        .keyboardType(.numbersAndPunctuation).textInputAutocapitalization(.never).autocorrectionDisabled()
                        .padding(12).background(Theme.card, in: RoundedRectangle(cornerRadius: 12))
                    PrimaryButton(title: "Connect", symbol: "link") { connect() }
                        .disabled(Self.lanURL(address) == nil)
                }
            } else {
                PrimaryButton(title: "Try again", symbol: "arrow.clockwise") {
                    tries += 1
                    Task { await store.start() }
                }
                .opacity(store.phase == .enrolling ? 0 : 1)
                if store.phase == .findingHome {
                    Button("Enter the hub address instead") { typing = true }.font(.subheadline)
                }
            }
        }
        .padding(24)
        .frame(maxWidth: 520)
        .frame(maxWidth: .infinity)
        .background(Backdrop())
        .task { await store.start() }
    }

    /// Only private LAN addresses (10.x, 172.16-31.x, 192.168.x) or .local names; https on 8443 unless given.
    static func lanURL(_ text: String) -> URL? {
        var t = text.trimmingCharacters(in: .whitespaces)
        if t.isEmpty { return nil }
        if !t.contains("://") { t = "https://" + t }
        guard var c = URLComponents(string: t), let host = c.host, c.scheme == "https" || c.scheme == "http" else { return nil }
        let parts = host.split(separator: ".").compactMap { Int($0) }
        let isPrivate = parts.count == 4 && (parts[0] == 10 || (parts[0] == 192 && parts[1] == 168) || (parts[0] == 172 && (16...31).contains(parts[1])))
        guard isPrivate || host.hasSuffix(".local") else { return nil }
        if c.port == nil { c.port = 8443 }
        c.path = ""
        return c.url
    }

    private func connect() {
        guard let url = Self.lanURL(address), let host = url.host else { return }
        AppSettings.hubPin = ""
        AppSettings.trustFirstCertFrom = url.scheme == "https" ? host : ""
        AppSettings.hubLAN = url.absoluteString
        typing = false
        Task { await store.start() }
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
