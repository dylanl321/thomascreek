import SwiftUI

// MARK: - The kids' night screen

/// From bedtime until wake-up a child's app turns into a calm night sky: nightlight, lights off,
/// and asking a grown-up. A parent can give "30 more minutes" from their phone.
struct KidBedtime: ViewModifier {
    @Environment(HomeStore.self) private var store
    @State private var family = FamilyStore.shared
    @State private var tick = Date.now
    private let timer = Timer.publish(every: 60, on: .main, in: .common).autoconnect()

    func body(content: Content) -> some View {
        content
            .fullScreenCover(isPresented: Binding(get: { store.role == .child && active }, set: { _ in })) {
                NightScreen()
            }
            .task { if store.role == .child { await family.load(force: true) } }
            .onReceive(timer) { now in
                tick = now
                if store.role == .child, Calendar.current.component(.minute, from: now) % 10 == 0 {
                    Task { await family.load(force: true) }
                }
            }
            .onReceive(NotificationCenter.default.publisher(for: UIApplication.didBecomeActiveNotification)) { _ in
                if store.role == .child { Task { await family.load(force: true) } }
            }
    }

    private var active: Bool { _ = tick; return family.bedtimeActive }
}

extension View {
    func kidBedtime() -> some View { modifier(KidBedtime()) }
}

struct NightScreen: View {
    @Environment(HomeStore.self) private var store
    @State private var asked = false
    @State private var glow = false

    private var little: Bool { store.me?.style == .little }
    private var name: String { store.me?.firstName ?? "" }
    private var lights: [DeviceItem] {
        guard let room = store.myRoom else { return [] }
        return (store.home?.devices(in: room) ?? []).filter { $0.kind == .light }
    }

    var body: some View {
        ZStack {
            SkyGradient(part: "night").ignoresSafeArea()
            Stars(count: 40).ignoresSafeArea()
            VStack(spacing: 22) {
                Spacer()
                Image(systemName: "moon.stars.fill").font(.system(size: 96)).foregroundStyle(.yellow.opacity(0.9))
                    .shadow(color: .yellow.opacity(glow ? 0.6 : 0.2), radius: glow ? 30 : 10)
                    .animation(.easeInOut(duration: 3).repeatForever(autoreverses: true), value: glow)
                Text("Goodnight, \(name)").font(Theme.kidFont(little ? 38 : 32)).foregroundStyle(.white)
                Text(wakeLine).font(.title3).foregroundStyle(.white.opacity(0.75))
                if little { SayButton(text: "Goodnight \(name). \(wakeLine)").foregroundStyle(.white) }
                Spacer()
                HStack(spacing: 14) {
                    nightButton("🌙", "Nightlight") { setLights(on: true, brightness: 8) }
                    nightButton("🌑", "Lights off") { setLights(on: false, brightness: nil) }
                }
                nightButton(asked ? "✅" : "🙋", asked ? "Asked!" : "Ask a grown-up") {
                    Task {
                        _ = try? await HubClient.shared.post("/v1/ask-parent", ["text": "Can you come to my room?", "emoji": "🌙"], as: Empty.self)
                        asked = true
                        if little { Speaker.say("I asked a grown-up for you.") }
                    }
                }
                .disabled(asked)
            }
            .padding(24)
        }
        .onAppear { glow = true; UIApplication.shared.isIdleTimerDisabled = false }
        .interactiveDismissDisabled()
    }

    private var wakeLine: String {
        "See you at \(FamilyStore.shared.today?.bedtime?.wakeLabel ?? "wake-up time")"
    }

    private func nightButton(_ emoji: String, _ title: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            VStack(spacing: 6) {
                Text(emoji).font(.system(size: 40))
                Text(title).font(Theme.kidFont(18, .semibold))
            }
            .frame(maxWidth: .infinity, minHeight: 110)
            .foregroundStyle(.white)
            .background(.white.opacity(0.12), in: RoundedRectangle(cornerRadius: 26, style: .continuous))
        }
        .buttonStyle(.plain)
    }

    private func setLights(on: Bool, brightness: Int?) {
        for l in lights {
            var p: [String: Any] = ["on": on]
            if let brightness { p["brightness"] = brightness }
            store.perform("light.set", target: l.id, params: p, optimistic: { $0.state["on"] = .bool(on) },
                          done: on ? "Nightlight on" : "Lights off")
        }
        if lights.isEmpty { store.show("Your room's lights aren't set up yet", isError: true) }
    }
}

// MARK: - Parents: bedtimes

struct BedtimesView: View {
    @Environment(HomeStore.self) private var store
    @State private var family = FamilyStore.shared
    @State private var editing: TodayInfo.KidBedtime?

    var body: some View {
        List {
            Section {
                ForEach(family.today?.kidsBedtimes ?? []) { b in
                    VStack(alignment: .leading, spacing: 10) {
                        HStack {
                            Avatar(name: b.name, size: 36)
                            VStack(alignment: .leading, spacing: 1) {
                                Text(b.name).font(.headline)
                                Text(b.enabled == false ? "Night screen off" : "\(b.label ?? "") until \(b.wakeLabel ?? "")")
                                    .font(.subheadline).foregroundStyle(Theme.dim)
                            }
                            Spacer()
                            if b.active == true { Pill(text: "Asleep", color: .indigo) }
                        }
                        HStack {
                            Button("Change") { editing = b }.buttonStyle(.bordered).controlSize(.small)
                            if b.active == true || (b.minutesLeft ?? 999) < 60 {
                                Button("30 more minutes") { Task { await extend(b) } }.buttonStyle(.borderedProminent).controlSize(.small).tint(.indigo)
                            }
                        }
                    }
                    .padding(.vertical, 4)
                }
            } footer: {
                Text("At bedtime the kids' app becomes a night sky with a nightlight button and \"Ask a grown-up\". Their internet pause is separate, in Kids online.")
            }
        }
        .navigationTitle("Bedtimes")
        .task { await family.load(force: true) }
        .sheet(item: $editing) { b in BedtimeEditor(kid: b) { await family.load(force: true) } }
    }

    private func extend(_ b: TodayInfo.KidBedtime) async {
        do {
            _ = try await HubClient.shared.post("/v1/people/\(b.id)/bedtime/extend", ["minutes": 30], as: Bedtime.self)
            store.show("\(b.name) can stay up 30 more minutes")
        } catch { store.show(error.localizedDescription, isError: true) }
        await family.load(force: true)
    }
}

private struct BedtimeEditor: View {
    @Environment(\.dismiss) private var dismiss
    @Environment(HomeStore.self) private var store
    let kid: TodayInfo.KidBedtime
    let saved: () async -> Void
    @State private var bed = Date.now
    @State private var wake = Date.now
    @State private var on = true

    var body: some View {
        NavigationStack {
            Form {
                Toggle("Night screen at bedtime", isOn: $on)
                DatePicker("Bedtime", selection: $bed, displayedComponents: .hourAndMinute).disabled(!on)
                DatePicker("Wake up", selection: $wake, displayedComponents: .hourAndMinute).disabled(!on)
            }
            .navigationTitle("\(kid.name)'s bedtime")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) { Button("Save") { Task { await save() } } }
            }
            .onAppear {
                bed = Self.date(kid.time ?? "20:00"); wake = Self.date(kid.wake ?? "07:00"); on = kid.enabled != false
            }
        }
        .presentationDetents([.medium])
    }

    static func date(_ hhmm: String) -> Date {
        let m = FamilyStore.minutes(hhmm)
        return Calendar.current.date(bySettingHour: m / 60, minute: m % 60, second: 0, of: .now) ?? .now
    }

    static func hhmm(_ d: Date) -> String {
        let c = Calendar.current.dateComponents([.hour, .minute], from: d)
        return String(format: "%02d:%02d", c.hour ?? 0, c.minute ?? 0)
    }

    private func save() async {
        do {
            let _: Bedtime = try await HubClient.shared.call("PUT", "/v1/people/\(kid.id)/bedtime",
                                                             body: ["time": Self.hhmm(bed), "wake": Self.hhmm(wake), "enabled": on])
            await saved()
            dismiss()
        } catch { store.show(error.localizedDescription, isError: true) }
    }
}
