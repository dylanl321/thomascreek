import SwiftUI
import NetworkExtension

/// Profile in three parts: You (follows the person to every device), This iPhone (only here), and for
/// Dylan, Admin (View as and the hub address).
struct ProfileView: View {
    @Environment(HomeStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    @State private var alerts = AlertsModel.shared
    @State private var hubLAN = AppSettings.hubLAN
    @State private var editHome = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    HStack(spacing: 14) {
                        Avatar(name: store.me?.person.name ?? "?", color: Color(hexString: store.me?.person.color), size: 54)
                        VStack(alignment: .leading) {
                            Text(store.me?.person.name ?? "Not set up").font(.title3.weight(.semibold))
                            Text(roleLine).foregroundStyle(Theme.dim)
                        }
                    }
                }
                // MARK: You: kept on the hub, the same on every device this person uses.
                Section {
                    if store.role.isGrownUp {
                        Button { editHome = true } label: {
                            LabeledContent {
                                Text(favoritesLine).lineLimit(1)
                            } label: { Label("Favorites and Home", systemImage: "star") }
                        }
                        .foregroundStyle(Theme.fg)
                    }
                    if store.role.isGrownUp {
                        NavigationLink { DoorAlertSettings() } label: { Label("Door on the TV", systemImage: "bell.and.waves.left.and.right") }
                        NavigationLink { ChannelHealthList() } label: { Label("Channel health", systemImage: "antenna.radiowaves.left.and.right") }
                    }
                    if store.role != .child || store.me?.style != .little {
                        NavigationLink { AlertsView() } label: {
                            LabeledContent {
                                Text(PushRegistration.shared.allowed == false ? "Off in iOS Settings" : alerts.summary).lineLimit(1)
                            } label: { Label("Alerts", systemImage: "bell.badge") }
                        }
                    }
                    if store.me?.style != .little {
                        NavigationLink { SuggestionsView() } label: { Label("Suggestions on Home", systemImage: "sparkles") }
                    }
                    Picker(selection: Binding(get: { SettingsStore.shared.string(SettingKey.appearance, "system") },
                                              set: { SettingsStore.shared.set(SettingKey.appearance, $0) })) {
                        Text("Match iPhone").tag("system")
                        Text("Light").tag("light")
                        Text("Dark").tag("dark")
                    } label: { Label("Appearance", systemImage: "circle.lefthalf.filled") }
                    Picker(selection: Binding(get: { SettingsStore.shared.string(SettingKey.textSize, "standard") },
                                              set: { SettingsStore.shared.set(SettingKey.textSize, $0) })) {
                        Text("Standard").tag("standard")
                        Text("Large").tag("large")
                        Text("Larger").tag("larger")
                    } label: { Label("Text size", systemImage: "textformat.size") }
                    Picker(selection: Binding(get: { CatDirector.shared.frequency.rawValue },
                                              set: { SettingsStore.shared.set(SettingKey.cats, $0) })) {
                        ForEach(CatDirector.Frequency.allCases) { Text($0.label).tag($0.rawValue) }
                    } label: { Label("Kiki and Bri", systemImage: "cat.fill") }
                } header: { Text("You") } footer: {
                    Text("These follow you to your other iPhone and iPad. Kiki and Bri wander around the top of the screen now and then; tap one to say hi.")
                }
                FamilySettingsSection()
                if store.me?.can(.holidayFull) == true || store.me?.can(.holidayLooks) == true {
                    LightsSettingsSection()
                }

                // MARK: This device only.
                Section {
                    NavigationLink { OfflineStorageView() } label: {
                        LabeledContent("Storage and downloads", value: byteSize(OfflineCache.usage().reduce(0) { $0 + $1.bytes }))
                    }
                } header: { Text("This \(UIDevice.current.model)") } footer: {
                    Text("What's saved here for when there's no connection. Download quality and Wi-Fi-only follow you; the files stay on this device.")
                }
                if VPN.shared.isAvailable { VPNSection() }
                Section {
                    LabeledContent("Name", value: store.me?.device?.name ?? AppSettings.deviceName)
                    LabeledContent("Connection", value: store.reachable ? (store.isLive ? "Live" : "Connected") : "Can't reach home")
                    LabeledContent("Version", value: version)
                    if AppUpdate.shared.available {
                        Button { AppUpdate.shared.openTestFlight() } label: {
                            Label("Update to \(AppUpdate.shared.latestLabel)", systemImage: "arrow.down.app.fill")
                        }
                    }
                }
                if store.role == .admin {
                    Section {
                        NavigationLink { ViewAsList() } label: { Label("View as…", systemImage: "eye") }
                    } header: { Text("Admin") } footer: {
                        Text("See the app exactly as a kid or guest does. Nothing in the house changes while you look.")
                    }
                    Section {
                        TextField("http://hub.local:8443", text: $hubLAN).keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                            .onSubmit { AppSettings.hubLAN = hubLAN; Task { await store.refreshAll() } }
                        if VPN.shared.isAvailable {
                            Button("Get fresh VPN settings") { Task { do { try await VPN.shared.reinstall(); store.show("Tunnel updated") } catch { store.show(error.localizedDescription, isError: true) } } }
                        }
                    } header: { Text("Hub") } footer: {
                        Text("Found automatically on home Wi-Fi. Only change this if the hub moved.")
                    }
                }
            }
            .navigationTitle("Profile")
            .sheet(isPresented: $editHome) { EditHomeSheet().personalStyle() }
            .toolbar { Button("Done") { dismiss() } }
            .onDisappear { if hubLAN != AppSettings.hubLAN { AppSettings.hubLAN = hubLAN } }
            .task {
                PushRegistration.shared.send()
                await alerts.load()
            }
        }
    }

    private var favoritesLine: String {
        let n = FavoriteKind.allCases.reduce(0) { $0 + SettingsStore.shared.favorites($1).count }
        return n == 0 ? "Arrange Home" : "\(n) starred"
    }

    private var roleLine: String {
        switch store.role {
        case .admin: "Admin"
        case .adult: "Adult"
        case .child: "Kid"
        case .guest: "Guest"
        case .tablet: "Wall tablet"
        case .pending: "Waiting for approval"
        }
    }

    private var vpnLine: String {
        if let p = VPN.shared.problem { return p }
        return switch VPN.shared.status {
        case .connected: "Connected"
        case .connecting, .reasserting: "Connecting"
        case .invalid: "Not set up"
        default: VPN.shared.mode == .off ? "Off" : "Ready"
        }
    }

    private var version: String {
        let v = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? ""
        let b = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? ""
        return "\(v) (\(b))"
    }
}


/// The VPN row: where it goes and whether it's on; the full screen has the buttons.
struct VPNSection: View {
    var body: some View {
        let vpn = VPN.shared
        Section {
            NavigationLink { VPNView(pushed: true) } label: {
                LabeledContent("VPN", value: "\(vpn.exit.title) · \(statusLine)")
            }
        } footer: {
            Text("Home reaches the house from anywhere. Private sends everything through PIA, for hotel and public Wi-Fi.")
        }
    }
    private var statusLine: String {
        if let p = VPN.shared.problem { return p }
        return switch VPN.shared.status {
        case .connected: "Connected"
        case .connecting, .reasserting: "Connecting"
        case .disconnecting: "Disconnecting"
        default: VPN.shared.mode == .off ? "Off" : "Connects away from home"
        }
    }
}
