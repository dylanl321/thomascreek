import SwiftUI

/// One person: home or out, their room, today's plans and chores. Opened by tapping a face in Who's home,
/// a name in Activity or a name on a calendar event.
struct PersonPage: View {
    @Environment(HomeStore.self) private var store
    @State private var family = FamilyStore.shared
    let personID: String

    private var member: FamilyMember? { family.family.first { $0.id == personID } }
    private var person: Person? { store.home?.people?.first { $0.id == personID } }
    private var first: String { member?.first ?? person.map { String($0.name.split(separator: " ").first ?? "") } ?? "Someone" }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                header
                if let room { place(room) }
                if !events.isEmpty {
                    SectionHeader("Today")
                    VStack(spacing: 0) {
                        ForEach(events, id: \.stableID) { e in
                            HStack(spacing: 12) {
                                Text(e.isAllDay ? "All day" : clock(e.start)).font(.footnote.monospacedDigit()).foregroundStyle(Theme.dim)
                                    .frame(width: 64, alignment: .leading)
                                Text(e.title).foregroundStyle(Theme.fg)
                                Spacer(minLength: 0)
                            }
                            .padding(.vertical, 8)
                        }
                    }
                    .card(padding: 14)
                    .links(to: .calendar, "Calendar", container: true)
                }
                if let c = chores {
                    SectionHeader("Chores")
                    HStack(spacing: 12) {
                        IconBadge(symbol: "star.fill", color: Theme.kid, size: 38)
                        VStack(alignment: .leading, spacing: 2) {
                            Text("\(c.done) of \(c.total) done").font(.headline)
                            Text("\(c.points) points").font(.subheadline).foregroundStyle(Theme.dim)
                        }
                        Spacer()
                        Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(Theme.dim)
                    }
                    .card(padding: 14)
                    .links(to: .chores, "Chores")
                }
                if let b = bedtime, b.enabled == true, let t = b.label ?? b.time {
                    SectionHeader("Bedtime")
                    HStack(spacing: 12) {
                        IconBadge(symbol: "moon.stars.fill", color: Theme.dyn(0x3D4FA8, 0x8C9BFF), size: 38)
                        Text(t).font(.headline)
                        Spacer()
                        Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(Theme.dim)
                    }
                    .card(padding: 14)
                    .links(to: .bedtimes, "Bedtimes")
                }
                if store.role == .admin {
                    Label("Devices and access", systemImage: "iphone.gen3").font(.subheadline.weight(.semibold))
                        .frame(maxWidth: .infinity).padding(.vertical, 12)
                        .foregroundStyle(Theme.accent)
                        .links(to: .people, "People")
                }
            }
            .screen()
        }
        .background(Backdrop())
        .navigationTitle(first)
        .navigationBarTitleDisplayMode(.inline)
        .task { await family.load() }
    }

    private var header: some View {
        HStack(spacing: 14) {
            Avatar(name: first, color: Color(hexString: person?.color), size: 60)
                .overlay(Circle().stroke(member?.home == true ? Color.green : .clear, lineWidth: 3).padding(-4))
            VStack(alignment: .leading, spacing: 3) {
                Text(member?.name ?? person?.name ?? first).font(.title2.weight(.bold))
                Text(presence).font(.subheadline).foregroundStyle(Theme.dim)
            }
            Spacer(minLength: 0)
        }
        .card()
    }

    private func place(_ room: Room) -> some View {
        HStack(spacing: 12) {
            IconBadge(symbol: room.symbol ?? "bed.double.fill", color: Theme.accent, size: 38)
            VStack(alignment: .leading, spacing: 2) {
                Text(room.name).font(.headline)
                if let s = room.summary { Text(s).font(.subheadline).foregroundStyle(Theme.dim).lineLimit(1) }
            }
            Spacer()
            Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(Theme.dim)
        }
        .card(padding: 14)
        .links(to: .room(room.id), room.name)
    }

    private var presence: String {
        let since = member?.sinceDate.map { " since \($0.formatted(date: .omitted, time: .shortened))" } ?? ""
        switch member?.home ?? person?.home {
        case true?: return "Home\(since)"
        case false?: return (member?.where.map { "At \($0)" } ?? "Out") + since
        default: return "The house can't tell where they are"
        }
    }

    /// Their own room, when the house knows one ("Theo's room").
    private var room: Room? {
        let rooms = store.home?.rooms ?? []
        if let a = person?.area, let r = rooms.first(where: { $0.id == a }) { return r }
        let n = first.lowercased()
        guard !n.isEmpty else { return nil }
        return rooms.first { $0.name.lowercased().hasPrefix(n + "'s") || $0.name.lowercased().hasPrefix(n + "’s") }
    }

    private var events: [CalendarEvent] {
        let n = first.lowercased()
        return (family.today?.events ?? []).filter { e in
            guard let who = e.who?.lowercased() else { return false }
            return who.contains(n) || who.contains("everyone") || who.contains("family")
        }
    }

    private var chores: TodayInfo.TodayChores.Kid? {
        family.today?.chores?.kids?.first { $0.id == personID || $0.name.lowercased() == first.lowercased() }
    }

    private var bedtime: TodayInfo.KidBedtime? {
        family.today?.kidsBedtimes?.first { $0.id == personID || $0.name.lowercased() == first.lowercased() }
    }

    private func clock(_ iso: String) -> String {
        ISO.parse(iso)?.formatted(date: .omitted, time: .shortened) ?? ""
    }
}
