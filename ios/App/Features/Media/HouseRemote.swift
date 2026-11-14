import SwiftUI
import TVPhone

/// One card per room. Tap for the remote. Drag onto another room to keep watching there.
struct HouseTVSection: View {
    @Environment(HomeStore.self) private var store

    var body: some View {
        let tvs = store.houseTVs
        if !tvs.isEmpty {
            SectionHeader("TVs", live: true)
            ForEach(tvs) { tv in
                HouseTVCard(tv: tv)
            }
        }
    }
}

struct HouseTVCard: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    var tv: TVPlayer
    @State private var targeted = false

    var body: some View {
        let playing = tv.playing
        let asleep = TVRemoteCopy.status(online: tv.online, playing: playing.map(housePlaying))
        VStack(alignment: .leading, spacing: 12) {
            Button {
                UIImpactFeedbackGenerator(style: .light).impactOccurred()
                router.go(.houseRemote(tv.id))
            } label: {
            HStack(spacing: 12) {
                HubImage(path: playing?.posterUrl) {
                    Image(systemName: "tv").font(.title2).foregroundStyle(Theme.accent)
                        .frame(maxWidth: .infinity, maxHeight: .infinity).background(Theme.card2)
                }
                .frame(width: 72, height: 54)
                .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                VStack(alignment: .leading, spacing: 3) {
                    Text(tv.name).font(.headline).foregroundStyle(Theme.fg).lineLimit(1)
                    Text(asleep ?? TVRemoteCopy.title(playing.map(housePlaying)))
                        .font(.subheadline).foregroundStyle(Theme.dim).lineLimit(1)
                    if let number = playing?.number {
                        Text("\(number)").font(.caption.monospacedDigit()).foregroundStyle(Theme.dim)
                    }
                }
                Spacer(minLength: 0)
                Image(systemName: "chevron.right").font(.caption.weight(.semibold)).foregroundStyle(Theme.dim)
            }
            }
            .buttonStyle(.plain)
            MoveToButton(from: tv.id, title: playing?.title ?? playing?.name ?? "It")
        }
        .card()
        .overlay {
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .stroke(Theme.accent, lineWidth: targeted ? 2 : 0)
        }
        .draggable(tv.id) {
            Text(tv.name).font(.headline).padding(.horizontal, 16).padding(.vertical, 10)
                .background(Theme.card, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        }
        .dropDestination(for: String.self) { items, _ in
            guard let from = items.first, from != tv.id else { return false }
            UIImpactFeedbackGenerator(style: .medium).impactOccurred()
            let title = store.houseTVs.first { $0.id == from || $0.mediaEntity == from }?.playing?.title ?? "It"
            store.moveTV(from: from, to: tv, title: title)
            return true
        } isTargeted: { targeted = $0 }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(tv.name), \(asleep ?? TVRemoteCopy.title(playing.map(housePlaying)))")
        .accessibilityHint("Opens the remote. Drag onto another room to keep watching there.")
        .accessibilityAddTraits(.isButton)
    }
}

/// Play, pause, a scrub bar, channel buttons, search, back and home.
struct HouseRemoteView: View {
    @Environment(HomeStore.self) private var store
    let deviceID: String
    @State private var paused = false
    @State private var position: Double = 0
    @State private var dragging = false
    @State private var query = ""
    @FocusState private var typing: Bool

    private var device: TVPlayer? { store.houseTVs.first { $0.id == deviceID } }

    var body: some View {
        let tv = device
        let upper = max(TVRemoteCopy.scrub(position: position).upper, 1)
        ScrollView {
            VStack(spacing: 22) {
                HubImage(path: tv?.playing?.posterUrl) {
                    Image(systemName: "tv").font(.largeTitle).foregroundStyle(Theme.accent)
                        .frame(maxWidth: .infinity, maxHeight: .infinity).background(Theme.card2)
                }
                .frame(maxWidth: .infinity).frame(height: 180)
                .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))

                VStack(spacing: 4) {
                    Text(tv?.name ?? "TV").font(.title2.weight(.bold))
                    Text(line(tv)).font(.subheadline).foregroundStyle(Theme.dim).lineLimit(1)
                }

                HStack(spacing: 18) {
                    Button { toggle(tv) } label: {
                        Image(systemName: paused ? "play.fill" : "pause.fill")
                            .font(.title2).frame(width: 64, height: 64)
                            .background(Theme.card, in: Circle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel(paused ? "Play" : "Pause")
                    Text(TVRemoteCopy.clock(position)).font(.title3.monospacedDigit()).foregroundStyle(Theme.dim)
                    Spacer()
                }

                Slider(value: $position, in: 0...upper) { editing in
                    dragging = editing
                    if !editing { seek(tv) }
                }
                .tint(Theme.accent)
                .accessibilityLabel("Place in the show")

                HStack(spacing: 16) {
                    round("chevron.down", "Channel down") { send(TVRemoteBody.channelDown(deviceID)) }
                    round("chevron.up", "Channel up") { send(TVRemoteBody.channelUp(deviceID)) }
                }

                HStack(spacing: 10) {
                    TextField("Search", text: $query)
                        .textFieldStyle(.roundedBorder)
                        .submitLabel(.search)
                        .focused($typing)
                        .onSubmit(type)
                    Button(action: type) {
                        Image(systemName: "keyboard").frame(width: 44, height: 44)
                            .background(Theme.card, in: Circle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Send to the TV")
                }

                HStack(spacing: 22) {
                    round("arrow.uturn.backward", "Back") { send(TVRemoteBody.back(deviceID)) }
                    round("house.fill", "Home") { send(TVRemoteBody.home(deviceID)) }
                }
            }
            .padding(.top, 8).screen()
        }
        .background(Backdrop())
        .navigationTitle("Remote")
        .navigationBarTitleDisplayMode(.inline)
        .onAppear { position = TVRemoteCopy.scrub(position: tv?.playing?.positionS).value }
        .onChange(of: tv?.playing?.positionS) { _, next in
            if !dragging { position = TVRemoteCopy.scrub(position: next).value }
        }
    }

    private func line(_ tv: TVPlayer?) -> String {
        if let asleep = TVRemoteCopy.status(online: tv?.online, playing: tv?.playing.map(housePlaying)) { return asleep }
        return TVRemoteCopy.title(tv?.playing.map(housePlaying))
    }

    private func round(_ symbol: String, _ label: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol).font(.title3.weight(.semibold))
                .frame(width: 64, height: 64).background(Theme.card, in: Circle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
    }

    private func toggle(_ tv: TVPlayer?) {
        UIImpactFeedbackGenerator(style: .light).impactOccurred()
        let was = paused
        paused.toggle()
        let body = paused ? TVRemoteBody.pause(deviceID) : TVRemoteBody.play(deviceID)
        Task {
            if let line = await store.press(body) {
                paused = was
                store.show(line)
            }
        }
        _ = tv
    }

    private func seek(_ tv: TVPlayer?) {
        UIImpactFeedbackGenerator(style: .light).impactOccurred()
        let was = tv?.playing?.positionS ?? 0
        let sent = position
        Task {
            if let line = await store.press(TVRemoteBody.seek(deviceID, position: sent)) {
                position = TVRemoteCopy.scrub(position: was).value
                store.show(line)
            }
        }
    }

    private func type() {
        guard let body = TVRemoteBody.type(deviceID, query) else { return }
        query = ""
        typing = false
        send(body)
    }

    private func send(_ body: TVRemoteBody) {
        UIImpactFeedbackGenerator(style: .light).impactOccurred()
        Task {
            if let line = await store.press(body) { store.show(line) }
        }
    }
}

private func housePlaying(_ playing: TVPlaying) -> HousePlaying {
    HousePlaying(channelKey: playing.channelKey, title: playing.title, name: playing.name, number: playing.number,
                 positionS: playing.positionS, posterUrl: playing.posterUrl)
}
