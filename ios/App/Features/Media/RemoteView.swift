import SwiftUI

// MARK: - TV remote

/// One remote for every TV and box (Google / Android TV, Apple TV, Roku, LG, Samsung, Sony). The hub says what
/// this TV can do (`state.remote`: keys, apps, inputs, typing) and sends each press through the TV's own Home
/// Assistant integration, so a TV added once works the same on every phone. Grown-ups get the full remote;
/// kids get big buttons and only their apps.
struct RemoteView: View {
    @Environment(HomeStore.self) private var store
    let deviceID: String
    @State private var volume: Double?
    @State private var volumeTask: Task<Void, Never>?
    @State private var touchpad = false
    @State private var typing = false
    @State private var showMore = false
    @State private var hookup = false
    private var grownUp: Bool { store.role == .admin || store.role == .adult }

    private var device: DeviceItem? { store.home?.device(deviceID) }
    private var remote: [String: JSONValue] {
        if case .object(let o)? = device?["remote"] { return o }
        return [:]
    }
    private var keys: Set<String> { Set(remote["keys"]?.array?.compactMap(\.string) ?? []) }
    private var commands: Set<String> { Set(device?["commands"]?.array?.compactMap(\.string) ?? []) }
    private var kid: Bool { store.me?.style != .standard }
    private func has(_ k: String) -> Bool { keys.contains(k) || commands.contains(k) }

    var body: some View {
        let d = device
        ScrollView {
            VStack(spacing: 18) {
                header(d)
                topRow(d)
                if !kid, has("up") { Picker("", selection: $touchpad) {
                    Text("Buttons").tag(false); Text("Touchpad").tag(true)
                }.pickerStyle(.segmented).frame(maxWidth: 260) }
                if has("up") || has("select") {
                    if touchpad && !kid { TouchPad { send($0) }.frame(height: 260) } else { dpad }
                }
                navRow
                playbackRow(d)
                volumeAndChannels(d)
                apps
                if !kid { extras }
                if grownUp { pluggedInto(d) }
            }
            .padding(.top, 8).screen()
        }
        .background(Backdrop())
        .navigationTitle("Remote").navigationBarTitleDisplayMode(.inline)
        .sheet(isPresented: $hookup) { HookupSheet(boxID: deviceID, boxName: device?.name ?? "This box") }
        .sheet(isPresented: $typing) { TypeSheet { text in
            store.perform("media.command", target: deviceID, params: ["command": "text", "text": text], done: "Typed on the TV")
        } }
    }

    // MARK: pieces

    /// "Plugged into Samsung TV · HDMI 2": a box doesn't know which TV it's on, so a grown-up says it once and
    /// the hub then wakes that TV and switches it to the box's input whenever the box is used.
    private func pluggedInto(_ d: DeviceItem?) -> some View {
        let on = d?["plugged_into"]?.object
        let line = on.map { o in [o["name"]?.string, o["input"]?.string].compactMap { $0 }.joined(separator: " · ") }
        return Button { hookup = true } label: {
            HStack(spacing: 10) {
                Image(systemName: "cable.connector.horizontal").frame(width: 24)
                VStack(alignment: .leading, spacing: 2) {
                    Text(line.map { "Plugged into \($0)" } ?? "Is this a box plugged into a TV?").font(.subheadline.weight(.semibold))
                    Text(on == nil ? "Say which TV and input, and the TV turns on and switches by itself."
                                   : "The TV turns on and switches input when you use this box.")
                        .font(.caption).foregroundStyle(Theme.dim)
                }
                Spacer()
                Image(systemName: "chevron.right").font(.caption).foregroundStyle(Theme.dim)
            }
            .padding(12).background(Theme.card, in: RoundedRectangle(cornerRadius: 14)).contentShape(Rectangle())
        }
        .buttonStyle(.plain).padding(.horizontal)
    }

    private func header(_ d: DeviceItem?) -> some View {
        VStack(spacing: 4) {
            Text(d?.name ?? "TV").font(.title2.weight(.bold)).multilineTextAlignment(.center)
            Text(nowLine(d)).font(.subheadline).foregroundStyle(Theme.dim).lineLimit(1)
        }
    }

    private func nowLine(_ d: DeviceItem?) -> String {
        guard let d, d.isOn else { return "Off" }
        if let t = d["title"]?.string { return t }
        if let app = remote["app"]?.string ?? d["source"]?.string { return app == "Home" ? "Home screen" : "In \(app)" }
        return "On"
    }

    @ViewBuilder private func topRow(_ d: DeviceItem?) -> some View {
        let inputs = remote["inputs"]?.array?.compactMap(\.string) ?? []
        HStack(spacing: 12) {
            if has("power") { roundKey("power", d?.isOn == true ? "Turn off" : "Turn on", "power",
                                       tint: d?.isOn == true ? Theme.crit : Theme.accent) }
            Spacer()
            if !inputs.isEmpty {
                Menu {
                    ForEach(inputs, id: \.self) { src in
                        Button { store.perform("media.command", target: deviceID, params: ["command": "source", "source": src],
                                               done: "Switched to \(src)") } label: {
                            if d?["source"]?.string == src { Label(src, systemImage: "checkmark") } else { Text(src) }
                        }
                    }
                } label: { pill("rectangle.on.rectangle", "Input") }
            } else if has("input") {
                Button { send("input") } label: { pill("rectangle.on.rectangle", "Input") }.buttonStyle(.plain)
            }
            if !kid, remote["text"]?.bool == true {
                Button { typing = true } label: { pill("keyboard", "Type") }.buttonStyle(.plain)
            }
        }
    }

    private var dpad: some View {
        let size: CGFloat = kid ? 280 : 250
        return ZStack {
            Circle().fill(Theme.card).frame(width: size, height: size)
            VStack(spacing: 0) {
                arrow("chevron.up", "up")
                HStack(spacing: 0) {
                    arrow("chevron.left", "left")
                    Button { send("select") } label: {
                        Text("OK").font(kid ? .title2.weight(.bold) : .headline)
                            .frame(width: kid ? 104 : 90, height: kid ? 104 : 90)
                            .background(Theme.card2, in: Circle())
                    }
                    .buttonStyle(.plain).accessibilityLabel("OK")
                    arrow("chevron.right", "right")
                }
                arrow("chevron.down", "down")
            }
        }
    }

    @ViewBuilder private var navRow: some View {
        HStack(spacing: 22) {
            if has("back") { roundKey("arrow.uturn.backward", "Back", "back") }
            if has("home") { roundKey("house.fill", "Home", "home") }
            if !kid, has("menu") { roundKey("line.3.horizontal", "Menu", "menu") }
        }
    }

    @ViewBuilder private func playbackRow(_ d: DeviceItem?) -> some View {
        let playing = d?["state"]?.string == "playing"
        HStack(spacing: 18) {
            if has("rewind") { roundKey("backward.fill", "Rewind", "rewind") } else if has("prev") { roundKey("backward.end.fill", "Back", "prev") }
            if has("play_pause") { roundKey(playing ? "pause.fill" : "playpause.fill", "Play or pause", "play_pause") }
            else if has("play") || has("pause") { roundKey(playing ? "pause.fill" : "play.fill", playing ? "Pause" : "Play", playing ? "pause" : "play") }
            if has("fast_forward") { roundKey("forward.fill", "Fast forward", "fast_forward") } else if has("next") { roundKey("forward.end.fill", "Next", "next") }
        }
    }

    @ViewBuilder private func volumeAndChannels(_ d: DeviceItem?) -> some View {
        HStack(alignment: .center, spacing: 12) {
            if has("vol_up") || has("vol_down") || d?["volume"]?.double != nil {
                VStack(spacing: 8) {
                    if let level = d?["volume"]?.double, !kid {
                        Slider(value: Binding(get: { volume ?? level }, set: { setVolume($0) }), in: 0...100, step: 1).tint(Theme.accent)
                    }
                    HStack(spacing: 10) {
                        roundKey("speaker.minus.fill", "Volume down", "vol_down")
                        if has("mute") {
                            roundKey(d?["muted"]?.bool == true ? "speaker.slash.fill" : "speaker.fill", "Mute", "mute",
                                     tint: d?["muted"]?.bool == true ? Theme.crit : Theme.fg)
                        }
                        roundKey("speaker.plus.fill", "Volume up", "vol_up")
                    }
                }
                .padding(12).frame(maxWidth: .infinity)
                .background(Theme.card, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
            }
            if !kid, has("ch_up") {
                VStack(spacing: 6) {
                    roundKey("chevron.up", "Channel up", "ch_up")
                    Text("CH").font(.caption.weight(.bold)).foregroundStyle(Theme.dim)
                    roundKey("chevron.down", "Channel down", "ch_down")
                }
                .padding(10)
                .background(Theme.card, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
            }
        }
    }

    private struct RemoteApp: Identifiable { let id: String; let name: String; let kid: Bool }
    private struct ExtraKey: Identifiable { let id: String; let symbol: String; let title: String }

    private var appList: [RemoteApp] {
        (remote["apps"]?.array ?? []).compactMap { v -> RemoteApp? in
            guard case .object(let o) = v, let id = o["id"]?.string, let name = o["name"]?.string else { return nil }
            return RemoteApp(id: id, name: name, kid: o["kid"]?.bool ?? false)
        }.filter { !kid || $0.kid }
    }

    @ViewBuilder private var apps: some View {
        let list = appList
        if !list.isEmpty {
            SectionHeader(kid ? "Watch" : "Apps")
            LazyVGrid(columns: [GridItem(.adaptive(minimum: kid ? 140 : 104), spacing: 10)], spacing: 10) {
                ForEach(list) { app in
                    AppTile(id: app.id, name: app.name, big: kid) {
                        store.perform("media.command", target: deviceID, params: ["command": "app", "app": app.id],
                                      done: "Opening \(app.name)")
                    }
                }
            }
        }
    }

    @ViewBuilder private var extras: some View {
        let more = [
            ExtraKey(id: "guide", symbol: "list.bullet.rectangle", title: "Guide"),
            ExtraKey(id: "info", symbol: "info.circle", title: "Info"),
            ExtraKey(id: "captions", symbol: "captions.bubble", title: "Captions"),
            ExtraKey(id: "search", symbol: "magnifyingglass", title: "Search"),
            ExtraKey(id: "assistant", symbol: "mic.fill", title: "Assistant"),
            ExtraKey(id: "settings", symbol: "gearshape.fill", title: "Settings"),
            ExtraKey(id: "replay", symbol: "gobackward", title: "Replay"),
            ExtraKey(id: "exit", symbol: "xmark.circle", title: "Exit"),
        ].filter { has($0.id) }
        if !more.isEmpty {
            DisclosureGroup(isExpanded: $showMore) {
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 100), spacing: 10)], spacing: 10) {
                    ForEach(more) { k in ChipButton(title: k.title, symbol: k.symbol) { send(k.id) } }
                }
                .padding(.top, 8)
            } label: { Text("More buttons").font(.subheadline.weight(.semibold)) }
            .tint(Theme.fg)
            .padding(14).background(Theme.card, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
        }
    }

    // MARK: building blocks

    private func arrow(_ symbol: String, _ cmd: String) -> some View {
        Button { send(cmd) } label: {
            Image(systemName: symbol).font((kid ? Font.title : .title2).weight(.bold)).foregroundStyle(Theme.fg)
                .frame(width: kid ? 88 : 76, height: kid ? 88 : 76).contentShape(Rectangle())
        }
        .buttonStyle(.plain).accessibilityLabel(cmd.capitalized)
    }

    private func roundKey(_ symbol: String, _ label: String, _ cmd: String, tint: Color = Theme.fg) -> some View {
        Button { send(cmd) } label: {
            Image(systemName: symbol).font(.title3.weight(.semibold)).foregroundStyle(tint)
                .frame(width: kid ? 66 : 56, height: kid ? 66 : 56)
                .background(Theme.card, in: Circle())
        }
        .buttonStyle(.plain).accessibilityLabel(label)
    }

    private func pill(_ symbol: String, _ title: String) -> some View {
        Label(title, systemImage: symbol).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.fg)
            .padding(.horizontal, 14).frame(minHeight: 44)
            .background(Theme.card, in: Capsule())
    }

    private func send(_ cmd: String) {
        UIImpactFeedbackGenerator(style: .light).impactOccurred()
        store.perform("media.command", target: deviceID, params: ["command": cmd])
    }

    /// Slider moves are coalesced so Home Assistant gets one volume_set, not dozens.
    private func setVolume(_ v: Double) {
        volume = v
        volumeTask?.cancel()
        volumeTask = Task {
            try? await Task.sleep(for: .milliseconds(250))
            guard !Task.isCancelled else { return }
            store.perform("media.command", target: deviceID, params: ["command": "volume", "volume": Int(v)])
            try? await Task.sleep(for: .seconds(2))
            if !Task.isCancelled { volume = nil }
        }
    }
}

/// Swipe to move, tap for OK: like the touch surface on a real remote.
private struct TouchPad: View {
    let send: (String) -> Void
    var body: some View {
        RoundedRectangle(cornerRadius: 28, style: .continuous).fill(Theme.card)
            .overlay(Text("Swipe to move · tap for OK").font(.caption).foregroundStyle(Theme.dim))
            .contentShape(Rectangle())
            .gesture(DragGesture(minimumDistance: 24).onEnded { g in
                let dx = g.translation.width, dy = g.translation.height
                send(abs(dx) > abs(dy) ? (dx > 0 ? "right" : "left") : (dy > 0 ? "down" : "up"))
            })
            .simultaneousGesture(TapGesture().onEnded { send("select") })
            .accessibilityLabel("Touchpad")
    }
}

/// An app button that looks the same on every TV.
struct AppTile: View {
    let id: String
    let name: String
    var big = false
    let action: () -> Void
    private var color: Color {
        switch id {
        case "youtube": .red
        case "youtube_kids": Color(red: 1, green: 0.2, blue: 0.2)
        case "netflix": Color(red: 0.75, green: 0.05, blue: 0.1)
        case "plex": Color(red: 0.9, green: 0.63, blue: 0.05)
        case "disney_plus": Color(red: 0.07, green: 0.2, blue: 0.55)
        case "pbs_kids": Color(red: 0.15, green: 0.55, blue: 0.2)
        case "prime_video": Color(red: 0.0, green: 0.55, blue: 0.85)
        case "max": Color(red: 0.2, green: 0.1, blue: 0.75)
        case "hulu": Color(red: 0.1, green: 0.7, blue: 0.4)
        case "spotify": Color(red: 0.12, green: 0.73, blue: 0.33)
        default: Theme.card2
        }
    }
    var body: some View {
        Button(action: action) {
            Text(name).font((big ? Font.title3 : .subheadline).weight(.bold)).foregroundStyle(.white)
                .lineLimit(1).minimumScaleFactor(0.7)
                .frame(maxWidth: .infinity, minHeight: big ? 72 : 52)
                .background(color.gradient, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        }
        .buttonStyle(.plain)
    }
}

/// Type into the TV's search box (Google / Android TV).
private struct TypeSheet: View {
    @Environment(\.dismiss) private var dismiss
    let send: (String) -> Void
    @State private var text = ""
    @FocusState private var focused: Bool
    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 12) {
                TextField("Search the TV", text: $text)
                    .textFieldStyle(.roundedBorder).autocorrectionDisabled().textInputAutocapitalization(.never)
                    .focused($focused).onSubmit(go)
                Text("Open a search box on the TV first. Letters, numbers and spaces are sent.")
                    .font(.caption).foregroundStyle(Theme.dim)
                Spacer()
            }
            .padding()
            .navigationTitle("Type on the TV").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) { Button("Send", action: go).disabled(text.isEmpty) }
            }
            .onAppear { focused = true }
        }
        .presentationDetents([.height(220)])
    }
    private func go() { guard !text.isEmpty else { return }; send(text); dismiss() }
}

// MARK: - Watch on a TV

/// Pick an app, then the TV: "YouTube on the basement TV" works the same on every kind of TV.
struct WatchAnywhereSheet: View {
    @Environment(HomeStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    @State private var apps: [WatchApp] = []
    @State private var loading = true
    @State private var picked: WatchApp?

    var body: some View {
        NavigationStack {
            ScrollView {
                if let app = picked {
                    VStack(spacing: 0) {
                        ForEach(app.screens) { s in
                            Button {
                                store.perform("media.command", target: s.entityId, params: ["command": "app", "app": app.id],
                                              done: "Opening \(app.name) on \(s.name)")
                                dismiss()
                            } label: {
                                HStack {
                                    Image(systemName: "tv").frame(width: 28)
                                    VStack(alignment: .leading) {
                                        Text(s.name).font(.body.weight(.semibold))
                                        Text([s.room, s.on ? "On" : "Off, it turns on"].compactMap { $0 }.joined(separator: " · "))
                                            .font(.caption).foregroundStyle(Theme.dim)
                                    }
                                    Spacer()
                                }
                                .padding(.vertical, 12).contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                            Divider().overlay(Theme.line)
                        }
                    }
                    .padding(.horizontal)
                } else if loading {
                    LoadingCard().padding()
                } else if apps.isEmpty {
                    EmptyCard(symbol: "tv", title: "No TVs with apps yet", detail: "Add a TV from Media → Add a TV or box.").padding()
                } else {
                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 140), spacing: 10)], spacing: 10) {
                        ForEach(apps) { a in AppTile(id: a.id, name: a.name, big: true) { picked = a } }
                    }
                    .padding()
                }
            }
            .navigationTitle(picked.map { "\($0.name) on…" } ?? "Watch on a TV").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                if picked != nil { ToolbarItem(placement: .navigation) { Button("Apps") { picked = nil } } }
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
            }
            .task {
                if let r: WatchApps = try? await HubClient.shared.get("/v1/media/watch") { apps = r.apps }
                loading = false
            }
        }
        .presentationDetents([.medium, .large])
    }
}

struct WatchApps: Decodable { var apps: [WatchApp] }
struct WatchApp: Decodable, Identifiable, Hashable {
    var id: String
    var name: String
    var kid: Bool?
    var screens: [Screen]
    struct Screen: Decodable, Identifiable, Hashable {
        var entityId: String
        var name: String
        var room: String?
        var on: Bool
        var id: String { entityId }
    }
}


// MARK: - Which TV a box is plugged into

struct HookupScreens: Decodable { var screens: [HookupScreen] }
struct HookupScreen: Decodable, Identifiable, Hashable {
    var entityId: String
    var name: String
    var inputs: [String]
    var pluggedInto: PluggedInto?
    var id: String { entityId }
    struct PluggedInto: Decodable, Hashable { var entityId: String; var name: String; var input: String? }
}

struct HookupSheet: View {
    @Environment(HomeStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let boxID: String
    let boxName: String
    @State private var screens: [HookupScreen] = []
    @State private var tv: String?
    @State private var input: String?
    @State private var saving = false
    @State private var loaded = false

    private var tvs: [HookupScreen] { screens.filter { $0.entityId != boxID && $0.pluggedInto == nil } }
    private var inputs: [String] { tvs.first { $0.entityId == tv }?.inputs ?? [] }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Picker("TV", selection: $tv) {
                        Text("Not plugged into another TV").tag(String?.none)
                        ForEach(tvs) { Text($0.name).tag(String?.some($0.entityId)) }
                    }
                    if tv != nil, !inputs.isEmpty {
                        Picker("Input", selection: $input) {
                            Text("Don't switch input").tag(String?.none)
                            ForEach(inputs, id: \.self) { Text($0).tag(String?.some($0)) }
                        }
                    }
                } footer: {
                    Text("When \(boxName) is used, the hub turns this TV on and switches it to the input. Volume goes to "
                         + "the TV, and power turns both on or off.")
                }
            }
            .disabled(!loaded)
            .navigationTitle("Plugged into").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) { Button("Save") { save() }.disabled(saving || !loaded) }
            }
            .onChange(of: tv) { _, new in if let i = input, !(tvs.first { $0.entityId == new }?.inputs.contains(i) ?? false) { input = nil } }
            .task {
                if let r: HookupScreens = try? await HubClient.shared.get("/v1/media/hookups") {
                    screens = r.screens
                    let mine = r.screens.first { $0.entityId == boxID }?.pluggedInto
                    tv = mine?.entityId; input = mine?.input
                }
                loaded = true
            }
        }
        .presentationDetents([.medium])
    }

    private func save() {
        saving = true
        Task {
            let body: [String: Any] = ["display_entity_id": tv.map { $0 as Any } ?? NSNull(),
                                       "input": (tv == nil ? nil : input).map { $0 as Any } ?? NSNull()]
            do {
                let data = try JSONSerialization.data(withJSONObject: body)
                _ = try await HubClient.shared.raw("PUT", "/v1/media/hookups/\(boxID)", body: data)
                store.toast = .init(title: tv == nil ? "Unplugged from the TV" : "Saved")
                await store.refreshAll()
                dismiss()
            } catch {
                store.toast = .init(title: "Couldn't save that", detail: error.localizedDescription, isError: true)
            }
            saving = false
        }
    }
}
