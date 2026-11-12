import SwiftUI
import NetworkExtension

/// Alerts (own screen), this phone, and (for Dylan) the hub address.
struct ProfileView: View {
    @Environment(HomeStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    @State private var alerts = AlertsModel.shared
    @State private var hubLAN = AppSettings.hubLAN

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
                if store.role != .child || store.me?.style != .little {
                    Section {
                        NavigationLink { AlertsView() } label: {
                            LabeledContent {
                                Text(PushRegistration.shared.allowed == false ? "Off in iOS Settings" : alerts.summary).lineLimit(1)
                            } label: { Label("Alerts", systemImage: "bell.badge") }
                        }
                    } footer: { Text(store.role == .child ? "Chores, requests and quiet hours." : "Doorbell, cameras, garage, leaks and quiet hours. Each person picks their own.") }
                }
                if store.me?.style != .little {
                    Section {
                        NavigationLink { SuggestionsView() } label: { Label("Suggestions on Home", systemImage: "sparkles") }
                    } footer: { Text("The \"Right now\" cards. Turn any kind of suggestion off or back on.") }
                }
                Section {
                    NavigationLink { OfflineStorageView() } label: {
                        LabeledContent("Storage and downloads", value: byteSize(OfflineCache.usage().reduce(0) { $0 + $1.bytes }))
                    }
                } footer: { Text("What's saved for when there's no connection: downloads, trips and the last copy of each screen.") }
                if store.me?.can(.holidayFull) == true || store.me?.can(.holidayLooks) == true {
                    LightsSettingsSection()
                }
                if VPN.shared.isAvailable { VPNSection() }
                Section("This \(UIDevice.current.model)") {
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
            .toolbar { Button("Done") { dismiss() } }
            .onDisappear { if hubLAN != AppSettings.hubLAN { AppSettings.hubLAN = hubLAN } }
            .task {
                PushRegistration.shared.send()
                await alerts.load()
            }
        }
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
