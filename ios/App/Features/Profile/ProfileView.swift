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
                            Button("Reinstall the home tunnel") { Task { do { try await VPN.shared.reinstall(); store.show("Tunnel updated") } catch { store.show(error.localizedDescription, isError: true) } } }
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


/// Turn the away-from-home VPN off, on when away, or always on. Nothing is installed while it's Off.
struct VPNSection: View {
    @Environment(HomeStore.self) private var store
    @State private var mode = VPN.shared.mode
    @State private var busy = false
    var body: some View {
        let vpn = VPN.shared
        Section {
            Picker("Away-from-home VPN", selection: $mode) {
                ForEach(VPN.Mode.allCases) { Text($0.title).tag($0) }
            }
            .disabled(busy)
            .onChange(of: mode) { _, m in
                busy = true
                Task {
                    defer { busy = false }
                    do { try await vpn.setMode(m); store.show(m == .off ? "VPN turned off" : "VPN set to \(m.title.lowercased())") }
                    catch { store.show(error.localizedDescription, isError: true); mode = vpn.mode }
                }
            }
            if mode != .off {
                LabeledContent("Status", value: statusLine)
                if vpn.status == .connected || vpn.status == .connecting {
                    Button("Disconnect for now") { Task { await vpn.disconnectNow() } }
                }
                Button("Get a fresh setup from the hub") {
                    Task { do { try await vpn.reinstall(); store.show("VPN updated") } catch { store.show(error.localizedDescription, isError: true) } } }
            }
        } header: { Text("Away from home") } footer: {
            Text(mode == .away ? "Connects by itself when you leave home Wi-Fi and only carries home traffic. You can also switch it in Settings → VPN."
                 : mode == .always ? "Stays connected everywhere, even at home. Only home traffic goes through it."
                 : "Off: it never connects by itself. Use Connect on Home when you need it.")
        }
    }
    private var statusLine: String {
        if let p = VPN.shared.problem { return p }
        return switch VPN.shared.status {
        case .connected: "Connected"
        case .connecting, .reasserting: "Connecting"
        case .disconnecting: "Disconnecting"
        case .invalid: "Not set up"
        default: mode == .away ? "Waiting until you leave home" : "Not connected"
        }
    }
}
