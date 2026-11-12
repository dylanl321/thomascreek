import SwiftUI

/// Holiday lights state, polled every 3 s while a lights screen is open (as in the Christmas Lights app).
@MainActor
final class LightsModel: ObservableObject {
    static let shared = LightsModel()

    @Published var status: ShowStatus? = AppSettings.cachedStatus
    @Published var reachable = true
    @Published var error: String?
    @Published var busy = false

    private var poller: Task<Void, Never>?
    private var watchers = 0

    func startPolling() {
        watchers += 1
        guard poller == nil else { return }
        poller = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh()
                try? await Task.sleep(for: .seconds(3))
            }
        }
    }

    func stopPolling() {
        watchers = max(0, watchers - 1)
        if watchers == 0 { poller?.cancel(); poller = nil }
    }

    func refresh() async {
        do {
            let s = try await LightsAPI.status()
            if s != status { status = s }
            reachable = true; error = nil
        } catch {
            reachable = false; self.error = error.localizedDescription
        }
    }

    /// Run a show command with a short confirmation.
    func run(_ label: String? = nil, _ op: @escaping () async throws -> String) {
        Task {
            busy = true; defer { busy = false }
            do {
                let r = try await op()
                HomeStore.shared.show(label ?? (r.prefix(1).uppercased() + r.dropFirst()))
                await refresh()
            } catch HubClient.HubError.forbidden(let ask, let message) {
                if ask { HomeStore.shared.askParent = .init(what: message ?? "Can I change the light show?") }
                else { HomeStore.shared.show(message ?? "Not allowed", isError: true) }
            } catch {
                self.error = error.localizedDescription
                HomeStore.shared.show(error.localizedDescription, isError: true)
            }
        }
    }
}

/// The Holiday lights section. Dylan gets the full show; adults get looks, brightness and skip tonight;
/// children get the look picker during show hours.
struct HolidayView: View {
    @Environment(HomeStore.self) private var store
    @StateObject private var model = LightsModel.shared

    var body: some View {
        Group {
            if store.role == .child { KidLightsView() }
            else { ShowView(full: store.me?.can(.holidayFull) == true) }
        }
        .environmentObject(model)
        .onAppear { model.startPolling() }
        .onDisappear { model.stopPolling() }
    }
}

/// Home card: tonight's show with quick looks.
struct HolidayHomeCard: View {
    @Environment(Router.self) private var router
    @StateObject private var model = LightsModel.shared

    var body: some View {
        let s = model.status
        Button { router.show(.house, .holiday) } label: {
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    Label(s?.phase.title ?? "Holiday lights", systemImage: s?.phase.symbol ?? "sparkles")
                        .font(.footnote.weight(.semibold)).foregroundStyle(.white.opacity(0.75))
                    Spacer()
                    if let sc = s?.schedule {
                        Text(sc.skip ? "Skipped tonight" : sc.phase == "day" ? "Show \(TonightCard.twelve(sc.start))" : "Calm \(TonightCard.twelve(sc.calm))")
                            .font(.caption).foregroundStyle(.white.opacity(0.75))
                    }
                }
                Text(s?.headline ?? "Connecting…").font(.system(.title2, design: .rounded, weight: .bold)).foregroundStyle(.white).lineLimit(1)
                HStack(spacing: 8) {
                    ForEach([Look.elegant, .twinkle, .festive, .calm, .off]) { l in
                        Button { model.run(l == .off ? "Lights off" : "\(l.title) on") { try await LightsAPI.look(l) } } label: {
                            Image(systemName: l.symbol).frame(maxWidth: .infinity, minHeight: 36)
                                .foregroundStyle(s?.currentLook == l && s?.running != true ? .black : l.tint)
                                .background(s?.currentLook == l && s?.running != true ? l.tint : l.tint.opacity(0.18), in: RoundedRectangle(cornerRadius: 10))
                        }
                        .buttonStyle(.plain).accessibilityLabel(l.title)
                    }
                }
            }
            .padding(16)
            .background(LinearGradient(colors: [Color(red: 0.16, green: 0.12, blue: 0.35), Color(red: 0.05, green: 0.06, blue: 0.14)],
                                       startPoint: .topLeading, endPoint: .bottomTrailing),
                        in: RoundedRectangle(cornerRadius: 20, style: .continuous))
        }
        .buttonStyle(.plain)
        .task { await model.refresh() }
    }
}

/// For the kids: big look buttons during show hours.
struct KidLightsView: View {
    @EnvironmentObject var model: LightsModel
    @Environment(HomeStore.self) private var store

    var body: some View {
        let showTime = model.status?.phase != .day
        ScrollView {
            VStack(spacing: 16) {
                HeaderArt(image: "LightsHeader", title: "Light show", subtitle: model.status?.headline)
                if showTime {
                    LazyVGrid(columns: [GridItem(.flexible(), spacing: 12), GridItem(.flexible(), spacing: 12)], spacing: 12) {
                        ForEach([Look.elegant, .twinkle, .festive, .calm]) { l in
                            Button { model.run("\(l.title)!") { try await LightsAPI.look(l) } } label: {
                                VStack(spacing: 8) {
                                    Image(systemName: l.symbol).font(.system(size: 40))
                                    Text(l.title).font(Theme.kidFont(22))
                                }
                                .frame(maxWidth: .infinity, minHeight: 130)
                                .foregroundStyle(model.status?.currentLook == l ? .black : l.tint)
                                .background(model.status?.currentLook == l ? l.tint : l.tint.opacity(0.18), in: RoundedRectangle(cornerRadius: 26))
                            }
                            .buttonStyle(.plain)
                        }
                    }
                } else {
                    VStack(spacing: 10) {
                        Image(systemName: "sun.max.fill").font(.system(size: 44)).foregroundStyle(.yellow)
                        Text("The show starts at \(TonightCard.twelve(model.status?.schedule?.start ?? "5:00"))")
                            .font(Theme.kidFont(22)).multilineTextAlignment(.center)
                    }
                    .frame(maxWidth: .infinity).padding(28).lightsCard()
                }
            }
            .padding(.horizontal, 16).padding(.bottom, 24)
        }
        .background(LightsBackdrop())
        .navigationTitle("Light show").navigationBarTitleDisplayMode(.inline)
    }
}

/// Controller health and the WLED manager (Dylan only).
struct ControllersView: View {
    @StateObject private var model = LightsModel.shared
    var body: some View {
        List {
            Section {
                ForEach(model.status?.controllerList ?? [], id: \.ip) { row in
                    HStack {
                        Circle().fill(row.c.ok ? .green : .red).frame(width: 10, height: 10)
                        VStack(alignment: .leading) {
                            Text(row.c.name).font(.body.weight(.medium))
                            Text(row.ip).font(.caption).foregroundStyle(.secondary).monospaced()
                        }
                        Spacer()
                        if !row.c.ok { Text("Not responding").font(.caption.weight(.semibold)).foregroundStyle(.red) }
                        else if let ms = row.c.ms { Text("\(ms) ms").font(.caption).foregroundStyle(.secondary).monospacedDigit() }
                        if row.c.live == true { Image(systemName: "dot.radiowaves.left.and.right").foregroundStyle(.green) }
                    }
                }
            } header: { Text("Health") } footer: {
                Text("Checked every minute\(model.status?.health?.checked.map { ", last at \($0)" } ?? ""). Alerts go out when one drops off or comes back.")
            }
            Section {
                NavigationLink { LightsWebPage(path: "wled", title: "Manager") } label: { Label("Controller manager", systemImage: "slider.horizontal.3") }
            } footer: { Text("Backups, presets, firmware, replacing a controller.") }
            if let log = model.status?.log, !log.isEmpty {
                Section("Player log") {
                    ForEach(log.reversed(), id: \.self) { Text($0).font(.caption.monospaced()).foregroundStyle(.secondary) }
                }
            }
        }
        .scrollContentBackground(.hidden).background(LightsBackdrop())
        .navigationTitle("Controllers")
        .refreshable { await model.refresh() }
        .onAppear { model.startPolling() }
        .onDisappear { model.stopPolling() }
    }
}

/// A page the show player serves (/house, /wled), through the hub. The hub accepts the access token once
/// as ?access_token= and sets a cookie for the page's own requests.
struct LightsWebPage: View {
    let path: String
    let title: String
    @State private var url: URL?
    @State private var reload = 0

    var body: some View {
        Group {
            if let url { WebPlayer(url: url, reload: reload).ignoresSafeArea(edges: .bottom) }
            else { ProgressView() }
        }
        .background(.black)
        .navigationTitle(title).navigationBarTitleDisplayMode(.inline)
        .toolbar { Button("Reload", systemImage: "arrow.clockwise") { reload += 1 } }
        .task {
            guard let base = await HubClient.shared.baseURL(), let token = try? await HubClient.shared.accessToken() else { return }
            var c = URLComponents(url: base.appendingPathComponent("/v1/lights/\(path)"), resolvingAgainstBaseURL: false)!
            c.queryItems = [URLQueryItem(name: "access_token", value: token)]
            url = c.url
        }
    }
}

/// Holiday alert choices and the Lock Screen Live Activity, shown in Profile.
struct LightsSettingsSection: View {
    @StateObject private var live = LiveActivities.shared
    @State private var auto = AppSettings.autoLiveActivity
    var body: some View {
        Section {
            Toggle("Start automatically at sunset", isOn: $auto)
                .onChange(of: auto) { _, v in AppSettings.autoLiveActivity = v; Task { await live.sendStartToken() } }
            if live.active {
                Button("End Live Activity", role: .destructive) { Task { await live.endAll() } }
            } else {
                Button("Show it now") {
                    Task { do { try await live.start() } catch { HomeStore.shared.show(error.localizedDescription, isError: true) } }
                }
            }
        } header: { Text("Light show on the Lock Screen") } footer: {
            Text("Shows the scene, what's next and the countdown to Calm on the Lock Screen and in the Dynamic Island.")
        }
    }
}
