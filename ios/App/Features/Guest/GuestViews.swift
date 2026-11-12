import SwiftUI

/// A guest staying in the apartment: welcome, Wi-Fi, house guidance and how long access lasts.
struct GuestHomeView: View {
    @Environment(HomeStore.self) private var store
    @State private var message = ""
    @State private var writing = false

    var body: some View {
        let g = store.me?.guest
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                HeroHeader(title: "Welcome, \(store.me?.firstName ?? "")", subtitle: "The Apartment", height: 230)
                if !store.reachable { OfflineCard() }
                if let until = store.me?.person.accessUntil.flatMap(ISO.parse) {
                    HStack(spacing: 12) {
                        IconBadge(symbol: "clock", size: 38)
                        VStack(alignment: .leading, spacing: 2) {
                            Text("Your access ends \(until.formatted(.dateTime.weekday(.abbreviated).month(.abbreviated).day().hour().minute()))")
                                .font(.subheadline.weight(.semibold))
                            Text("Apartment lights, thermostat, TV and door").font(.caption).foregroundStyle(Theme.dim)
                        }
                    }
                    .card()
                }
                if let ssid = g?.wifiSsid {
                    SectionHeader("Wi-Fi")
                    VStack(alignment: .leading, spacing: 12) {
                        LabeledContent("Network") { Text(ssid).font(.headline) }
                        if let pw = g?.wifiPassword {
                            HStack {
                                VStack(alignment: .leading) {
                                    Text("Password").font(.caption).foregroundStyle(Theme.dim)
                                    Text(pw).font(.headline.monospaced())
                                }
                                Spacer()
                                Button { UIPasteboard.general.string = pw; store.show("Wi-Fi password copied") } label: {
                                    Label("Copy", systemImage: "doc.on.doc")
                                }
                                .buttonStyle(.bordered)
                            }
                        }
                    }
                    .card()
                }
                if let tips = g?.guidance, !tips.isEmpty {
                    SectionHeader("Good to know")
                    RowsCard { ForEach(tips) { InfoRow($0.icon ?? "info.circle", $0.title, $0.detail) } }
                }
                PrimaryButton(title: "Message your host", symbol: "message.fill") { writing = true }.padding(.top, 8)
            }
            .screen()
        }
        .background(Backdrop())
        .toolbar(.hidden, for: .navigationBar)
        .alert("Message \(g?.hostName ?? "your host")", isPresented: $writing) {
            TextField("Message", text: $message)
            Button("Send") {
                let t = message; message = ""
                Task {
                    try? await HubClient.shared.send("/v1/requests", ["kind": "message", "text": t])
                    store.show("Message sent", "You'll get the reply here")
                }
            }
            Button("Cancel", role: .cancel) {}
        }
    }
}

/// The guest's area: its lights, thermostat, TV and door lock.
struct MySpaceView: View {
    @Environment(HomeStore.self) private var store
    var body: some View {
        let devices = store.myRoom.map { store.home?.devices(in: $0) ?? [] } ?? (store.home?.devices ?? [])
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                let lights = devices.filter { [.light, .fan, .switch].contains($0.kind) }
                if !lights.isEmpty {
                    SectionHeader("Lights", live: true)
                    LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible(), spacing: 10)], spacing: 10) {
                        ForEach(lights) { DeviceTile(device: $0) }
                    }
                }
                ForEach(devices.filter { $0.kind == .climate }) { d in
                    SectionHeader("Thermostat")
                    ClimateCard(device: d)
                }
                let other = devices.filter { [.media, .lock].contains($0.kind) }
                if !other.isEmpty {
                    SectionHeader("TV and door")
                    LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible(), spacing: 10)], spacing: 10) {
                        ForEach(other) { DeviceTile(device: $0) }
                    }
                }
            }
            .padding(.top, 8).screen()
        }
        .background(Backdrop())
        .navigationTitle(store.myRoom?.name ?? "My Space")
    }
}
