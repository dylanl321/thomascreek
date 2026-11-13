import SwiftUI
import SafariServices

/// Signs a browser in to the hub's own status and keys page (/admin, home network only).
/// Either type the code the page shows, or open a one-time link right here on the phone. Admin only.
struct HubWebPageView: View {
    @Environment(HomeStore.self) private var store
    @State private var code = ""
    @State private var busy = false
    @State private var message: (String, Bool)?
    @State private var safari: IdentifiedURL?

    private struct Link: Decodable { var url: String; var expiresIn: Int? }
    private struct OK: Decodable { var ok: Bool? }

    private var cleaned: String { code.uppercased().filter { $0.isLetter || $0.isNumber } }

    var body: some View {
        List {
            Section {
                TextField("ABCD-EFGH", text: $code)
                    .font(.title3.monospaced())
                    .textInputAutocapitalization(.characters)
                    .autocorrectionDisabled()
                    .onChange(of: code) { _, v in if v.count > 9 { code = String(v.prefix(9)) } }
                Button { Task { await approve() } } label: {
                    Label("Sign that browser in", systemImage: "checkmark.shield")
                }
                .disabled(cleaned.count != 8 || busy)
            } header: {
                Text("On a computer")
            } footer: {
                Text("On a computer at home, open the hub's address followed by /admin, and type the code it shows. The page signs itself in within a couple of seconds.")
            }
            Section {
                Button { Task { await openHere() } } label: {
                    Label("Open on this phone", systemImage: "safari")
                }
                .disabled(busy)
            } footer: {
                Text("Uses a one-time link that lasts a minute. Safari warns about the hub's certificate the first time; that's expected for a home server.")
            }
            if let m = message {
                Section { Label(m.0, systemImage: m.1 ? "checkmark.circle.fill" : "exclamationmark.triangle.fill").foregroundStyle(m.1 ? Theme.accent : Theme.warn) }
            }
            if busy { Section { ProgressView().frame(maxWidth: .infinity) } }
        }
        .scrollContentBackground(.hidden).background(Backdrop())
        .navigationTitle("Hub web page")
        .sheet(item: $safari) { SafariView(url: $0.url).ignoresSafeArea() }
    }

    private func approve() async {
        busy = true; defer { busy = false }
        do {
            let _: OK = try await HubClient.shared.post("/v1/admin/web-approve", ["code": cleaned], confirm: "Sign in the hub web page")
            message = ("Approved. The browser signs itself in now.", true)
            code = ""
        } catch HubClient.HubError.cancelled {
        } catch HubClient.HubError.unreachable {
            message = ("Couldn't reach the hub at home. This only works on home Wi-Fi.", false)
        } catch HubClient.HubError.server(404, let text) {
            // The hub's own answer says the code is unknown; anything else came from somewhere other than the hub.
            message = (text.contains("not valid") ? "That code has expired. Reload the page for a new one." : "The hub didn't answer this (\(text)). Are you on home Wi-Fi?", false)
        } catch {
            message = (error.localizedDescription, false)
        }
    }

    private func openHere() async {
        busy = true; defer { busy = false }
        do {
            let l: Link = try await HubClient.shared.post("/v1/admin/web-link", [:], confirm: "Open the hub web page")
            guard let url = await HubClient.shared.resolve(l.url) else { message = ("Couldn't reach the hub.", false); return }
            message = nil
            safari = IdentifiedURL(url: url)
        } catch HubClient.HubError.cancelled {
        } catch {
            message = (error.localizedDescription, false)
        }
    }
}

struct SafariView: UIViewControllerRepresentable {
    let url: URL
    func makeUIViewController(context: Context) -> SFSafariViewController { SFSafariViewController(url: url) }
    func updateUIViewController(_ vc: SFSafariViewController, context: Context) {}
}
