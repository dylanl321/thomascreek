import SwiftUI

/// Profile rows for family life: simple mode, arriving and leaving, welcome scene, Tell Dylan.
/// Placed under "You" in Profile.
struct FamilySettingsSection: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    @State private var arrivals = PresenceMonitor.shared.isOn
    @State private var welcome: WelcomeSetting?

    private var grownUp: Bool { store.role == .admin || store.role == .adult }

    var body: some View {
        Section {
            if grownUp {
                Toggle(isOn: Binding(get: { SettingsStore.shared.bool(SettingKey.simpleMode) },
                                     set: { SettingsStore.shared.set(SettingKey.simpleMode, $0) })) {
                    Label { VStack(alignment: .leading, spacing: 1) {
                        Text("Simple mode")
                        Text("Big buttons for doors, lights and modes on Home").font(.caption).foregroundStyle(Theme.dim)
                    } } icon: { Image(systemName: "textformat.size.larger") }
                }
                Toggle(isOn: $arrivals) {
                    Label { VStack(alignment: .leading, spacing: 1) {
                        Text("Arriving and leaving")
                        Text(PresenceMonitor.shared.needsAlways && arrivals ? "Allow location \"Always\" in Settings for this to work"
                             : "Last one out gets a check of doors and garage").font(.caption).foregroundStyle(Theme.dim)
                    } } icon: { Image(systemName: "location.fill") }
                }
                .onChange(of: arrivals) { _, on in on ? PresenceMonitor.shared.enable() : PresenceMonitor.shared.disable() }
                if arrivals, let w = welcome, let modes = w.modes, !modes.isEmpty {
                    Picker(selection: Binding(get: { w.mode ?? "" }, set: { new in Task { await setWelcome(new.isEmpty ? nil : new) } })) {
                        Text("Nothing").tag("")
                        ForEach(modes) { Text($0.name).tag($0.id) }
                    } label: { Label("When I get home", systemImage: "house.and.flag.fill") }
                }
            }
            Button { TellDylan.shared.open() } label: {
                Label(store.me?.firstName == "Dylan" ? "Send yourself a note" : "Tell Dylan", systemImage: "envelope.badge")
            }
        } header: {
            Text("Family")
        } footer: {
            Text("You can also shake your phone anywhere to tell Dylan something, with a picture of the screen.")
        }
        .task { if grownUp { welcome = try? await HubClient.shared.get("/v1/me/welcome") } }
    }

    private func setWelcome(_ mode: String?) async {
        do {
            let _: Empty = try await HubClient.shared.call("PUT", "/v1/me/welcome", body: ["mode": mode.map { $0 as Any } ?? NSNull()])
            welcome = try? await HubClient.shared.get("/v1/me/welcome")
        } catch { store.show(error.localizedDescription, isError: true) }
    }
}
