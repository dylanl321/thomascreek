import SwiftUI
import UniformTypeIdentifiers

struct IdentifiedURL: Identifiable { let url: URL; var id: String { url.absoluteString } }

/// The WireGuard clients Dylan made on the Firewalla. The hub hands one to each approved phone.
struct VPNPoolView: View {
    @Environment(HomeStore.self) private var store
    @State private var pool: Pool?
    @State private var picking = false
    @State private var picked: URL?
    struct Pool: Decodable {
        var clients: [Client]
        struct Client: Decodable, Identifiable, Hashable { var name: String; var address: String?; var deviceName: String?; var id: String { name } }
    }

    var body: some View {
        List {
            if let pool {
                Section {
                    ForEach(pool.clients) { c in
                        HStack {
                            Image(systemName: c.deviceName == nil ? "circle.dashed" : "iphone").foregroundStyle(c.deviceName == nil ? Theme.dim : Theme.accent)
                            VStack(alignment: .leading) {
                                Text(c.name)
                                Text(c.deviceName ?? "Free").font(.caption).foregroundStyle(Theme.dim)
                            }
                            Spacer()
                            if let a = c.address { Text(a).font(.caption.monospaced()).foregroundStyle(Theme.dim) }
                        }
                    }
                    if pool.clients.isEmpty { Text("No clients yet").foregroundStyle(Theme.dim) }
                } footer: {
                    Text("Make a WireGuard client on the Firewalla, share its file to Thomas Creek (or add it here), and the hub gives it to the next phone that needs one.")
                }
                Section { Button { picking = true } label: { Label("Add a client file", systemImage: "plus.circle") } }
            } else { ProgressView() }
        }
        .scrollContentBackground(.hidden).background(Backdrop())
        .navigationTitle("Away-from-home VPN")
        .task { await load() }
        .refreshable { await load() }
        .fileImporter(isPresented: $picking, allowedContentTypes: [UTType(filenameExtension: "conf") ?? .plainText, .plainText]) { r in
            if case .success(let u) = r { picked = u }
        }
        .sheet(item: Binding(get: { picked.map(IdentifiedURL.init) }, set: { picked = $0?.url })) { f in
            VPNImportSheet(file: f.url) { Task { await load() } }
        }
    }

    private func load() async { pool = try? await HubClient.shared.get("/v1/vpn/pool") }
}

/// Reads a .conf and adds it to the hub's pool (admin, Face ID). The file's text never leaves this phone except to the hub.
struct VPNImportSheet: View {
    @Environment(HomeStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let file: URL
    var done: (() -> Void)? = nil
    @State private var text: String?
    @State private var result: String?
    @State private var error: String?
    @State private var busy = false
    private struct Reply: Decodable { var name: String?; var clients: [VPNPoolView.Pool.Client]? }

    var body: some View {
        NavigationStack {
            VStack(spacing: 16) {
                Image(systemName: "lock.shield").font(.system(size: 44)).foregroundStyle(Theme.accent).padding(.top, 24)
                Text(file.deletingPathExtension().lastPathComponent).font(.title3.weight(.semibold))
                if store.role != .admin {
                    Text("Only Dylan can add VPN clients.").foregroundStyle(Theme.dim)
                } else if let result {
                    Label(result, systemImage: "checkmark.circle.fill").foregroundStyle(Theme.accent)
                } else {
                    Text("Add this WireGuard client to the house so a phone can use it away from home.")
                        .multilineTextAlignment(.center).foregroundStyle(Theme.dim)
                    if let error { Text(error).font(.footnote).foregroundStyle(Theme.warn) }
                    PrimaryButton(title: busy ? "Adding…" : "Add to the house", symbol: "plus") { add() }
                        .disabled(text == nil || busy)
                }
                Spacer()
            }
            .padding(24)
            .toolbar { Button(result == nil ? "Cancel" : "Done") { done?(); dismiss() } }
            .task { text = Self.read(file) ; if text == nil { error = "Couldn't read that file." } }
        }
    }

    static func read(_ url: URL) -> String? {
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        guard let s = try? String(contentsOf: url, encoding: .utf8), s.contains("[Interface]") else { return nil }
        return s
    }

    private func add() {
        guard let text else { return }
        busy = true
        Task {
            defer { busy = false }
            do {
                let r: Reply = try await HubClient.shared.post("/v1/vpn/pool", ["config": text], confirm: "Add a VPN client")
                result = "Added \(r.name ?? "client") · \(r.clients?.count ?? 0) in the pool"
            } catch { self.error = error.localizedDescription }
        }
    }
}
