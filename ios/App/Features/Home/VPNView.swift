import SwiftUI
import NetworkExtension

/// The VPN screen: two places to go (Home, Private), one Connect button, what's happening, and whether it
/// connects by itself. Opened from the Home VPN card and from Profile.
struct VPNView: View {
    @Environment(HomeStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    /// True when pushed inside Profile's navigation (no Done button of its own).
    var pushed = false
    @State private var busy = false
    @State private var stats: VPN.Stats?

    var body: some View {
        let vpn = VPN.shared
        let on = vpn.status == .connected
        let working = busy || [.connecting, .reasserting, .disconnecting].contains(vpn.status)
        List {
            Section {
                VStack(spacing: 10) {
                    ZStack {
                        Circle().fill(on ? Theme.accent.opacity(0.18) : Theme.card2).frame(width: 92, height: 92)
                        Image(systemName: on ? "checkmark.shield.fill" : "shield")
                            .font(.system(size: 40, weight: .medium))
                            .foregroundStyle(on ? Theme.accent : Theme.dim)
                            .symbolEffect(.pulse, isActive: working)
                    }
                    Text(headline(vpn)).font(.title2.weight(.bold))
                    Text(subline(vpn)).font(.subheadline).foregroundStyle(Theme.dim).multilineTextAlignment(.center)
                }
                .frame(maxWidth: .infinity).padding(.vertical, 10)
                .accessibilityElement(children: .combine)
            }
            .listRowBackground(Theme.card)

            Section {
                HStack(spacing: 10) {
                    ForEach(VPN.Exit.allCases) { e in
                        ExitCard(exit: e, detail: e == .home ? e.blurb : privateDetail(vpn),
                                 selected: vpn.exit == e, enabled: e == .home || vpn.privateAvailable) {
                            choose(e)
                        }
                    }
                }
                // Room for the selected card's outline, which the list would otherwise clip.
                .padding(2)
                .listRowInsets(EdgeInsets()).listRowBackground(Color.clear)
            }

            if !vpn.privateAvailable {
                Section {
                    Label {
                        VStack(alignment: .leading, spacing: 2) {
                            Text("Private isn't available").font(.subheadline.weight(.semibold))
                            Text(vpn.privateProblem ?? "Checking with the hub…").font(.subheadline).foregroundStyle(Theme.dim)
                        }
                    } icon: {
                        Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(Theme.warn)
                    }
                    .padding(.vertical, 4)
                }
                .listRowBackground(Theme.card)
            } else if !vpn.choices.isEmpty {
                Section {
                    LocationRow(flag: "📍", title: "Nearest", subtitle: "Picked automatically, fastest from where you are",
                                checked: vpn.exit == .private && vpn.regionChoice == nil) { pick(nil) }
                    ForEach(vpn.choices, id: \.id) { r in
                        LocationRow(flag: flag(r.country), title: r.name, subtitle: nil,
                                    checked: vpn.exit == .private && vpn.regionChoice == r.id) { pick(r.id) }
                    }
                } header: { Text("Private location") } footer: {
                    Text("Picking a place switches to Private. If it's connected, it moves there now.")
                }
                .listRowBackground(Theme.card)
            }

            Section {
                Button {
                    busy = true
                    Task {
                        defer { busy = false }
                        if on || working { await vpn.disconnectNow() }
                        else {
                            do { try await vpn.connectNow() }
                            catch { store.show(error.localizedDescription, isError: true) }
                        }
                    }
                } label: {
                    Text(on || working ? "Disconnect" : "Connect")
                        .font(.headline).frame(maxWidth: .infinity).padding(.vertical, 4)
                }
                .buttonStyle(.borderedProminent)
                .tint(on || working ? Theme.crit : Theme.accent)
                .disabled(busy)
                .listRowInsets(EdgeInsets()).listRowBackground(Color.clear)
            }

            if on {
                Section {
                    if let since = vpn.connectedSince {
                        LabeledContent("Connected for") { Text(since, style: .relative).monospacedDigit() }
                    }
                    LabeledContent(vpn.exit == .home ? "Through" : "Region",
                                   value: vpn.exit == .home ? "Firewalla" : (vpn.regionName ?? "Nearest"))
                    if let s = stats {
                        LabeledContent("Data", value: "\(byteSize(s.received)) down · \(byteSize(s.sent)) up")
                        if let hs = s.lastHandshake {
                            LabeledContent("Last check-in") { Text(hs, style: .relative) }
                        }
                    }
                }
            }

            Section {
                AutoRow(title: "Never", checked: vpn.mode == .off) { auto(nil) }
                AutoRow(title: "Home, whenever I leave home Wi-Fi", checked: vpn.mode != .off && vpn.exit == .home) { auto(.home) }
                if vpn.privateAvailable {
                    AutoRow(title: "Private, whenever I leave home Wi-Fi", checked: vpn.mode != .off && vpn.exit == .private) { auto(.private) }
                }
            } header: { Text("Connect by itself") } footer: {
                Text("Off by default. On home Wi-Fi it stays off. Disconnect also turns this back to Never.")
            }
        }
        .scrollContentBackground(.hidden).background(Backdrop())
        .navigationTitle("VPN")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { if !pushed { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } } }
        .task {
            await vpn.refreshStatus()
            await vpn.loadExits()
        }
        .task(id: on) {
            // Counters every 2 s while connected.
            while on, !Task.isCancelled {
                stats = await vpn.stats()
                try? await Task.sleep(for: .seconds(2))
            }
        }
    }

    private func choose(_ e: VPN.Exit) {
        guard e != VPN.shared.exit, !busy else { return }
        busy = true
        Task {
            defer { busy = false }
            do { try await VPN.shared.setExit(e) }
            catch { store.show(error.localizedDescription, isError: true) }
        }
    }

    private func pick(_ id: String?) {
        guard !busy else { return }
        busy = true
        Task {
            defer { busy = false }
            do { try await VPN.shared.setRegion(id) }
            catch { store.show(error.localizedDescription, isError: true) }
        }
    }

    private func privateDetail(_ vpn: VPN) -> String {
        let place = vpn.regionChoice.flatMap { id in vpn.choices.first { $0.id == id }?.name } ?? "Nearest location"
        return "PIA · \(place). For hotel and public Wi-Fi"
    }

    private func flag(_ country: String?) -> String {
        guard let c = country?.uppercased(), c.count == 2 else { return "🌐" }
        let code = c == "UK" ? "GB" : c
        return String(String.UnicodeScalarView(code.unicodeScalars.compactMap { UnicodeScalar(127397 + $0.value) }))
    }

    private func auto(_ e: VPN.Exit?) {
        busy = true
        Task {
            defer { busy = false }
            do {
                if let e {
                    if e != VPN.shared.exit { try await VPN.shared.setExit(e) }
                    try await VPN.shared.setMode(.away)
                    store.show("\(e.title) connects by itself away from home")
                } else {
                    try await VPN.shared.setMode(.off)
                    store.show("VPN won't connect by itself")
                }
            } catch { store.show(error.localizedDescription, isError: true) }
        }
    }

    private func headline(_ vpn: VPN) -> String {
        switch vpn.status {
        case .connected: "Connected"
        case .connecting, .reasserting: "Connecting"
        case .disconnecting: "Disconnecting"
        default: "Not connected"
        }
    }

    private func subline(_ vpn: VPN) -> String {
        if let p = vpn.problem { return p }
        switch vpn.status {
        case .connected:
            return vpn.exit == .home ? "Connected to home" : "PIA · \(vpn.regionName ?? "nearest region")"
        case .connecting, .reasserting:
            return vpn.exit == .home ? "Connecting to home…" : "Finding the nearest PIA region…"
        default:
            return vpn.exit == .home ? "Tap Connect to reach home from anywhere" : "Tap Connect for a private connection"
        }
    }
}

private struct ExitCard: View {
    let exit: VPN.Exit
    let detail: String
    let selected: Bool
    let enabled: Bool
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: 6) {
                Image(systemName: exit.symbol)
                    .font(.body.weight(.semibold))
                    .frame(width: 34, height: 34)
                    .background(selected ? Theme.accent : Theme.card2, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                    .foregroundStyle(selected ? Theme.onAccent : Theme.fg)
                Text(exit.title).font(.headline).foregroundStyle(Theme.fg)
                Text(detail).font(.caption).foregroundStyle(Theme.dim).multilineTextAlignment(.leading)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
            }
            .frame(maxWidth: .infinity, minHeight: 112, alignment: .topLeading)
            .padding(14)
            .background(Theme.card, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).strokeBorder(selected ? Theme.accent : .clear, lineWidth: 2))
        }
        .buttonStyle(.plain)
        .disabled(!enabled)
        .opacity(enabled ? 1 : 0.45)
        .accessibilityAddTraits(selected ? .isSelected : [])
        .accessibilityHint(detail)
    }
}

private struct AutoRow: View {
    let title: String
    let checked: Bool
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            HStack {
                Text(title).foregroundStyle(Theme.fg)
                Spacer()
                if checked { Image(systemName: "checkmark").foregroundStyle(Theme.accent).fontWeight(.semibold) }
            }
        }
        .accessibilityAddTraits(checked ? .isSelected : [])
    }
}

private struct LocationRow: View {
    let flag: String
    let title: String
    let subtitle: String?
    let checked: Bool
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            HStack(spacing: 12) {
                Text(flag).font(.title3).frame(width: 30)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).foregroundStyle(Theme.fg)
                    if let subtitle { Text(subtitle).font(.caption).foregroundStyle(Theme.dim) }
                }
                Spacer()
                if checked { Image(systemName: "checkmark").foregroundStyle(Theme.accent).fontWeight(.semibold) }
            }
        }
        .accessibilityAddTraits(checked ? .isSelected : [])
    }
}
