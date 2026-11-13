import SwiftUI

/// Dylan only: see the app exactly as someone else does. Nothing in the house can be changed while
/// previewing (the hub refuses it); their favorites and Home layout can be set up. The preview ends when
/// the app goes to the background.
struct ViewAsSheet: View {
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        NavigationStack {
            ViewAsList()
                .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } } }
        }
    }
}

/// The people to preview as; pushed from Profile, or in its own sheet from a link.
struct ViewAsList: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    @State private var people: [Candidate]?
    @State private var failed = false

    struct Candidate: Decodable, Identifiable {
        var id: String
        var name: String
        var role: Role?
        var style: UIStyle?
        var color: String?
    }
    private struct Reply: Decodable { var people: [Candidate] }

    var body: some View {
            List {
                Section {
                    if let people {
                        ForEach(people) { p in
                            Button { start(p) } label: {
                                HStack(spacing: 12) {
                                    Avatar(name: p.name, color: Color(hexString: p.color), size: 34)
                                    VStack(alignment: .leading, spacing: 1) {
                                        Text(p.name).font(.body.weight(.semibold)).foregroundStyle(Theme.fg)
                                        Text(describe(p)).font(.caption).foregroundStyle(Theme.dim)
                                    }
                                    Spacer()
                                    Image(systemName: "eye").foregroundStyle(Theme.accent)
                                }
                            }
                        }
                    } else if failed {
                        Text("Can't reach home right now.").foregroundStyle(Theme.dim)
                    } else {
                        ProgressView()
                    }
                } footer: {
                    Text("You'll see their tabs, rooms and shows. Lights, locks and TVs can't be changed while you're looking, but you can set up their favorites and Home. Leaving the app ends it.")
                }
            }
            .navigationTitle("View as")
            .navigationBarTitleDisplayMode(.inline)
            .task { await load() }
    }

    private func describe(_ p: Candidate) -> String {
        switch p.role {
        case .child: p.style == .little ? "Kid, little style" : "Kid"
        case .adult: "Grown-up"
        case .guest: "Guest"
        case .tablet: "Shared tablet"
        case .admin: "Admin"
        default: ""
        }
    }

    private func load() async {
        do { people = try await HubClient.shared.get("/v1/me/view-as", as: Reply.self).people }
        catch { failed = true }
    }

    private func start(_ c: Candidate) {
        let person = Person(id: c.id, name: c.name, role: c.role, color: c.color, uiStyle: c.style)
        router.showViewAs = false
        router.showProfile = false
        Task { await store.startPreview(person) }
    }
}

/// The bar across the top while previewing someone else.
struct PreviewBanner: View {
    @Environment(HomeStore.self) private var store
    var body: some View {
        if let p = store.previewing {
            HStack(spacing: 10) {
                Image(systemName: "eye.fill")
                Text("Viewing as \(p.name) · nothing in the house changes").font(.footnote.weight(.semibold))
                    .lineLimit(1).minimumScaleFactor(0.8)
                Spacer(minLength: 4)
                Button("Exit") { Task { await store.stopPreview() } }
                    .font(.footnote.weight(.bold))
                    .padding(.horizontal, 12).padding(.vertical, 5)
                    .background(.white, in: Capsule())
                    .foregroundStyle(Theme.accent)
            }
            .foregroundStyle(.white)
            .padding(.horizontal, 16).padding(.vertical, 8)
            .frame(maxWidth: .infinity)
            .background(Theme.accent.ignoresSafeArea(edges: .top))
        }
    }
}
