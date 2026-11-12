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
                Section {
                    NavigationLink { OfflineStorageView() } label: {
                        LabeledContent("Storage and downloads", value: byteSize(OfflineCache.usage().reduce(0) { $0 + $1.bytes }))
                    }
                } footer: { Text("What's saved for when there's no connection: downloads, trips and the last copy of each screen.") }
                if store.me?.can(.holidayFull) == true || store.me?.can(.holidayLooks) == true {
                    LightsSettingsSection()
                }
                Section("This \(UIDevice.current.model)") {
                    LabeledContent("Name", value: store.me?.device?.name ?? AppSettings.deviceName)
                    LabeledContent("Connection", value: store.reachable ? (store.isLive ? "Live" : "Connected") : "Can't reach home")
                    if VPN.shared.isAvailable { LabeledContent("Away from home", value: vpnLine) }
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
        default: "Connects when you leave home"
        }
    }

    private var version: String {
        let v = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? ""
        let b = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? ""
        return "\(v) (\(b))"
    }
}
