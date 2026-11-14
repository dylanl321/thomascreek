import SwiftUI

/// Simple mode: big words, big buttons, only the everyday things. For anyone who wants Home
/// without the dashboard (Profile → Simple mode). Everything else is still in the other tabs.
struct SimpleHomeView: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router

    private var doors: [DeviceItem] {
        guard store.me?.can(.doors) == true else { return [] }
        return (store.home?.devices(kind: .garage) ?? []) + (store.home?.devices(kind: .lock) ?? [])
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(Date.now.formatted(.dateTime.weekday(.wide).month(.wide).day())).font(.title3).foregroundStyle(Theme.dim)
                        Text(greeting(store.me?.firstName)).font(.system(.largeTitle, design: .rounded, weight: .bold))
                    }
                    Spacer()
                    Button { router.showProfile = true } label: {
                        Avatar(name: store.me?.person.name ?? "?", color: Color(hexString: store.me?.person.color), size: 52)
                    }
                    .accessibilityLabel("Profile")
                }
                .padding(.top, 12)

                if let line = store.home?.status.line {
                    Label(line, systemImage: store.home?.status.level == "ok" ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                        .font(.title3.weight(.semibold))
                        .foregroundStyle(store.home?.status.level == "ok" ? Theme.accent : Theme.warn)
                        .card()
                }

                WhosHomeRow()
                HouseTVSection()

                if !doors.isEmpty {
                    Text("Doors").font(.title2.weight(.bold)).padding(.top, 6)
                    grid { ForEach(doors) { DeviceTile(device: $0, big: true) } }
                }

                let rooms = store.favoriteRooms
                if !rooms.isEmpty {
                    Text("Lights").font(.title2.weight(.bold)).padding(.top, 6)
                    grid { ForEach(rooms) { RoomTile(room: $0) } }
                }

                if let modes = store.home?.modes, !modes.isEmpty {
                    Text("Modes").font(.title2.weight(.bold)).padding(.top, 6)
                    VStack(spacing: 10) {
                        ForEach(modes) { m in
                            BigRow(symbol: m.symbol ?? "sparkles", title: m.name, color: Theme.accent) {
                                store.perform("mode.run", target: m.id, done: "\(m.name) is on")
                            }
                        }
                    }
                }

                VStack(spacing: 10) {
                    BigRow(symbol: "sun.max.fill", title: "Today", color: .orange) { router.go(.today) }
                    if store.me?.can(.familyEdit) == true {
                        BigRow(symbol: "megaphone.fill", title: "Intercom", color: .purple) { router.go(.intercom) }
                    }
                    BigRow(symbol: "mic.fill", title: "Ask Thomas Creek", color: .blue) { router.showAssistant = true }
                    BigRow(symbol: "envelope.fill", title: store.role == .admin ? "Send yourself a note" : "Tell Dylan", color: .pink) { TellDylan.shared.open() }
                }
                .padding(.top, 6)
            }
            .screen()
        }
        .background(Backdrop())
        .dynamicTypeSize(.xLarge ... .accessibility2)
        .toolbar(.hidden, for: .navigationBar)
        .refreshable { await store.refreshAll() }
    }

    private func grid<C: View>(@ViewBuilder _ c: () -> C) -> some View {
        LazyVGrid(columns: [GridItem(.flexible(), spacing: 12), GridItem(.flexible(), spacing: 12)], spacing: 12, content: c)
    }
}

private struct BigRow: View {
    let symbol: String
    let title: String
    let color: Color
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            HStack(spacing: 14) {
                IconBadge(symbol: symbol, color: color, size: 48)
                Text(title).font(.title3.weight(.semibold)).foregroundStyle(Theme.fg)
                Spacer()
                Image(systemName: "chevron.right").foregroundStyle(Theme.dim)
            }
            .frame(minHeight: 64)
            .card(padding: 12)
        }
        .buttonStyle(.plain)
    }
}
