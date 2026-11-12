import SwiftUI
import UserNotifications

/// First time each person opens the app: a few friendly pages for their role, then straight in.
/// Shown once per person and role (synced, so a new iPad doesn't ask again).
struct FirstRunWelcome: ViewModifier {
    @Environment(HomeStore.self) private var store
    @State private var show = false

    private var key: String { "app.family.welcome_seen" }
    private var token: String { "\(store.me?.person.id ?? "")/\(store.role.rawValue)" }

    func body(content: Content) -> some View {
        content
            .onChange(of: token, initial: true) { _, t in
                guard store.me != nil, store.role != .pending, store.role != .tablet else { return }
                let seen = SettingsStore.shared.optionalString(key) ?? UserDefaults.standard.string(forKey: key)
                if seen != t { show = true }
            }
            .fullScreenCover(isPresented: $show) {
                WelcomePages(role: store.role, little: store.me?.style == .little, name: store.me?.firstName ?? "") {
                    SettingsStore.shared.set(key, token)
                    UserDefaults.standard.set(token, forKey: key)
                    show = false
                }
            }
    }
}

extension View {
    func familyWelcome() -> some View { modifier(FirstRunWelcome()) }
    func tellDylan() -> some View { modifier(TellDylanModifier()) }
}

private struct Page: Identifiable {
    let id = UUID()
    var emoji: String
    var title: String
    var text: String
    var action: (title: String, run: @MainActor () async -> Void)? = nil
}

struct WelcomePages: View {
    let role: Role
    let little: Bool
    let name: String
    let finish: () -> Void
    @State private var index = 0
    @State private var did: Set<Int> = []

    var body: some View {
        let ps = pages
        ZStack {
            SkyGradient(part: role == .child ? "afternoon" : SkyGradient.nowPart).ignoresSafeArea()
            if SkyGradient.nowPart == "night" && role != .child { Stars().ignoresSafeArea() }
            VStack(spacing: 0) {
                TabView(selection: $index) {
                    ForEach(Array(ps.enumerated()), id: \.offset) { i, p in
                        VStack(spacing: 18) {
                            Spacer()
                            Text(p.emoji).font(.system(size: little ? 110 : 88))
                                .scaleEffect(index == i ? 1 : 0.6).animation(.spring(duration: 0.5, bounce: 0.4), value: index)
                            Text(p.title).font(.system(size: little ? 36 : 30, weight: .bold, design: .rounded))
                                .multilineTextAlignment(.center)
                            Text(p.text).font(little ? .title3 : .body).multilineTextAlignment(.center).opacity(0.9)
                                .padding(.horizontal, 28)
                            if let a = p.action {
                                Button {
                                    Task { await a.run(); did.insert(i) }
                                } label: {
                                    Label(did.contains(i) ? "Done" : a.title, systemImage: did.contains(i) ? "checkmark" : "hand.tap.fill")
                                        .font(.headline).padding(.horizontal, 20).padding(.vertical, 12)
                                        .background(.white.opacity(0.25), in: Capsule())
                                }
                                .disabled(did.contains(i))
                            }
                            if little || role == .child { SayButton(text: "\(p.title). \(p.text)") }
                            Spacer(); Spacer()
                        }
                        .foregroundStyle(.white)
                        .tag(i)
                    }
                }
                .tabViewStyle(.page(indexDisplayMode: .always))
                Button {
                    if index < ps.count - 1 { withAnimation { index += 1 } } else { finish() }
                } label: {
                    Text(index < ps.count - 1 ? "Next" : (role == .child ? "Let's go!" : "Start"))
                        .font(.headline).frame(maxWidth: .infinity, minHeight: 54)
                        .background(.white, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
                        .foregroundStyle(Color(red: 0.12, green: 0.2, blue: 0.3))
                }
                .padding(.horizontal, 24).padding(.bottom, 12)
                Button("Skip") { finish() }.font(.subheadline).foregroundStyle(.white.opacity(0.8)).padding(.bottom, 20)
            }
        }
    }

    private var pages: [Page] {
        switch role {
        case .child where little:
            return [
                Page(emoji: "👋", title: "Hi \(name)!", text: "This is your house app."),
                Page(emoji: "💡", title: "Your lights", text: "Tap the light picture to turn your lights on and off."),
                Page(emoji: "📺", title: "Your shows", text: "Tap Shows to watch on your TV."),
                Page(emoji: "⭐", title: "Jobs and stars", text: "Do your jobs to get stars."),
                Page(emoji: "🙋", title: "Need a grown-up?", text: "Tap Ask a grown-up and pick a picture."),
            ]
        case .child:
            return [
                Page(emoji: "👋", title: "Hey \(name)!", text: "Thomas Creek runs your room, your TV and your chores."),
                Page(emoji: "🛏️", title: "Your room", text: "Lights, fan and temperature in My Room."),
                Page(emoji: "⭐", title: "Chores and rewards", text: "Tick off chores for points, then spend them in the reward shop."),
                Page(emoji: "🎬", title: "Watch", text: "Shows, films and your YouTube channels. Ask for new ones and a parent says yes or no."),
                Page(emoji: "🙋", title: "Ask a parent", text: "Anything you can't do yet, the app asks a parent for you."),
            ]
        case .guest:
            return [
                Page(emoji: "🏡", title: "Welcome, \(name)", text: "Wi-Fi, your space's lights and house tips are all here."),
                Page(emoji: "💡", title: "Your space", text: "Lights and temperature for where you're staying."),
            ]
        default:
            return [
                Page(emoji: "🏡", title: "Welcome home, \(name)", text: "The whole house in one place: doors, lights, cameras, dinner, the calendar and the kids."),
                Page(emoji: "☀️", title: "Today, at a glance", text: "Home shows what matters right now, plus who's home and what's for dinner."),
                Page(emoji: "🔔", title: "Only the alerts you want", text: "Doorbell, garage left open, a leak. Everything else stays quiet.",
                     action: ("Allow notifications", {
                         _ = try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge])
                     })),
                Page(emoji: "🚗", title: "Arriving and leaving", text: "Leave last and the app checks the doors and garage for you. Come home to the lights on.",
                     action: ("Turn on", { PresenceMonitor.shared.enable() })),
                Page(emoji: "💬", title: "Just ask", text: "Tap the Ask button or say \"Hey Siri, ask Thomas Creek\" to do anything by voice. Shake your phone to tell Dylan something."),
            ]
        }
    }
}
