import SwiftUI

// MARK: - Who's home

/// Faces in a row: lit with a green ring when home, faded when out, plain when the house can't tell.
struct WhosHomeRow: View {
    @Environment(HomeStore.self) private var store
    @State private var family = FamilyStore.shared
    @State private var picked: FamilyMember?

    var body: some View {
        let people = family.family
        Group {
        if !people.isEmpty {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 14) {
                    ForEach(people) { p in
                        Button { picked = p } label: { face(p) }.buttonStyle(.plain)
                    }
                }
                .padding(.horizontal, 4).padding(.vertical, 6)
            }
            .card(padding: 10)
            .popover(item: $picked) { p in
                VStack(alignment: .leading, spacing: 4) {
                    Text(p.first).font(.headline)
                    Text(detail(p)).font(.subheadline).foregroundStyle(Theme.dim)
                }
                .padding().presentationCompactAdaptation(.popover)
            }
            .accessibilityElement(children: .contain)
            .accessibilityLabel("Who's home")
        }
        }
        .task { await family.load() }
    }

    private func face(_ p: FamilyMember) -> some View {
        VStack(spacing: 5) {
            ZStack(alignment: .bottomTrailing) {
                Avatar(name: p.first, color: p.home == false ? Theme.dim.opacity(0.5) : nil, size: 46)
                    .overlay(Circle().stroke(p.home == true ? Color.green : .clear, lineWidth: 2.5).padding(-3))
                    .opacity(p.home == false ? 0.55 : 1)
                if p.home == true {
                    Image(systemName: "house.fill").font(.system(size: 9, weight: .bold)).foregroundStyle(.white)
                        .padding(4).background(Color.green, in: Circle()).offset(x: 3, y: 3)
                }
            }
            Text(p.me == true ? "You" : p.first).font(.caption.weight(.medium)).foregroundStyle(p.home == false ? Theme.dim : Theme.fg)
        }
        .frame(minWidth: 52)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(p.first), \(p.home == true ? "home" : p.home == false ? "out" : "not known")")
    }

    private func detail(_ p: FamilyMember) -> String {
        let since = p.sinceDate.map { " since \($0.formatted(date: .omitted, time: .shortened))" } ?? ""
        switch p.home {
        case true?: return "Home\(since)"
        case false?: return (p.where.map { "At \($0)" } ?? "Out") + since
        default: return "The house can't tell. Turn on Arriving and leaving in Profile."
        }
    }
}

// MARK: - Today on Home

/// A sky-coloured card for the day: headline, weather, next thing, dinner and the kids' chores. Opens Today.
struct TodaySection: View {
    @Environment(Router.self) private var router
    @State private var family = FamilyStore.shared

    var body: some View {
        Button { router.go(.today) } label: { content }
            .buttonStyle(.plain)
            .task { await family.load() }
    }

    @ViewBuilder private var content: some View {
        let t = family.today
        let part = t?.partOfDay ?? SkyGradient.nowPart
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Today").font(.caption.weight(.semibold)).foregroundStyle(.white.opacity(0.8)).textCase(.uppercase)
                    Text(t?.headline ?? "Your day").font(.system(.title2, design: .rounded, weight: .bold)).foregroundStyle(.white)
                }
                Spacer()
                if let w = t?.weather {
                    VStack(alignment: .trailing, spacing: 0) {
                        Image(systemName: w.symbol ?? "cloud.sun.fill").symbolRenderingMode(.multicolor).font(.title)
                        if let temp = w.temp { Text("\(Int(temp.rounded()))°").font(.title3.weight(.semibold)).foregroundStyle(.white) }
                    }
                }
            }
            if let line = t?.weather?.line, !line.isEmpty {
                Text(line).font(.subheadline).foregroundStyle(.white.opacity(0.9))
            }
            HStack(spacing: 8) {
                if let e = nextEvent { chip("calendar", "\(e.isAllDay ? "Today" : clock(e.start)) · \(e.title)") }
                if let d = t?.dinner { chip("fork.knife", d.title) }
                if let k = t?.chores?.kids, !k.isEmpty {
                    chip("star.fill", k.map { "\($0.name) \($0.done)/\($0.total)" }.joined(separator: " · "))
                }
            }
            .lineLimit(1)
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background {
            ZStack {
                SkyGradient(part: part)
                if part == "night" { Stars(count: 18) }
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: 22, style: .continuous))
        .accessibilityElement(children: .combine)
        .accessibilityHint("Opens Today")
    }

    private var nextEvent: CalendarEvent? {
        (family.today?.events ?? []).first { e in e.isAllDay || (ISO.parse(e.end ?? e.start) ?? .distantFuture) > .now }
    }

    private func chip(_ symbol: String, _ text: String) -> some View {
        Label(text, systemImage: symbol).font(.caption.weight(.semibold)).foregroundStyle(.white)
            .padding(.horizontal, 10).padding(.vertical, 6).background(.white.opacity(0.2), in: Capsule())
    }
}

// MARK: - The Today screen

struct TodayView: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    @State private var family = FamilyStore.shared

    var body: some View {
        let t = family.today
        let part = t?.partOfDay ?? SkyGradient.nowPart
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                hero(t, part: part)
                WhosHomeRow()
                if let events = t?.events, !events.isEmpty { timeline(events) }
                if let d = t?.dinner { dinner(d) }
                if let kids = t?.chores?.kids, !kids.isEmpty { kidsChores(kids) }
                if let mine = t?.chores?.mine { myChores(mine) }
                if let beds = t?.kidsBedtimes, !beds.isEmpty { bedtimes(beds) }
                if let trip = t?.trip { tripCard(trip) }
                if t == nil {
                    if let err = family.error {
                        VStack(spacing: 12) {
                            EmptyCard(symbol: "exclamationmark.triangle", title: "Today didn't load", detail: err)
                            Button("Try again") { Task { await family.load(force: true) } }.buttonStyle(.bordered)
                        }
                    } else {
                        LoadingCard()
                    }
                }
            }
            .screen()
        }
        .background(Backdrop())
        .navigationTitle("Today")
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { await family.load(force: true) }
        .task { await family.load(force: true) }
    }

    private func hero(_ t: TodayInfo?, part: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(Date.now.formatted(.dateTime.weekday(.wide).month(.wide).day())).font(.subheadline.weight(.medium))
                .foregroundStyle(.white.opacity(0.85))
            Text(t?.headline ?? greeting(store.me?.firstName)).font(.system(size: 32, weight: .bold, design: .rounded))
                .foregroundStyle(.white).fixedSize(horizontal: false, vertical: true)
            if let w = t?.weather {
                HStack(spacing: 10) {
                    Image(systemName: w.symbol ?? "cloud.sun.fill").symbolRenderingMode(.multicolor).font(.system(size: 38))
                    VStack(alignment: .leading, spacing: 0) {
                        if let temp = w.temp { Text("\(Int(temp.rounded()))°  \(w.condition ?? "")").font(.title3.weight(.semibold)) }
                        if let line = w.line { Text(line).font(.subheadline).opacity(0.9) }
                    }
                }
                .foregroundStyle(.white).padding(.top, 6)
            }
        }
        .padding(20).padding(.top, 30)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background { ZStack { SkyGradient(part: part); if part == "night" { Stars() } } }
        .clipShape(RoundedRectangle(cornerRadius: 26, style: .continuous))
        .padding(.top, 6)
    }

    private func timeline(_ events: [CalendarEvent]) -> some View {
        let sorted = events.sorted { ($0.isAllDay ? 0 : 1, ISO.parse($0.start) ?? .distantFuture) < ($1.isAllDay ? 0 : 1, ISO.parse($1.start) ?? .distantFuture) }
        return VStack(alignment: .leading, spacing: 0) {
            SectionHeader(title: "On the calendar") { MoreButton("Calendar") { router.show(.family, .calendar) } }
            VStack(alignment: .leading, spacing: 0) {
                ForEach(sorted, id: \.stableID) { e in
                    let past = !e.isAllDay && (ISO.parse(e.end ?? e.start) ?? .distantFuture) < .now
                    HStack(alignment: .top, spacing: 12) {
                        VStack(spacing: 0) {
                            Circle().fill(past ? Theme.dim.opacity(0.4) : Theme.accent).frame(width: 10, height: 10).padding(.top, 5)
                            Rectangle().fill(Theme.line).frame(width: 2)
                        }
                        VStack(alignment: .leading, spacing: 2) {
                            Text(e.isAllDay ? "All day" : clock(e.start)).font(.caption.monospacedDigit()).foregroundStyle(Theme.dim)
                            Text(e.title).font(.body.weight(.medium)).foregroundStyle(past ? Theme.dim : Theme.fg)
                            if let w = e.who { Text(w).font(.caption).foregroundStyle(Theme.dim) }
                            if let l = e.location { Label(l, systemImage: "mappin").font(.caption).foregroundStyle(Theme.dim) }
                        }
                        .padding(.bottom, 12)
                        Spacer(minLength: 0)
                    }
                }
            }
            .card()
        }
    }

    private func dinner(_ d: TodayInfo.TodayDinner) -> some View {
        Button { router.show(.family, d.slug.map { .recipe($0) } ?? .meals) } label: {
            ZStack(alignment: .bottomLeading) {
                HubImage(path: d.imageUrl) { LinearGradient(colors: [.orange.opacity(0.7), .red.opacity(0.6)], startPoint: .topLeading, endPoint: .bottomTrailing) }
                    .frame(height: 150).frame(maxWidth: .infinity).clipped()
                LinearGradient(colors: [.clear, .black.opacity(0.6)], startPoint: .center, endPoint: .bottom)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Dinner\(d.time.map { " · \($0)" } ?? "")").font(.caption.weight(.semibold)).opacity(0.85)
                    Text(d.title).font(.title3.weight(.bold))
                }
                .foregroundStyle(.white).padding(14)
            }
            .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
        }
        .buttonStyle(.plain)
    }

    private func kidsChores(_ kids: [TodayInfo.TodayChores.Kid]) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            SectionHeader(title: "Chores") { MoreButton { router.show(.family, .chores) } }
            HStack(spacing: 12) {
                ForEach(kids) { k in
                    VStack(spacing: 6) {
                        ZStack {
                            ProgressRing(value: k.total == 0 ? 0 : Double(k.done) / Double(k.total), lineWidth: 8, color: Theme.kid)
                            Text("\(k.done)/\(k.total)").font(.headline.monospacedDigit())
                        }
                        .frame(width: 66, height: 66)
                        Text(k.name).font(.subheadline.weight(.semibold))
                        Text("\(k.points) pts").font(.caption).foregroundStyle(Theme.dim)
                    }
                    .frame(maxWidth: .infinity)
                }
            }
            .card()
        }
    }

    private func myChores(_ m: TodayInfo.TodayChores.Mine) -> some View {
        HStack(spacing: 14) {
            ZStack {
                ProgressRing(value: m.total == 0 ? 0 : Double(m.done) / Double(m.total), lineWidth: 9, color: Theme.kid)
                Text("\(m.done)/\(m.total)").font(.headline)
            }
            .frame(width: 70, height: 70)
            VStack(alignment: .leading, spacing: 2) {
                Text(m.done >= m.total && m.total > 0 ? "All done! 🎉" : "Chores").font(.headline)
                if let n = m.next { Text("Next: \(n)").font(.subheadline).foregroundStyle(Theme.dim) }
                Text("\(m.points) points").font(.caption).foregroundStyle(Theme.dim)
            }
            Spacer()
        }
        .card()
    }

    private func bedtimes(_ beds: [TodayInfo.KidBedtime]) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            SectionHeader(title: "Bedtimes") { MoreButton("Change") { router.go(.bedtimes) } }
            VStack(spacing: 8) {
                ForEach(beds) { b in
                    HStack {
                        Image(systemName: b.active == true ? "moon.zzz.fill" : "moon.stars").foregroundStyle(.indigo)
                        Text(b.name).font(.subheadline.weight(.semibold))
                        Spacer()
                        Text(b.enabled == false ? "Off" : b.active == true ? "Asleep until \(b.wakeLabel ?? "morning")" : b.label ?? "")
                            .font(.subheadline).foregroundStyle(Theme.dim)
                    }
                }
            }
            .card()
        }
    }

    private func tripCard(_ trip: TodayInfo.TodayTrip) -> some View {
        Button { if let id = trip.id { router.show(.family, .trip(id)) } } label: {
            HStack(spacing: 12) {
                Text("🧳").font(.system(size: 34))
                VStack(alignment: .leading, spacing: 2) {
                    Text(trip.title).font(.headline).foregroundStyle(Theme.fg)
                    Text(trip.status == "now" ? "Happening now" : trip.daysUntil.map { $0 == 1 ? "Tomorrow" : "In \($0) days" } ?? "Coming up")
                        .font(.subheadline).foregroundStyle(Theme.dim)
                }
                Spacer()
                Image(systemName: "chevron.right").foregroundStyle(Theme.dim)
            }
            .card()
        }
        .buttonStyle(.plain)
    }
}
