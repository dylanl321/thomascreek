import SwiftUI
import Observation

/// Today, who's home and bedtime, shared by Home, the kids' home, the bedtime screen and widgets.
@MainActor
@Observable
final class FamilyStore {
    static let shared = FamilyStore()

    var today: TodayInfo? = HubClient.saved("/v1/today")
    var people: [FamilyMember] = []
    var loadedAt: Date?
    private var loading = false

    var family: [FamilyMember] { people.isEmpty ? (today?.family ?? []) : people }

    func load(force: Bool = false) async {
        guard !loading else { return }
        if !force, let t = loadedAt, -t.timeIntervalSinceNow < 60 { return }
        loading = true
        defer { loading = false }
        if let t: TodayInfo = try? await HubClient.shared.get("/v1/today") {
            today = t
            people = t.family ?? people
            loadedAt = .now
        }
    }

    func loadPresence() async {
        if let p: FamilyPresence = try? await HubClient.shared.get("/v1/family/presence") { people = p.people }
    }

    /// A child's bedtime from the last Today, with the clock checked locally so the screen dims on time
    /// even if the hub hasn't been asked for a while.
    var bedtimeActive: Bool {
        guard let b = today?.bedtime, b.enabled != false, let start = b.time, let wake = b.wake else { return false }
        if let ext = b.extendedUntil.flatMap(ISO.parse), ext > .now { return false }
        let m = Calendar.current.component(.hour, from: .now) * 60 + Calendar.current.component(.minute, from: .now)
        let s = Self.minutes(start), w = Self.minutes(wake)
        return m >= s || m < w
    }

    static func minutes(_ hhmm: String) -> Int {
        let p = hhmm.split(separator: ":").compactMap { Int($0) }
        return p.count == 2 ? p[0] * 60 + p[1] : 0
    }
}

/// The time of day as a sky: used behind Today, the welcome screens and bedtime.
struct SkyGradient: View {
    var part: String
    var body: some View {
        LinearGradient(colors: colors, startPoint: .top, endPoint: .bottom)
    }
    private var colors: [Color] {
        switch part {
        case "morning": [Color(red: 0.98, green: 0.78, blue: 0.55), Color(red: 0.55, green: 0.76, blue: 0.93)]
        case "afternoon": [Color(red: 0.36, green: 0.62, blue: 0.92), Color(red: 0.62, green: 0.84, blue: 0.96)]
        case "evening": [Color(red: 0.95, green: 0.55, blue: 0.38), Color(red: 0.36, green: 0.27, blue: 0.55)]
        default: [Color(red: 0.07, green: 0.09, blue: 0.22), Color(red: 0.18, green: 0.16, blue: 0.38)]
        }
    }

    static var nowPart: String {
        let h = Calendar.current.component(.hour, from: .now)
        return h < 12 ? "morning" : h < 17 ? "afternoon" : h < 21 ? "evening" : "night"
    }
}

/// Twinkling stars for night skies.
struct Stars: View {
    var count = 28
    @State private var on = false
    var body: some View {
        GeometryReader { g in
            ForEach(0..<count, id: \.self) { i in
                let x = CGFloat((i * 73) % 100) / 100, y = CGFloat((i * 37) % 60) / 100
                Circle().fill(.white).frame(width: i % 5 == 0 ? 3 : 2)
                    .position(x: x * g.size.width, y: y * g.size.height)
                    .opacity(on ? (i % 3 == 0 ? 0.3 : 0.9) : (i % 3 == 0 ? 0.9 : 0.35))
                    .animation(.easeInOut(duration: 1.6 + Double(i % 4) * 0.4).repeatForever(autoreverses: true), value: on)
            }
        }
        .allowsHitTesting(false)
        .onAppear { on = true }
    }
}
