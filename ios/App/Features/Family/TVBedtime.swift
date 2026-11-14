import SwiftUI

/// The kids' TV limit. The hub already fills in a bedtime, so this screen only changes it.
struct TVBedtimeRules: Decodable {
    var profile: String
    var dailyMinutes: Int?
    var from: String?
    var to: String?
    var warnMinutes: Int?
    var state: String?
    var minutesLeft: Int?
}

struct TVBedtimeCard: View {
    @Environment(HomeStore.self) private var store
    var notice: TVBedtimeNotice

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 10) {
                Image(systemName: notice.state == "locked" ? "moon.stars.fill" : "hourglass")
                    .font(.title3).foregroundStyle(Theme.accent)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).font(.headline)
                    Text(detail).font(.caption).foregroundStyle(Theme.dim)
                }
            }
            if store.role.isGrownUp, notice.state != "unlocked" {
                Button {
                    UIImpactFeedbackGenerator(style: .medium).impactOccurred()
                    Task { await store.extendBedtime(minutes: 15) }
                } label: {
                    Label("15 more minutes", systemImage: "plus")
                        .font(.subheadline.weight(.semibold)).frame(maxWidth: .infinity, minHeight: 40)
                }
                .buttonStyle(.plain)
                .foregroundStyle(Theme.onAccent)
                .background(Theme.accent, in: Capsule())
            }
        }
        .card()
    }

    private var title: String {
        if notice.state == "locked" { return "The kids' TV is done for now" }
        if let left = notice.minutesLeft { return "\(left) minutes left" }
        return "Almost bedtime"
    }

    private var detail: String {
        notice.state == "locked" ? "A parent can add a little more." : "Then it stops, and the lights can dim."
    }
}

struct TVBedtimeNotice: Equatable {
    var state: String
    var profile: String
    var minutesLeft: Int?
}

struct TVBedtimeSettings: View {
    @State private var daily = 90
    @State private var from = "20:30"
    @State private var wake = "07:00"
    @State private var warn = 10
    @State private var note: String?
    @State private var loaded = false

    var body: some View {
        Form {
            Section {
                Stepper("\(daily) minutes a day", value: $daily, in: 15...240, step: 15)
                TextField("Bedtime", text: $from)
                TextField("Until", text: $wake)
                Stepper("Warn \(warn) minutes before", value: $warn, in: 5...30, step: 5)
            } footer: {
                Text("This is already on for the kids' TV. Bedtime and the daily limit both count, and a warning comes first.")
            }
            if let note { Section { Text(note).foregroundStyle(Theme.dim) } }
        }
        .navigationTitle("Kids' TV")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Save") { Task { await save() } }.disabled(!loaded) } }
        .task { await load() }
    }

    private func load() async {
        guard let rules: TVBedtimeRules = try? await HubClient.shared.get("/v1/tv/bedtime", ["profile": "kids"]) else {
            note = "The kids' TV will keep the usual bedtime."
            loaded = true
            return
        }
        daily = rules.dailyMinutes ?? 90
        from = rules.from ?? "20:30"
        wake = rules.to ?? "07:00"
        warn = rules.warnMinutes ?? 10
        loaded = true
    }

    private func save() async {
        do {
            let _: TVBedtimeRules = try await HubClient.shared.call("PUT", "/v1/tv/bedtime", body: [
                "profile": "kids", "daily_minutes": daily, "from": from, "to": wake, "warn_minutes": warn,
            ])
            UINotificationFeedbackGenerator().notificationOccurred(.success)
            note = "Saved."
        } catch {
            note = "The kids' TV will keep the usual bedtime."
        }
    }
}
