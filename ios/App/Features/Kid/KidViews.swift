import SwiftUI
import AVKit
import AVFoundation

/// Reads words aloud for the 5-year-old, who is still learning to read.
@MainActor
enum Speaker {
    private static let synth = AVSpeechSynthesizer()
    static func say(_ text: String) {
        synth.stopSpeaking(at: .immediate)
        let u = AVSpeechUtterance(string: text)
        u.rate = 0.45
        synth.speak(u)
    }
}

struct SayButton: View {
    let text: String
    var body: some View {
        Button { Speaker.say(text) } label: {
            Image(systemName: "speaker.wave.2.fill").font(.title3).padding(10).background(.white.opacity(0.25), in: Circle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Read aloud")
    }
}

/// The house is unreachable: say it kindly.
struct KidNap: View {
    var body: some View {
        HStack(spacing: 12) {
            Text("😴").font(.system(size: 40))
            VStack(alignment: .leading) {
                Text("The house is napping").font(Theme.kidFont(20))
                Text("Your lights still work with the wall switch.").font(.subheadline).foregroundStyle(Theme.dim)
            }
        }
        .card(color: Theme.card2)
    }
}

// MARK: - Home

struct KidHomeView: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    @State private var board: ChoreBoard? = AppSettings.cachedChores

    private var little: Bool { store.me?.style == .little }
    private var color: Color { little ? Theme.little : Theme.kid }
    private var name: String { store.me?.firstName ?? "" }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                header
                if !store.reachable { KidNap() }
                // Dinner and the next trip have their own cards on this screen.
                RightNowSection(hide: ["my_dinner", "trip"], kid: true)
                if store.me?.can(.tripsView) == true { KidTripCard(little: little, color: color) }
                if little { littleHome } else { kidHome }
            }
            .screen()
        }
        .background(Backdrop())
        .toolbar(.hidden, for: .navigationBar)
        .task { await loadChores() }
        .task { if store.me?.can(.tripsView) == true { await TripStore.shared.load() } }
        .refreshable { await loadChores(); await store.refreshAll(); if store.me?.can(.tripsView) == true { await TripStore.shared.load(force: true) } }
    }

    private var header: some View {
        HStack(spacing: 12) {
            Button { router.showProfile = true } label: { Avatar(name: name, color: color, size: 46) }
            VStack(alignment: .leading, spacing: 0) {
                Text(little ? "Hi \(name)!" : greeting(name)).font(Theme.kidFont(little ? 34 : 28))
                if !little { Text(Date.now.formatted(.dateTime.weekday(.wide))).foregroundStyle(Theme.dim) }
            }
            Spacer()
            if little { SayButton(text: "Hi \(name)! You have \(board?.points ?? 0) stars.").foregroundStyle(color) }
            else { BellButton() }
        }
        .padding(.top, 12)
    }

    // 9-year-old: words and pictures.
    @ViewBuilder private var kidHome: some View {
        if let b = board {
            HStack(spacing: 16) {
                ZStack {
                    ProgressRing(value: b.items.isEmpty ? 0 : Double(b.doneCount) / Double(b.items.count), lineWidth: 11, color: color)
                    VStack(spacing: 0) {
                        Text("\(b.doneCount)/\(b.items.count)").font(Theme.kidFont(24))
                        Text("chores").font(.caption).foregroundStyle(Theme.dim)
                    }
                }
                .frame(width: 96, height: 96)
                VStack(alignment: .leading, spacing: 4) {
                    Text("\(b.points)").font(Theme.kidFont(32))
                    Text("points\(b.streak.map { " · \($0)-day streak" } ?? "")").foregroundStyle(Theme.dim)
                    Button("Reward shop") { router.show(.chores) }.buttonStyle(.borderedProminent).tint(color).controlSize(.small)
                }
            }
            .card()
        }
        InternetChip()
        let todo = (board?.items ?? []).filter { !$0.done }
        if !todo.isEmpty {
            SectionHeader(title: "Still to do") { MoreButton("All") { router.show(.chores) } }
            ForEach(todo.prefix(3)) { c in ChoreRow(chore: c, color: color) { await done(c) } }
        }
        if let room = store.myRoom {
            SectionHeader(title: "My room") { MoreButton("More") { router.show(.myRoom) } }
            LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible(), spacing: 10)], spacing: 10) {
                ForEach((store.home?.devices(in: room) ?? []).filter { [.light, .fan].contains($0.kind) }.prefix(4)) { DeviceTile(device: $0) }
            }
        }
        if let d = store.home?.today?.dinner {
            SectionHeader("Dinner tonight")
            HStack(spacing: 12) { Text(d.emoji ?? "🍽️").font(.system(size: 40)); Text(d.title).font(Theme.kidFont(20, .semibold)) }.card()
        }
        if store.holidaySeason {
            Button { router.go(.holiday) } label: {
                Label("Pick tonight's light show", systemImage: "sparkles").font(Theme.kidFont(18, .semibold))
                    .frame(maxWidth: .infinity, minHeight: 56)
            }
            .buttonStyle(.borderedProminent).tint(Color(red: 0.35, green: 0.25, blue: 0.75))
        }
    }

    // 5-year-old: pictures and colour first, words second, everything can be read aloud.
    @ViewBuilder private var littleHome: some View {
        let stars = board?.points ?? 0
        HStack(spacing: 4) {
            ForEach(0..<10, id: \.self) { i in
                Image(systemName: i < min(stars, 10) ? "star.fill" : "star").font(.title2).foregroundStyle(Theme.star)
            }
        }
        .frame(maxWidth: .infinity).card()
        LazyVGrid(columns: [GridItem(.flexible(), spacing: 12), GridItem(.flexible(), spacing: 12)], spacing: 12) {
            BigPictureButton(emoji: "💡", title: "Lights", colors: [.orange, .red]) { router.show(.myRoom) }
            BigPictureButton(emoji: "📺", title: "Shows", colors: [.blue, .indigo]) { router.show(.watch) }
            BigPictureButton(emoji: "⭐", title: "Jobs \(board?.doneCount ?? 0)/\(board?.items.count ?? 0)", colors: [.pink, Theme.little]) { router.show(.chores) }
            if store.holidaySeason {
                BigPictureButton(emoji: "✨", title: "Light show", colors: [.purple, Color(red: 0.23, green: 0.16, blue: 0.56)]) { router.go(.holiday) }
            }
        }
        if let d = store.home?.today?.dinner {
            SectionHeader("Dinner")
            HStack(spacing: 12) {
                Text(d.emoji ?? "🍽️").font(.system(size: 52))
                Text(d.title).font(Theme.kidFont(22))
                Spacer()
                SayButton(text: "Dinner is \(d.title)").foregroundStyle(color)
            }
            .card()
        }
    }

    private func loadChores() async {
        if let b: ChoreBoard = try? await HubClient.shared.get("/v1/chores") { board = b; AppSettings.cachedChores = b }
    }

    private func markDoneOffline(_ c: ChoreBoard.Chore) {
        board?.markDone(c.id); AppSettings.cachedChores = board
    }

    private func done(_ c: ChoreBoard.Chore) async {
        do {
            let sent = try await Outbox.shared.send("/v1/chores/\(c.id)/done", label: "\(c.title) (chore)")
            store.show(little ? "⭐ Great job!" : "Nice! +\(c.points ?? 0) points", sent ? nil : "It's sent when home is back")
            if little { Speaker.say("Great job!") }
            if sent { await loadChores() } else { markDoneOffline(c) }
            AppSettings.refreshSurfaces()
        } catch { store.show(error.localizedDescription, isError: true) }
    }
}

struct BigPictureButton: View {
    let emoji: String
    let title: String
    let colors: [Color]
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            VStack(spacing: 8) {
                Text(emoji).font(.system(size: 54))
                Text(title).font(Theme.kidFont(22)).foregroundStyle(.white)
            }
            .frame(maxWidth: .infinity, minHeight: 150)
            .background(LinearGradient(colors: colors, startPoint: .topLeading, endPoint: .bottomTrailing), in: RoundedRectangle(cornerRadius: 30, style: .continuous))
            .overlay(alignment: .topTrailing) { SayButton(text: title).foregroundStyle(.white).padding(8) }
        }
        .buttonStyle(.plain)
    }
}

struct ChoreRow: View {
    let chore: ChoreBoard.Chore
    let color: Color
    var big = false
    let done: () async -> Void
    @State private var tapped = false
    var body: some View {
        Button { tapped = true; Task { await done() } } label: {
            HStack(spacing: 14) {
                Text(chore.icon ?? "✅").font(.system(size: big ? 40 : 28))
                VStack(alignment: .leading, spacing: 2) {
                    Text(chore.title).font(Theme.kidFont(big ? 22 : 18, .semibold)).foregroundStyle(Theme.fg)
                        .strikethrough(chore.done && !chore.waiting)
                    if chore.waiting { Text("Waiting for a grown-up to check").font(.caption).foregroundStyle(Theme.dim) }
                    else if chore.locked { Text("Do the one before first").font(.caption).foregroundStyle(Theme.dim) }
                    else if let t = chore.time { Text(t).font(.caption).foregroundStyle(Theme.dim) }
                }
                Spacer()
                if let p = chore.points { Text("+\(p)").font(.headline).foregroundStyle(Theme.star) }
                Image(systemName: chore.locked ? "lock.fill" : chore.waiting ? "hourglass.circle.fill" : chore.done || tapped ? "checkmark.circle.fill" : "circle")
                    .font(.system(size: big ? 34 : 28)).foregroundStyle(color)
                    .symbolEffect(.bounce, value: tapped)
            }
            .card(padding: big ? 18 : 14)
        }
        .buttonStyle(.plain)
        .disabled(chore.done || chore.locked)
        .opacity(chore.locked ? 0.55 : 1)
    }
}

/// "Internet until 8:30 because chores are done", with a way to ask for more.
struct InternetChip: View {
    @Environment(HomeStore.self) private var store
    var body: some View {
        if let net = store.me?.child?.internet {
            Button {
                if !net.paused { store.askParent = .init(what: "Can I have 30 more minutes of internet?", kind: "internet") }
            } label: {
                HStack(spacing: 10) {
                    Image(systemName: net.paused ? "wifi.slash" : "wifi")
                    Text(net.paused ? "Internet is paused by a parent" : "Internet until \(net.until ?? "bedtime")\(net.reason.map { " \($0)" } ?? "")")
                        .font(.subheadline).foregroundStyle(Theme.fg)
                    Spacer()
                    if !net.paused { Text("Ask for more").font(.subheadline.weight(.semibold)).foregroundStyle(Theme.kid) }
                }
                .card(padding: 12)
            }
            .buttonStyle(.plain)
        }
    }
}

// MARK: - My Room

struct MyRoomView: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    var body: some View {
        let little = store.me?.style == .little
        let room = store.myRoom
        let devices = room.map { store.home?.devices(in: $0) ?? [] } ?? []
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if !store.reachable { KidNap() }
                LazyVGrid(columns: [GridItem(.flexible(), spacing: 12), GridItem(.flexible(), spacing: 12)], spacing: 12) {
                    ForEach(devices.filter { [.light, .fan, .switch].contains($0.kind) }) { DeviceTile(device: $0, big: true) }
                }
                ForEach(devices.filter { $0.kind == .climate }) { ClimateCard(device: $0) }
                if let tv = store.me?.child?.tvDeviceId {
                    Button { router.go(.remote(tv)) } label: {
                        Label("My TV remote", systemImage: "appletvremote.gen4").font(Theme.kidFont(20, .semibold))
                            .frame(maxWidth: .infinity, minHeight: 60)
                    }
                    .buttonStyle(.borderedProminent).tint(little ? Theme.little : Theme.kid)
                }
                if let bedtime = store.home?.modes?.first(where: { $0.id.contains("bedtime") || $0.name.lowercased().contains("bedtime") }) {
                    Button { store.perform("mode.run", target: bedtime.id, done: "Good night!") } label: {
                        Label("Bedtime", systemImage: "moon.stars.fill").font(Theme.kidFont(20, .semibold)).frame(maxWidth: .infinity, minHeight: 60)
                    }
                    .buttonStyle(.bordered).tint(.indigo)
                }
                if devices.isEmpty { EmptyCard(symbol: "lightbulb", title: "Your room isn't set up yet", detail: "Ask a parent to pick your room.") }
            }
            .padding(.top, 8).screen()
        }
        .background(Backdrop())
        .navigationTitle(room?.name ?? "My Room")
    }
}

// MARK: - Watch (rated library, built-in player, own TV)

struct KidWatchView: View {
    @Environment(HomeStore.self) private var store
    @State private var items: [MediaItem] = []
    @State private var requests: [MediaRequest] = []
    @State private var picked: MediaItem?
    @State private var asking = false
    @State private var askText = ""

    var body: some View {
        let little = store.me?.style == .little
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                DownloadsEntry(tint: little ? Theme.little : Theme.kid)
                LazyVGrid(columns: [GridItem(.adaptive(minimum: little ? 150 : 120), spacing: 14)], spacing: 16) {
                    ForEach(items) { m in Poster(item: m).onTapGesture { picked = m } }
                }
                if store.me?.can(.mediaRequest) == true {
                    Button { asking = true } label: {
                        Label("Ask for a movie", systemImage: "hand.raised.fill").font(Theme.kidFont(18, .semibold)).frame(maxWidth: .infinity, minHeight: 54)
                    }
                    .buttonStyle(.borderedProminent).tint(little ? Theme.little : Theme.kid)
                }
                if !requests.isEmpty {
                    SectionHeader("My asks")
                    RowsCard {
                        ForEach(requests) { r in InfoRow(r.status == "ready" ? "checkmark.circle.fill" : "hourglass", r.title, RequestStatus.text(r)) }
                    }
                }
            }
            .padding(.top, 8).screen()
        }
        .background(Backdrop())
        .navigationTitle(little ? "Shows" : "Watch")
        .task {
            if let l: MediaList = try? await HubClient.shared.get("/v1/media/library", ["rated": "1"]) { items = l.items }
            if let r: RequestList = try? await HubClient.shared.get("/v1/requests", ["mine": "1"]) { requests = r.items }
        }
        .sheet(item: $picked) { KidPlaySheet(item: $0) }
        .alert("What do you want to watch?", isPresented: $asking) {
            TextField("Movie name", text: $askText)
            Button("Ask") {
                let t = askText; askText = ""
                Task {
                    let sent = (try? await Outbox.shared.send("/v1/requests", ["kind": "media", "text": t], label: "Ask for \(t)")) ?? false
                    store.show("Asked a parent!", sent ? nil : "It's sent when home is back")
                }
            }
            Button("Cancel", role: .cancel) {}
        }
    }
}

struct KidPlaySheet: View {
    @Environment(HomeStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let item: MediaItem
    @State private var player: AVPlayer?

    var body: some View {
        NavigationStack {
        VStack(spacing: 18) {
            if let player {
                VideoPlayer(player: player).aspectRatio(16 / 9, contentMode: .fit).onAppear { player.play() }
            } else {
                Poster(item: item).frame(width: 160)
                Text(item.title).font(Theme.kidFont(26))
                if UIDevice.current.userInterfaceIdiom == .pad {
                    PrimaryButton(title: "Watch here", symbol: "play.fill", color: Theme.kid) { Task { await playHere() } }
                }
                if let tv = store.me?.child?.tvDeviceId, store.home?.device(tv) != nil {
                    PrimaryButton(title: "Play on my TV", symbol: "tv.fill", color: Theme.accent) {
                        if let d = store.home?.device(tv) { store.play(item, on: d) }
                        dismiss()
                    }
                }
                DownloadButton(item: item, tint: store.me?.style == .little ? Theme.little : Theme.kid)
            }
        }
        .padding(24)
        }
        .presentationDetents(player == nil ? [.medium] : [.large])
        .onDisappear { player?.pause() }
    }

    private func playHere() async {
        do {
            let s: StreamInfo = try await HubClient.shared.get("/v1/media/\(item.id)/stream")
            if let url = await HubClient.shared.resolve(s.hlsUrl) { player = AVPlayer(url: url) }
        } catch { store.show(error.localizedDescription, isError: true) }
    }
}

// MARK: - Chores and rewards

struct KidChoresView: View {
    @Environment(HomeStore.self) private var store
    @State private var board: ChoreBoard? = AppSettings.cachedChores

    var body: some View {
        let little = store.me?.style == .little
        let color = little ? Theme.little : Theme.kid
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                if let b = board {
                    HStack {
                        Label("\(b.points)", systemImage: "star.fill").font(Theme.kidFont(30)).foregroundStyle(Theme.star)
                        Spacer()
                        if let s = b.streak, s > 0 { Pill(text: "🔥 \(s) days", color: color) }
                    }
                    .card()
                    ForEach(b.items) { c in ChoreRow(chore: c, color: color, big: little) { await done(c) } }
                    if let rewards = b.rewards, !rewards.isEmpty {
                        SectionHeader(little ? "Treats" : "Reward shop")
                        LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible(), spacing: 10)], spacing: 10) {
                            ForEach(rewards) { r in
                                Button { redeem(r) } label: {
                                    VStack(spacing: 6) {
                                        Text(r.icon ?? "🎁").font(.system(size: 40))
                                        Text(r.title).font(Theme.kidFont(16, .semibold)).foregroundStyle(Theme.fg).multilineTextAlignment(.center)
                                        Label("\(r.cost)", systemImage: "star.fill").font(.caption.weight(.bold)).foregroundStyle(Theme.star)
                                    }
                                    .frame(maxWidth: .infinity, minHeight: 130).card(padding: 10)
                                    .opacity(b.points >= r.cost ? 1 : 0.5)
                                }
                                .buttonStyle(.plain)
                                .disabled(b.points < r.cost)
                            }
                        }
                    }
                } else { LoadingCard() }
            }
            .padding(.top, 8).screen()
        }
        .background(Backdrop())
        .navigationTitle(little ? "Jobs" : "Chores")
        .task { await load() }
        .refreshable { await load() }
    }

    private func load() async {
        if let b: ChoreBoard = try? await HubClient.shared.get("/v1/chores") { board = b; AppSettings.cachedChores = b }
    }

    private func markDoneOffline(_ c: ChoreBoard.Chore) {
        board?.markDone(c.id); AppSettings.cachedChores = board
    }

    private func done(_ c: ChoreBoard.Chore) async {
        do {
            let sent = try await Outbox.shared.send("/v1/chores/\(c.id)/done", label: "\(c.title) (chore)")
            if store.me?.style == .little { Speaker.say("Great job!") }
            if sent { await load() } else { markDoneOffline(c); store.show("Ticked!", "It's sent when home is back") }
            AppSettings.refreshSurfaces()
        } catch { store.show(error.localizedDescription, isError: true) }
    }

    private func redeem(_ r: ChoreBoard.Reward) {
        Task {
            do {
                try await HubClient.shared.send("/v1/rewards/\(r.id)/redeem")
                store.show("Asked a parent for \(r.title)")
                await load()
            } catch { store.show(error.localizedDescription, isError: true) }
        }
    }
}

extension ChoreBoard {
    /// Ticked while offline: show it done (the hub gets it when home is back).
    mutating func markDone(_ id: String) {
        if let i = items.firstIndex(where: { $0.id == id }) { items[i].done = true }
    }
}
