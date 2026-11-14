import SwiftUI
import TVPhone

/// One pending TV, with the hub's suggested room already chosen. Approve is one tap.
/// A late tap (`ok: false`, `status: "expired"`) is a calm line, never an error.
@MainActor
@Observable
final class TVPairingCenter {
    static let shared = TVPairingCenter()

    private(set) var asks: [TVPairAsk] = []
    /// Room the person picked, when it isn't the suggestion. Keyed by pair id.
    private(set) var roomChoice: [String: String] = [:]
    /// A late tap, or the house not answering. Never a raw error.
    var note: String?
    private var poll: Task<Void, Never>?
    private var arrived: [String: Date] = [:]
    private var noteTask: Task<Void, Never>?

    func start() {
        guard HomeStore.shared.role == .admin, HomeStore.shared.previewing == nil else {
            asks = []
            stop()
            return
        }
        guard poll == nil else { return }
        poll = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh()
                try? await Task.sleep(for: .seconds(8))
            }
        }
    }

    func stop() {
        poll?.cancel()
        poll = nil
    }

    func note(_ event: TVPairEvent) {
        guard HomeStore.shared.role == .admin, HomeStore.shared.previewing == nil else { return }
        if event.status != "pending" {
            asks.removeAll { $0.pairId == event.pairId }
            arrived[event.pairId] = nil
            roomChoice[event.pairId] = nil
            return
        }
        arrived[event.pairId] = .now
        let incoming = TVPairAsk(event)
        if let index = asks.firstIndex(where: { $0.pairId == event.pairId }) {
            let kept = asks[index]
            asks[index] = TVPairAsk(pairId: incoming.pairId, installId: incoming.installId,
                                    name: incoming.name ?? kept.name, model: incoming.model ?? kept.model,
                                    suggestedRoom: incoming.suggestedRoom ?? kept.suggestedRoom,
                                    suggestedRoomName: kept.suggestedRoomName, expiresAt: kept.expiresAt)
        } else {
            asks.append(incoming)
            UINotificationFeedbackGenerator().notificationOccurred(.success)
        }
        Task { await refresh() }
    }

    func refresh() async {
        guard HomeStore.shared.role == .admin, HomeStore.shared.previewing == nil else {
            asks = []
            return
        }
        guard let list: TVPairList = try? await HubClient.shared.get(TVPairPath.list) else { return }
        var next = list.pending
        let known = Set(asks.map(\.pairId))
        if next.contains(where: { !known.contains($0.pairId) && arrived[$0.pairId] == nil }) {
            UINotificationFeedbackGenerator().notificationOccurred(.success)
        }
        let ids = Set(next.map(\.pairId))
        for ask in asks where !ids.contains(ask.pairId) {
            if let at = arrived[ask.pairId], Date().timeIntervalSince(at) < 3 {
                next.append(ask)
            } else {
                roomChoice[ask.pairId] = nil
            }
        }
        asks = next
        roomChoice = roomChoice.filter { id, _ in next.contains { $0.pairId == id } }
    }

    func chooseRoom(_ pairID: String, _ roomID: String) {
        roomChoice[pairID] = roomID
    }

    func roomID(for ask: TVPairAsk) -> String? {
        TVPairCopy.selectedRoom(suggested: ask.suggestedRoom, chosen: roomChoice[ask.pairId])
    }

    func approve(_ ask: TVPairAsk) async {
        UIImpactFeedbackGenerator(style: .medium).impactOccurred()
        let room = roomID(for: ask)
        await decide(ask, path: TVPairPath.approve(ask.pairId), room: room, joined: true)
    }

    /// One tap from the notification. The hub keeps the suggested room when none is sent.
    func approve(pairID: String) async {
        if let ask = asks.first(where: { $0.pairId == pairID }) {
            await approve(ask)
            return
        }
        let standIn = TVPairAsk(pairId: pairID, installId: "", name: nil, model: nil, suggestedRoom: nil,
                                suggestedRoomName: nil, expiresAt: nil)
        await decide(standIn, path: TVPairPath.approve(pairID), room: nil, joined: true)
    }

    func notNow(_ ask: TVPairAsk) async {
        await decide(ask, path: TVPairPath.deny(ask.pairId), room: nil, joined: false)
    }

    func notNow(pairID: String) async {
        if let ask = asks.first(where: { $0.pairId == pairID }) {
            await notNow(ask)
            return
        }
        let standIn = TVPairAsk(pairId: pairID, installId: "", name: nil, model: nil, suggestedRoom: nil,
                                suggestedRoomName: nil, expiresAt: nil)
        await decide(standIn, path: TVPairPath.deny(pairID), room: nil, joined: false)
    }

    private func decide(_ ask: TVPairAsk, path: String, room: String?, joined: Bool) async {
        var body: [String: Any] = [:]
        if let room, !room.isEmpty { body["room"] = room }
        do {
            let reply: TVPairReply = try await HubClient.shared.post(path, body)
            asks.removeAll { $0.pairId == ask.pairId }
            roomChoice[ask.pairId] = nil
            arrived[ask.pairId] = nil
            if let line = reply.calmLine {
                show(line)
            } else if joined, reply.status == "approved" {
                UINotificationFeedbackGenerator().notificationOccurred(.success)
                let name = ask.name?.trimmingCharacters(in: .whitespacesAndNewlines)
                HomeStore.shared.show(name.map { "\($0) joined" } ?? "Joined")
            }
        } catch HubClient.HubError.cancelled {
        } catch HubClient.HubError.unreachable {
            show("Can't reach the house right now.")
        } catch {
            show("That didn't go through.")
        }
    }

    private func show(_ line: String) {
        note = line
        noteTask?.cancel()
        noteTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(2.8))
            if !Task.isCancelled { self?.note = nil }
        }
    }
}

struct TVPairBanner: View {
    @Environment(HomeStore.self) private var store
    @Environment(TVPairingCenter.self) private var center

    var body: some View {
        let asks = center.asks
        if asks.isEmpty, center.note == nil {
            EmptyView()
        } else {
            VStack(spacing: 8) {
                if let note = center.note {
                    Text(note).font(.subheadline).foregroundStyle(Theme.dim).frame(maxWidth: .infinity, alignment: .leading).card()
                }
                ForEach(asks) { ask in
                    card(ask)
                }
            }
            .padding(.horizontal, 16)
            .padding(.top, 8)
            .frame(maxWidth: 720)
            .frame(maxWidth: .infinity)
        }
    }

    private func card(_ ask: TVPairAsk) -> some View {
        let rooms = (store.home?.rooms ?? []).map { TVPairRoom(id: $0.id, name: $0.name) }
        let selected = center.roomID(for: ask)
        let label = TVPairCopy.roomLabel(selected: selected, suggested: ask.suggestedRoom, suggestedName: ask.suggestedRoomName, rooms: rooms)
        return VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Image(systemName: "tv").font(.title3).foregroundStyle(Theme.accent)
                VStack(alignment: .leading, spacing: 2) {
                    Text(TVPairCopy.title(name: ask.name)).font(.headline).foregroundStyle(Theme.fg)
                    if let model = ask.model, !model.isEmpty {
                        Text(model).font(.caption).foregroundStyle(Theme.dim)
                    }
                }
                Spacer(minLength: 0)
            }
            HStack(spacing: 8) {
                Text("Room").font(.subheadline).foregroundStyle(Theme.dim)
                roomMenu(ask, rooms: rooms, selected: selected, label: label)
                Spacer(minLength: 0)
            }
            HStack(spacing: 10) {
                Button("Approve") { Task { await center.approve(ask) } }
                    .buttonStyle(.borderedProminent)
                    .tint(Theme.accent)
                Button("Not now") { Task { await center.notNow(ask) } }
                    .buttonStyle(.bordered)
                    .tint(Theme.dim)
            }
        }
        .card()
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(TVPairCopy.title(name: ask.name)). \(label)")
    }

    @ViewBuilder
    private func roomMenu(_ ask: TVPairAsk, rooms: [TVPairRoom], selected: String?, label: String) -> some View {
        let choices = menuRooms(ask, rooms: rooms)
        if choices.count > 1 {
            Menu {
                ForEach(choices, id: \.id) { room in
                    Button {
                        center.chooseRoom(ask.pairId, room.id)
                    } label: {
                        if room.id == selected { Label(room.name, systemImage: "checkmark") } else { Text(room.name) }
                    }
                }
            } label: {
                HStack(spacing: 4) {
                    Text(label).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.fg)
                    Image(systemName: "chevron.up.chevron.down").font(.caption2).foregroundStyle(Theme.dim)
                }
            }
            .accessibilityLabel("Room, \(label)")
        } else {
            Text(label).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.fg)
        }
    }

    private func menuRooms(_ ask: TVPairAsk, rooms: [TVPairRoom]) -> [TVPairRoom] {
        var out = rooms
        if let id = ask.suggestedRoom, !out.contains(where: { $0.id == id }) {
            out.insert(TVPairRoom(id: id, name: ask.suggestedRoomName ?? id.replacingOccurrences(of: "_", with: " ")), at: 0)
        }
        return out
    }
}
