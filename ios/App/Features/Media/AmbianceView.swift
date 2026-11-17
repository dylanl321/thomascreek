import SwiftUI

// Ambiance: YouTube scenes (a fireplace, a holiday cabin with jazz, snow) for the TVs. The hub keeps the list
// (GET /v1/ambient/scenes) and decides what is saved; this screen browses it, sends a scene to a TV
// (POST /v1/tvs/{id}/actions with ambience_id), and lets a parent add or remove scenes and set the idle screen.

struct AmbiancePage: Decodable {
    var featured: AmbianceScene?
    var reason: String?
    var categories: [AmbianceCategory]
    var idle: AmbianceIdle
    var canEdit: Bool?
    var savingOn: Bool?
    var saving: Saving?
    struct Saving: Decodable { var id: String; var title: String?; var progress: Double? }
}
struct AmbianceCategory: Decodable, Identifiable { var id: String; var title: String; var items: [AmbianceScene] }
struct AmbianceScene: Decodable, Identifiable, Hashable {
    var id: String
    var title: String
    var category: String?
    var channel: String?
    var durationS: Int?
    var pictureUrl: String?
    var state: String
    var progress: Double?
    var added: Bool?
    var youtubeUrl: String?

    var status: String {
        switch state {
        case "ready": return "Plays on the TV"
        case "saving": return "Saving" + (progress.map { " \(Int($0 * 100))%" } ?? "")
        case "queued": return "Saving soon"
        case "failed": return "Opens in YouTube"
        default: return "Opens in YouTube"
        }
    }
    var length: String? {
        guard let s = durationS, s > 0 else { return nil }
        let m = max(1, s / 60)
        return m >= 90 ? "\((m + 30) / 60) hr" : "\(m) min"
    }
}
struct AmbianceIdle: Decodable, Equatable { var enabled: Bool; var afterMin: Int; var sound: Bool; var choice: String }
struct AmbianceSearch: Decodable { var items: [AmbianceScene] }
struct AmbianceCategories: Decodable { var categories: [Item]; struct Item: Decodable, Identifiable, Hashable { var id: String; var title: String } }

struct AmbianceView: View {
    @State private var page: AmbiancePage?
    @State private var tvs: [HubTvRow] = []
    @State private var tvID: String?
    @State private var note: String?
    @State private var adding = false
    @State private var failed = false

    private var canEdit: Bool { page?.canEdit == true }
    private var tv: HubTvRow? { tvs.first { $0.id == tvID } }

    var body: some View {
        List {
            Section {
                if tvs.isEmpty {
                    Text("No TV is paired with the hub yet.").foregroundStyle(Theme.dim)
                } else {
                    Picker("Play on", selection: $tvID) {
                        ForEach(tvs) { row in
                            Text(row.name + (row.online ? "" : " (offline)")).tag(Optional(row.id))
                        }
                    }
                }
                if let note { Text(note).font(.footnote).foregroundStyle(Theme.dim) }
            } footer: {
                Text("A scene marked “Plays on the TV” loops in the Thomas Creek app. Others open in the YouTube app on the TV while the hub saves them.")
            }
            if let page {
                ForEach(page.categories) { cat in
                    Section(cat.title + (cat.id == page.categories.first?.id ? " · " + (page.reason ?? "") : "")) {
                        ScrollView(.horizontal, showsIndicators: false) {
                            LazyHStack(alignment: .top, spacing: 12) {
                                ForEach(cat.items) { scene in card(scene) }
                            }
                            .padding(.vertical, 4)
                        }
                        .listRowInsets(EdgeInsets(top: 8, leading: 16, bottom: 8, trailing: 0))
                    }
                }
                if canEdit { IdleSection(idle: page.idle, scenes: page.categories.flatMap(\.items)) { await load() } }
                if canEdit {
                    Section {
                        NavigationLink { AmbientSettings() } label: { Label("Seasonal channel scenes", systemImage: "sparkles.tv") }
                    } footer: { Text("The older idle scenes that play a channel from the lineup.") }
                }
            } else if failed {
                Section { Text("The hub didn't send the scenes. Pull to try again.").foregroundStyle(Theme.dim) }
            } else {
                Section { ProgressView() }
            }
        }
        .navigationTitle("Ambiance")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            if canEdit {
                ToolbarItem(placement: .primaryAction) {
                    Button { adding = true } label: { Image(systemName: "plus") }.accessibilityLabel("Add a scene from YouTube")
                }
            }
        }
        .sheet(isPresented: $adding) { NavigationStack { AddAmbianceScene { await load() } } }
        .refreshable { await load() }
        .task { await load() }
    }

    private func card(_ scene: AmbianceScene) -> some View {
        Button { Task { await play(scene) } } label: {
            VStack(alignment: .leading, spacing: 4) {
                HubImage(path: scene.pictureUrl) { Rectangle().fill(Theme.card2) }
                    .frame(width: 220, height: 124)
                    .clipShape(RoundedRectangle(cornerRadius: 10))
                Text(scene.title).font(.subheadline.weight(.semibold)).lineLimit(2).foregroundStyle(Theme.fg)
                Text([scene.status, scene.length].compactMap { $0 }.joined(separator: " · "))
                    .font(.caption).foregroundStyle(scene.state == "ready" ? .green : Theme.dim)
            }
            .frame(width: 220, alignment: .leading)
        }
        .buttonStyle(.plain)
        .contextMenu {
            ForEach(tvs) { row in
                Button("Play on \(row.name)") { Task { await play(scene, on: row) } }
            }
            if canEdit {
                Button("Use when a TV is idle") { Task { await setChoice(scene.id) } }
                Button("Remove from the list", role: .destructive) { Task { await remove(scene) } }
            }
        }
    }

    private func load() async {
        do {
            page = try await HubClient.shared.call("GET", "/v1/ambient/scenes")
            failed = false
        } catch {
            failed = page == nil
            note = (error as? LocalizedError)?.errorDescription ?? "The hub didn't answer."
        }
        if let fresh: HubTvsPage = try? await HubClient.shared.get("/v1/tvs") {
            tvs = fresh.tvs.filter { $0.status == "approved" }
            if tvID == nil || !tvs.contains(where: { $0.id == tvID }) {
                tvID = (tvs.first { $0.online } ?? tvs.first)?.id
            }
        }
    }

    private func play(_ scene: AmbianceScene, on row: HubTvRow? = nil) async {
        guard let target = row ?? tv else { note = "Pick a TV first."; return }
        do {
            let _: TvActionReply = try await HubClient.shared.post("/v1/tvs/\(target.id)/actions",
                                                                       ["action": "play", "ambience_id": scene.id])
            UINotificationFeedbackGenerator().notificationOccurred(.success)
            note = scene.state == "ready" ? "\(scene.title) is on \(target.name)." :
                "\(scene.title) opens in YouTube on \(target.name); the hub is saving it."
        } catch {
            note = (error as? LocalizedError)?.errorDescription ?? "\(target.name) didn't take it."
        }
    }

    private func remove(_ scene: AmbianceScene) async {
        do {
            _ = try await HubClient.shared.raw("DELETE", "/v1/ambient/scenes/\(scene.id)")
            await load()
        } catch { note = (error as? LocalizedError)?.errorDescription ?? "The hub kept it." }
    }

    private func setChoice(_ id: String) async {
        do {
            let _: AmbianceIdle = try await HubClient.shared.call("PUT", "/v1/ambient/idle", body: ["choice": id])
            note = "Idle TVs will show that scene once it's saved."
            await load()
        } catch { note = (error as? LocalizedError)?.errorDescription ?? "The hub kept the old idle scene." }
    }
}

/// House-wide idle screen: on or off, after how long, sound, and which scene.
private struct IdleSection: View {
    let idle: AmbianceIdle
    let scenes: [AmbianceScene]
    let saved: () async -> Void
    @State private var enabled = true
    @State private var after = 10
    @State private var sound = false
    @State private var choice = "season"
    @State private var ready = false

    var body: some View {
        Section {
            Toggle("Show a scene when a TV is idle", isOn: $enabled)
            if enabled {
                Stepper("After \(after) min", value: $after, in: 1...240, step: after < 10 ? 1 : 5)
                Toggle("With sound", isOn: $sound)
                Picker("Scene", selection: $choice) {
                    Text("The season's pick").tag("season")
                    Text("A different one each hour").tag("shuffle")
                    if choice != "season" && choice != "shuffle" {
                        Text(scenes.first { $0.id == choice }?.title ?? "Chosen scene").tag(choice)
                    }
                }
            }
        } header: { Text("Idle screen") } footer: {
            Text("A TV on its Home screen that nobody touched shows the clock over the scene. Only saved scenes play here.")
        }
        .onAppear {
            enabled = idle.enabled; after = idle.afterMin; sound = idle.sound; choice = idle.choice
            DispatchQueue.main.async { ready = true }
        }
        .onChange(of: enabled) { _, _ in save() }
        .onChange(of: after) { _, _ in save() }
        .onChange(of: sound) { _, _ in save() }
        .onChange(of: choice) { _, _ in save() }
    }

    private func save() {
        guard ready else { return }
        Task {
            let body: [String: Any] = ["enabled": enabled, "after_min": after, "sound": sound, "choice": choice]
            _ = try? await HubClient.shared.call("PUT", "/v1/ambient/idle", body: body, as: AmbianceIdle.self)
        }
    }
}

/// Search YouTube through the hub (or paste a link) and put a video in a category.
private struct AddAmbianceScene: View {
    let added: () async -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var query = ""
    @State private var results: [AmbianceScene] = []
    @State private var categories: [AmbianceCategories.Item] = []
    @State private var category = "fireplace"
    @State private var busy = false
    @State private var note: String?

    var body: some View {
        List {
            Section {
                Picker("Category", selection: $category) {
                    ForEach(categories) { Text($0.title).tag($0.id) }
                }
                if let note { Text(note).font(.footnote).foregroundStyle(Theme.dim) }
            }
            Section {
                if busy { ProgressView() }
                ForEach(results) { scene in
                    Button { Task { await add(scene) } } label: {
                        HStack(spacing: 12) {
                            HubImage(path: scene.pictureUrl) { Rectangle().fill(Theme.card2) }
                                .frame(width: 112, height: 63)
                                .clipShape(RoundedRectangle(cornerRadius: 8))
                            VStack(alignment: .leading, spacing: 2) {
                                Text(scene.title).font(.subheadline.weight(.semibold)).lineLimit(2).foregroundStyle(Theme.fg)
                                Text([scene.channel, scene.length].compactMap { $0 }.joined(separator: " · "))
                                    .font(.caption).foregroundStyle(Theme.dim)
                            }
                        }
                    }
                }
            } footer: {
                Text("Long videos work best: the hub saves the first half hour and the TV loops it.")
            }
        }
        .searchable(text: $query, prompt: "Search YouTube or paste a link")
        .onSubmit(of: .search) { Task { await search() } }
        .navigationTitle("Add a scene")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Done") { dismiss() } } }
        .task {
            if let list: AmbianceCategories = try? await HubClient.shared.call("GET", "/v1/ambient/categories") {
                categories = list.categories
                if !categories.contains(where: { $0.id == category }), let first = categories.first { category = first.id }
            }
        }
    }

    private func search() async {
        let q = query.trimmingCharacters(in: .whitespaces)
        guard !q.isEmpty else { return }
        busy = true
        defer { busy = false }
        do {
            let r: AmbianceSearch = try await HubClient.shared.call("GET", "/v1/ambient/search", query: ["q": q])
            results = r.items
            note = r.items.isEmpty ? "Nothing found. Try other words, or paste a YouTube link." : nil
        } catch { note = (error as? LocalizedError)?.errorDescription ?? "YouTube didn't answer the hub." }
    }

    private func add(_ scene: AmbianceScene) async {
        do {
            let _: AmbianceScene = try await HubClient.shared.call("POST", "/v1/ambient/scenes",
                                                                    body: ["video": scene.id, "category": category])
            UINotificationFeedbackGenerator().notificationOccurred(.success)
            note = "Added \(scene.title). The hub is saving it for the TVs."
            await added()
        } catch { note = (error as? LocalizedError)?.errorDescription ?? "The hub couldn't add it." }
    }
}
