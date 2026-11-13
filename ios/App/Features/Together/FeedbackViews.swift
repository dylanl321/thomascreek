import SwiftUI
import UIKit

// MARK: - Tell Dylan

extension Notification.Name { static let deviceShaken = Notification.Name("tc.deviceShaken") }

/// Shake anywhere to tell Dylan something (with a picture of the screen).
extension UIWindow {
    open override func motionEnded(_ motion: UIEvent.EventSubtype, with event: UIEvent?) {
        if motion == .motionShake { NotificationCenter.default.post(name: .deviceShaken, object: nil) }
        super.motionEnded(motion, with: event)
    }
}

@MainActor
enum Screenshot {
    /// The app's window as a JPEG, taken before the feedback sheet covers it.
    static func capture() -> Data? {
        guard let window = UIApplication.shared.connectedScenes.compactMap({ ($0 as? UIWindowScene)?.keyWindow }).first else { return nil }
        let format = UIGraphicsImageRendererFormat()
        format.scale = min(2, window.screen.scale)
        let img = UIGraphicsImageRenderer(bounds: window.bounds, format: format).image { _ in
            window.drawHierarchy(in: window.bounds, afterScreenUpdates: false)
        }
        return img.jpegData(compressionQuality: 0.6)
    }
}

@MainActor
@Observable
final class TellDylan {
    static let shared = TellDylan()
    var shot: Data?
    var showing = false

    func open() {
        guard !showing, HomeStore.shared.role != .pending else { return }
        shot = Screenshot.capture()
        showing = true
    }
}

struct TellDylanSheet: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    @Environment(\.dismiss) private var dismiss
    let shot: Data?
    @State private var text = ""
    @State private var withShot = true
    @State private var sending = false
    @State private var sent = false
    @FocusState private var focused: Bool

    private var kid: Bool { store.role == .child }

    var body: some View {
        NavigationStack {
            if sent {
                VStack(spacing: 14) {
                    Image(systemName: "paperplane.fill").font(.system(size: 48)).foregroundStyle(Theme.accent).symbolEffect(.bounce, value: sent)
                    Text("Sent to Dylan").font(.title2.weight(.bold))
                    Text("Thanks! It's in his inbox.").foregroundStyle(Theme.dim)
                }
                .task { try? await Task.sleep(for: .seconds(1.3)); dismiss() }
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 14) {
                        Text(kid ? "What should Dylan know? 🙂" : "Something not right, or an idea? Dylan gets it with a picture of the screen.")
                            .foregroundStyle(Theme.dim)
                        TextField(kid ? "Type here" : "What happened, or what would help?", text: $text, axis: .vertical)
                            .lineLimit(4...10).focused($focused)
                            .padding(12).background(Theme.card2, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                        if let shot, let img = UIImage(data: shot) {
                            Toggle(isOn: $withShot) { Label("Include the screen", systemImage: "photo") }
                            if withShot {
                                Image(uiImage: img).resizable().scaledToFit().frame(maxHeight: 260)
                                    .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
                                    .overlay(RoundedRectangle(cornerRadius: 14).stroke(Theme.line))
                                    .frame(maxWidth: .infinity)
                            }
                        }
                    }
                    .padding(20)
                }
                .navigationTitle("Tell Dylan")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                    ToolbarItem(placement: .confirmationAction) {
                        Button("Send") { Task { await send() } }
                            .disabled(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || sending)
                    }
                }
                .onAppear { focused = true }
            }
        }
        .presentationDetents([.large])
    }

    private func send() async {
        sending = true
        defer { sending = false }
        var body: [String: Any] = ["text": text, "screen": router.screenName,
                                   "app_build": Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? ""]
        if withShot, let shot { body["screenshot_jpeg_b64"] = shot.base64EncodedString() }
        do {
            try await HubClient.shared.send("/v1/feedback", body)
            sent = true
        } catch { store.show(error.localizedDescription, isError: true) }
    }
}

/// Adds the shake gesture and the sheet to the whole app.
struct TellDylanModifier: ViewModifier {
    @State private var tell = TellDylan.shared
    func body(content: Content) -> some View {
        content
            .onReceive(NotificationCenter.default.publisher(for: .deviceShaken)) { _ in tell.open() }
            .sheet(isPresented: $tell.showing) { TellDylanSheet(shot: tell.shot) }
    }
}

// MARK: - Dylan's list

struct FeedbackListView: View {
    @State private var items: [FeedbackItem]?
    @State private var full: FeedbackItem?

    var body: some View {
        List {
            if let items, items.isEmpty {
                Text("Nothing yet. Anyone can shake their phone, or use Profile → Tell Dylan.").foregroundStyle(Theme.dim)
            }
            ForEach(items ?? []) { f in
                Button { full = f } label: {
                    HStack(alignment: .top, spacing: 12) {
                        if let shot = f.shot {
                            HubImage(path: shot) { Theme.card2 }.frame(width: 54, height: 96)
                                .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                        }
                        VStack(alignment: .leading, spacing: 3) {
                            HStack {
                                Text(f.who).font(.subheadline.weight(.semibold))
                                Spacer()
                                if let d = f.date { Text(d, format: .relative(presentation: .named)).font(.caption).foregroundStyle(Theme.dim) }
                            }
                            Text(f.text).font(.subheadline).foregroundStyle(f.done == true ? Theme.dim : Theme.fg).lineLimit(4)
                            if let s = f.screen { Text("\(s)\(f.appBuild.map { " · build \($0)" } ?? "")").font(.caption2).foregroundStyle(Theme.dim) }
                        }
                    }
                }
                .buttonStyle(.plain)
                .swipeActions {
                    if f.done != true {
                        Button("Done") { Task { try? await HubClient.shared.send("/v1/feedback/\(f.id)/done"); await load() } }.tint(Theme.accent)
                    }
                }
            }
        }
        .navigationTitle("Family feedback")
        .task { await load() }
        .refreshable { await load() }
        .sheet(item: $full) { f in
            NavigationStack {
                ScrollView {
                    VStack(alignment: .leading, spacing: 12) {
                        Text(f.text).font(.body)
                        if let shot = f.shot { HubImage(path: shot, contentMode: .fit) { LoadingCard() } }
                    }
                    .padding()
                }
                .navigationTitle(f.who)
                .navigationBarTitleDisplayMode(.inline)
            }
        }
    }

    private func load() async {
        items = (try? await HubClient.shared.get("/v1/feedback", as: FeedbackList.self))?.items ?? items ?? []
    }
}
