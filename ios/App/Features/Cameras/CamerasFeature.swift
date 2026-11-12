import SwiftUI

// MARK: - Cameras section (Frigate only)

/// Events first: what the cameras saw, newest at the top. The second tab is the camera grid,
/// where very wide cameras get a full-width row at their real shape instead of a squashed tile.
struct CamerasView: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    enum Tab: String, CaseIterable { case events = "Events", cameras = "Cameras" }
    @State private var tab: Tab = .events
    @State private var cameras: [Camera]?
    @State private var events: [CameraEvent]?
    @State private var nextBefore: String?
    @State private var cameraFilter: String?
    @State private var labelFilter: String?
    @State private var loadingMore = false

    private var allCameras: [Camera] { cameras ?? store.home?.cameras ?? [] }
    private var labels: [String] {
        Array(Set((events ?? []).map(\.label))).sorted()
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                Picker("View", selection: $tab) {
                    ForEach(Tab.allCases, id: \.self) { Text($0.rawValue).tag($0) }
                }
                .pickerStyle(.segmented)
                switch tab {
                case .events: eventsFeed
                case .cameras: CameraGrid(cameras: allCameras)
                }
            }
            .padding(.top, 8).screen()
        }
        .background(Backdrop())
        .navigationTitle("Cameras")
        .refreshable { await reload() }
        .task { if let list: CameraList = try? await HubClient.shared.get("/v1/cameras") { cameras = list.cameras } }
        .task(id: "\(cameraFilter ?? "")|\(labelFilter ?? "")") { await loadEvents(reset: true) }
        .onChange(of: store.recentEvents?.first?.id) { _, _ in
            // A new detection arrived over the live stream; show it at the top when it matches the filters.
            guard let e = store.recentEvents?.first, matches(e), events?.contains(where: { $0.id == e.id }) == false else { return }
            events?.insert(e, at: 0)
        }
    }

    @ViewBuilder private var eventsFeed: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                Menu {
                    Button("All cameras") { cameraFilter = nil }
                    ForEach(allCameras) { c in Button(c.name) { cameraFilter = c.id } }
                } label: { FilterPill(title: allCameras.first { $0.id == cameraFilter }?.name ?? "All cameras", active: cameraFilter != nil) }
                ChipButton(title: "Everything", active: labelFilter == nil) { labelFilter = nil }
                ForEach(labels.isEmpty ? ["person", "car", "dog", "cat", "package"] : labels, id: \.self) { l in
                    ChipButton(title: l.capitalized, active: labelFilter == l) { labelFilter = labelFilter == l ? nil : l }
                }
            }
        }
        .scrollClipDisabled()
        if let events {
            if events.isEmpty {
                EmptyCard(symbol: "video.badge.checkmark", title: "Nothing spotted",
                          detail: "When the cameras see a person, car or animal it shows up here.")
            } else {
                ForEach(groupedByDay(events), id: \.0) { day, items in
                    SectionHeader(day)
                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 160), spacing: 10)], spacing: 10) {
                        ForEach(items) { e in
                            Button { router.go(.cameraEvent(e.id)) } label: { EventCard(event: e, showCamera: cameraFilter == nil) }
                                .buttonStyle(.plain)
                                .onAppear { if e.id == events.last?.id { Task { await loadEvents(reset: false) } } }
                        }
                    }
                }
                if loadingMore { ProgressView().frame(maxWidth: .infinity).padding() }
            }
        } else { LoadingCard() }
    }

    private func matches(_ e: CameraEvent) -> Bool {
        (cameraFilter == nil || e.camera == cameraFilter) && (labelFilter == nil || e.label == labelFilter)
    }

    private func reload() async {
        if let list: CameraList = try? await HubClient.shared.get("/v1/cameras") { cameras = list.cameras }
        await loadEvents(reset: true)
    }

    private func loadEvents(reset: Bool) async {
        if !reset { guard nextBefore != nil, !loadingMore else { return }; loadingMore = true }
        defer { loadingMore = false }
        var q = ["limit": "40"]
        if let cameraFilter { q["camera"] = cameraFilter }
        if let labelFilter { q["label"] = labelFilter }
        if !reset, let nextBefore { q["before"] = nextBefore }
        guard let page: CameraEvents = try? await HubClient.shared.get("/v1/cameras/events", q) else {
            if reset && events == nil { events = [] }
            return
        }
        events = reset ? page.items : (events ?? []) + page.items.filter { n in !(events ?? []).contains { $0.id == n.id } }
        nextBefore = page.nextBefore
    }
}

func groupedByDay(_ events: [CameraEvent]) -> [(String, [CameraEvent])] {
    let cal = Calendar.current
    var out: [(String, [CameraEvent])] = []
    for e in events {
        let d = e.date
        let key = cal.isDateInToday(d) ? "Today" : cal.isDateInYesterday(d) ? "Yesterday"
            : d.formatted(.dateTime.weekday(.wide).month().day())
        if let i = out.firstIndex(where: { $0.0 == key }) { out[i].1.append(e) } else { out.append((key, [e])) }
    }
    return out
}

struct FilterPill: View {
    let title: String
    let active: Bool
    var body: some View {
        HStack(spacing: 4) { Text(title); Image(systemName: "chevron.down").font(.caption2.weight(.bold)) }
            .font(.subheadline.weight(.semibold))
            .padding(.horizontal, 12).frame(minHeight: 36)
            .foregroundStyle(active ? Theme.onAccent : Theme.fg)
            .background(active ? Theme.accent : Theme.card2, in: Capsule())
    }
}

/// A detection: its snapshot, what it was, which camera, and when.
struct EventCard: View {
    let event: CameraEvent
    var showCamera = true
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ZStack(alignment: .topLeading) {
                HubImage(path: event.thumbUrl ?? event.snapshotUrl) {
                    Theme.card2.overlay(Image(systemName: event.symbol).foregroundStyle(Theme.dim))
                }
                .frame(maxWidth: .infinity).frame(height: 96)
                .clipped()
                Label(event.title, systemImage: event.symbol)
                    .font(.caption2.weight(.bold)).labelStyle(.titleAndIcon)
                    .padding(.horizontal, 7).padding(.vertical, 3)
                    .background(.black.opacity(0.55), in: Capsule())
                    .foregroundStyle(.white).padding(6)
                if event.end == nil {
                    Text("NOW").font(.caption2.weight(.heavy)).padding(.horizontal, 6).padding(.vertical, 2)
                        .background(Theme.warn, in: Capsule()).foregroundStyle(.black)
                        .padding(6).frame(maxWidth: .infinity, alignment: .topTrailing)
                }
            }
            .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
            VStack(alignment: .leading, spacing: 1) {
                if showCamera { Text(event.cameraName ?? event.camera ?? "Camera").font(.caption.weight(.semibold)).lineLimit(1) }
                Text(event.date.formatted(.relative(presentation: .named))).font(.caption2).foregroundStyle(Theme.dim)
            }
            .padding(.horizontal, 2)
        }
        .padding(6)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
    }
}

/// Normal cameras two across; wide ones (panoramic, dual-lens) take a full row at their true shape.
struct CameraGrid: View {
    @Environment(Router.self) private var router
    let cameras: [Camera]
    var body: some View {
        if cameras.isEmpty {
            EmptyCard(symbol: "video.slash", title: "No cameras yet", detail: "Cameras come from Frigate once the hub is connected to it.")
        } else {
            VStack(spacing: 10) {
                ForEach(rows, id: \.first!.id) { row in
                    HStack(spacing: 10) {
                        ForEach(row) { c in
                            Button { router.go(.camera(c.id)) } label: {
                                CameraThumb(camera: c)
                                    .aspectRatio(row.count == 1 ? c.aspect : 16 / 10, contentMode: .fit)
                                    .overlay(alignment: .topTrailing) {
                                        if c.online == false {
                                            Text("Offline").font(.caption2.weight(.bold)).padding(.horizontal, 6).padding(.vertical, 2)
                                                .background(.black.opacity(0.6), in: Capsule()).foregroundStyle(.white).padding(8)
                                        }
                                    }
                            }
                            .buttonStyle(.plain)
                        }
                        if row.count == 1 && !row[0].isWide { Color.clear.aspectRatio(16 / 10, contentMode: .fit) }
                    }
                }
            }
        }
    }

    private var rows: [[Camera]] {
        var out: [[Camera]] = []
        var pending: Camera?
        for c in cameras {
            if c.isWide { out.append([c]); continue }
            if let p = pending { out.append([p, c]); pending = nil } else { pending = c }
        }
        if let p = pending { out.append([p]) }
        return out
    }
}

// MARK: - One camera

/// Live view through Frigate (a short-lived pass from the hub), at the camera's real shape, then its recent events.
struct CameraView: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    let cameraID: String
    @State private var camera: Camera?
    @State private var events: [CameraEvent]?
    @State private var error: String?
    @State private var playerURL: URL?
    @State private var reload = 0
    @State private var fullScreen = false

    var body: some View {
        let cam = camera ?? store.home?.cameras?.first { $0.id == cameraID }
        let aspect = cam?.aspect ?? 16 / 9
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                live(cam, aspect: aspect)
                if let error {
                    Label(error, systemImage: "exclamationmark.triangle.fill").font(.footnote).foregroundStyle(Theme.warn)
                }
                SectionHeader("Recent events")
                if let events {
                    if events.isEmpty {
                        EmptyCard(symbol: "video.badge.checkmark", title: "Nothing spotted lately")
                    } else {
                        LazyVGrid(columns: [GridItem(.adaptive(minimum: 160), spacing: 10)], spacing: 10) {
                            ForEach(events) { e in
                                Button { router.go(.cameraEvent(e.id)) } label: { EventCard(event: e, showCamera: false) }
                                    .buttonStyle(.plain)
                            }
                        }
                    }
                } else { LoadingCard() }
            }
            .padding(.top, 8).screen()
        }
        .background(Backdrop())
        .navigationTitle(cam?.name ?? "Camera")
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button { reload += 1 } label: { Image(systemName: "arrow.clockwise") }.accessibilityLabel("Reload live view")
            }
        }
        .fullScreenCover(isPresented: $fullScreen) {
            ZStack(alignment: .topTrailing) {
                Color.black.ignoresSafeArea()
                if let playerURL { WebPlayer(url: playerURL, reload: reload).aspectRatio(aspect, contentMode: .fit).frame(maxHeight: .infinity) }
                Button { fullScreen = false } label: {
                    Image(systemName: "xmark.circle.fill").font(.title).symbolRenderingMode(.hierarchical).foregroundStyle(.white)
                }
                .padding()
            }
        }
        .task { await start(cam) }
    }

    @ViewBuilder private func live(_ cam: Camera?, aspect: Double) -> some View {
        ZStack(alignment: .bottomTrailing) {
            if let url = playerURL {
                WebPlayer(url: url, reload: reload)
            } else if let cam {
                CameraThumb(camera: cam).overlay { if error == nil { ProgressView().tint(.white) } }
            } else { Theme.card2 }
            if playerURL != nil {
                Button { fullScreen = true } label: {
                    Image(systemName: "arrow.up.left.and.arrow.down.right").font(.caption.weight(.bold))
                        .padding(8).background(.black.opacity(0.5), in: Circle()).foregroundStyle(.white)
                }
                .padding(8).accessibilityLabel("Full screen")
            }
        }
        .aspectRatio(aspect, contentMode: .fit)
        .frame(maxWidth: .infinity)
        .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
    }

    private func start(_ cam: Camera?) async {
        if camera == nil, let list: CameraList = try? await HubClient.shared.get("/v1/cameras") {
            camera = list.cameras.first { $0.id == cameraID }
        }
        do {
            let p: CameraPass = try await HubClient.shared.post("/v1/cameras/\(cameraID)/pass", [:], confirm: "Watch \(cam?.name ?? "the camera")")
            playerURL = await HubClient.shared.resolve(p.playerUrl)
        } catch { self.error = "Live view isn't available right now. \(error.localizedDescription)" }
        let e: CameraEvents? = try? await HubClient.shared.get("/v1/cameras/events", ["camera": cameraID, "limit": "30"])
        events = e?.items ?? []
    }
}

// MARK: - One event

/// The snapshot, what Frigate saw, and the clip.
struct CameraEventView: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    let eventID: String
    @State private var event: CameraEvent?
    @State private var failed = false
    @State private var playClip = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                if let e = event {
                    ZStack {
                        if playClip, let clip = e.clipUrl {
                            ClipPlayer(path: clip)
                        } else {
                            HubImage(path: e.snapshotUrl ?? e.thumbUrl, contentMode: .fit) { Theme.card2 }
                            if e.clipUrl != nil {
                                Button { playClip = true } label: {
                                    Image(systemName: "play.circle.fill").font(.system(size: 54)).symbolRenderingMode(.hierarchical)
                                        .foregroundStyle(.white)
                                }
                                .accessibilityLabel("Play clip")
                            }
                        }
                    }
                    .frame(maxWidth: .infinity, minHeight: 200)
                    .background(.black)
                    .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
                    RowsCard {
                        detail("What", e.title + (e.score.map { " · \(Int($0 * 100))% sure" } ?? ""), symbol: e.symbol)
                        detail("Camera", e.cameraName ?? e.camera ?? "Camera", symbol: "video.fill")
                        detail("When", e.date.formatted(date: .abbreviated, time: .shortened), symbol: "clock")
                        if let end = e.end.flatMap(ISO.parse) {
                            detail("Lasted", Duration.seconds(max(1, end.timeIntervalSince(e.date))).formatted(.units(allowed: [.minutes, .seconds], width: .abbreviated)), symbol: "timer")
                        }
                        if let z = e.zones, !z.isEmpty { detail("Zones", z.joined(separator: ", ").capitalized, symbol: "square.dashed") }
                    }
                    if let cam = e.camera {
                        Button { router.go(.camera(cam)) } label: {
                            Label("Watch \(e.cameraName ?? "this camera") live", systemImage: "dot.radiowaves.left.and.right")
                                .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.borderedProminent).tint(Theme.accent).controlSize(.large)
                    }
                } else if failed {
                    EmptyCard(symbol: "video.slash", title: "That event is gone", detail: "Frigate may have cleaned it up already.")
                } else { LoadingCard() }
            }
            .padding(.top, 8).screen()
        }
        .background(Backdrop())
        .navigationTitle(event?.title ?? "Event")
        .navigationBarTitleDisplayMode(.inline)
        .task {
            if let e = store.recentEvents?.first(where: { $0.id == eventID }) { event = e; return }
            if let e: CameraEvent = try? await HubClient.shared.get("/v1/cameras/events/\(eventID)") { event = e } else { failed = true }
        }
    }

    private func detail(_ k: String, _ v: String, symbol: String) -> some View {
        HStack(spacing: 12) {
            Image(systemName: symbol).foregroundStyle(Theme.accent).frame(width: 22)
            Text(k).foregroundStyle(Theme.dim)
            Spacer()
            Text(v).multilineTextAlignment(.trailing)
        }
        .font(.subheadline).padding(.vertical, 6)
    }
}
