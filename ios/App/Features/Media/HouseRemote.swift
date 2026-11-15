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
        .accessibilityHint("Opens this TV: watch, remote and apps. Drag onto another room to keep watching there.")
        .accessibilityAddTraits(.isButton)
    }
}

private func housePlaying(_ playing: TVPlaying) -> HousePlaying {
    HousePlaying(channelKey: playing.channelKey, title: playing.title, name: playing.name, number: playing.number,
                 positionS: playing.positionS, posterUrl: playing.posterUrl)
}
