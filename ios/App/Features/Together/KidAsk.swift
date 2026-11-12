import SwiftUI

/// "Ask a grown-up" in pictures, for children who can't type a message yet.
struct AskGrownUpSheet: View {
    @Environment(HomeStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    @State private var sentTo: [String]?
    @State private var sending: String?

    private var little: Bool { store.me?.style == .little }
    private let asks: [(String, String, String)] = [
        ("🤗", "Come here please", "Can you come here please?"),
        ("🍎", "I'm hungry", "I'm hungry."),
        ("🥤", "I'm thirsty", "I'm thirsty."),
        ("🚽", "Bathroom help", "I need help in the bathroom."),
        ("🤕", "I got hurt", "I got hurt."),
        ("😢", "I'm sad", "I'm feeling sad."),
        ("📺", "TV help", "Can you help me with the TV?"),
        ("🧸", "Play with me", "Will you play with me?"),
    ]

    var body: some View {
        NavigationStack {
            ScrollView {
                if let sentTo {
                    VStack(spacing: 14) {
                        Text("📨").font(.system(size: 80))
                        Text("Sent to \(sentTo.isEmpty ? "a grown-up" : sentTo.joined(separator: " and "))!").font(Theme.kidFont(28))
                            .multilineTextAlignment(.center)
                    }
                    .padding(.top, 60)
                    .task {
                        if little { Speaker.say("I told \(sentTo.isEmpty ? "a grown-up" : sentTo.joined(separator: " and ")).") }
                        try? await Task.sleep(for: .seconds(2)); dismiss()
                    }
                } else {
                    LazyVGrid(columns: [GridItem(.flexible(), spacing: 12), GridItem(.flexible(), spacing: 12)], spacing: 12) {
                        ForEach(asks.indices, id: \.self) { i in
                            let a = asks[i]
                            Button { Task { await send(a) } } label: {
                                VStack(spacing: 6) {
                                    Text(a.0).font(.system(size: little ? 56 : 44))
                                    Text(a.1).font(Theme.kidFont(little ? 20 : 17, .semibold)).foregroundStyle(Theme.fg)
                                        .multilineTextAlignment(.center)
                                }
                                .frame(maxWidth: .infinity, minHeight: little ? 150 : 120)
                                .background(Theme.card, in: RoundedRectangle(cornerRadius: 26, style: .continuous))
                                .overlay { if sending == a.1 { ProgressView() } }
                            }
                            .buttonStyle(.plain)
                            .disabled(sending != nil)
                        }
                    }
                    .padding(16)
                }
            }
            .background(Backdrop())
            .navigationTitle(little ? "Ask a grown-up" : "Ask a parent")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Close") { dismiss() } } }
        }
    }

    private func send(_ a: (String, String, String)) async {
        sending = a.1
        if little { Speaker.say(a.1) }
        struct R: Decodable { var sentTo: [String]? }
        do {
            let r: R = try await HubClient.shared.post("/v1/ask-parent", ["text": a.2, "emoji": a.0])
            sentTo = r.sentTo ?? []
        } catch { store.show(error.localizedDescription, isError: true) }
        sending = nil
    }
}
