import ActivityKit
import SwiftUI

/// A doorbell or a person at the door, from the hub's `home_alert` event. The picture is a hub path.
struct DoorNotice: Identifiable, Equatable {
    var id: String
    var title: String
    var kind: String
    var camera: String?
    var snapshotUrl: String?
    var ttlS: Double
}

struct DoorAlertCard: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    var alert: DoorNotice

    var body: some View {
        Button {
            UIImpactFeedbackGenerator(style: .medium).impactOccurred()
            if let camera = alert.camera, store.me?.can(.cameras) == true {
                router.show(.house, .camera(camera))
            }
        } label: {
            HStack(spacing: 12) {
                HubImage(path: alert.snapshotUrl.map { CameraThumb.fresh($0) }) {
                    Image(systemName: alert.kind == "doorbell" ? "bell.and.waves.left.and.right.fill" : "person.fill")
                        .font(.title3).foregroundStyle(Theme.accent)
                        .frame(maxWidth: .infinity, maxHeight: .infinity).background(Theme.card2)
                }
                .frame(width: 84, height: 56)
                .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                VStack(alignment: .leading, spacing: 2) {
                    Text(alert.title).font(.headline).foregroundStyle(Theme.fg).lineLimit(2)
                    Text(alert.kind == "doorbell" ? "Doorbell" : "At the door").font(.caption).foregroundStyle(Theme.dim)
                }
                Spacer(minLength: 0)
                Image(systemName: "chevron.right").font(.caption.weight(.semibold)).foregroundStyle(Theme.dim)
            }
        }
        .buttonStyle(.plain)
        .card()
        .accessibilityLabel(alert.title)
    }
}

enum DoorLive {
    /// The Lock Screen card, when the app is open to hear the alert. Push still starts it when the app is closed.
    @MainActor
    static func show(_ alert: DoorNotice) {
        guard alert.kind == "doorbell" else { return }
        let attributes = DoorbellAttributes(camera: alert.camera ?? "", cameraName: alert.title, door: "front door")
        let state = DoorbellAttributes.ContentState(status: "ringing", atUnix: Date().timeIntervalSince1970)
        let content = ActivityContent(state: state, staleDate: Date().addingTimeInterval(alert.ttlS))
        if let existing = Activity<DoorbellAttributes>.activities.first {
            Task { await existing.update(content) }
            return
        }
        Task { _ = try? Activity.request(attributes: attributes, content: content) }
    }
}

/// Optional. The hub already picks the door cameras and the quiet hours.
struct DoorAlertSettings: View {
    @Environment(HomeStore.self) private var store
    @State private var found: [DoorCamera] = []
    @State private var chosen: Set<String> = []
    @State private var quietFrom = "22:00"
    @State private var quietTo = "07:00"
    @State private var motion = false
    @State private var loaded = false
    @State private var note: String?

    var body: some View {
        Form {
            Section {
                TextField("Quiet from", text: $quietFrom)
                TextField("Quiet until", text: $quietTo)
                Toggle("Motion at the door", isOn: $motion)
            } header: { Text("Quiet hours") } footer: {
                Text("A doorbell still shows. Someone at the door, and motion, wait until morning. 00:00 to 00:00 turns quiet hours off.")
            }
            Section {
                if found.isEmpty && loaded {
                    Text("No door camera yet. One will be used when it shows up.").foregroundStyle(Theme.dim)
                }
                ForEach(found) { cam in
                    Toggle(cam.name, isOn: Binding(get: { chosen.contains(cam.id) }, set: { on in
                        if on { chosen.insert(cam.id) } else { chosen.remove(cam.id) }
                    }))
                }
            } header: { Text("Cameras") } footer: {
                Text("These are the ones by a door. Uncheck a camera to leave it out.")
            }
            if let note { Section { Text(note).foregroundStyle(Theme.dim) } }
        }
        .navigationTitle("Door on the TV")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .confirmationAction) {
                Button("Save") { Task { await save() } }.disabled(!loaded)
            }
        }
        .task { await load() }
    }

    private func load() async {
        guard let page: DoorSettings = try? await HubClient.shared.get("/v1/tv/alerts/settings") else {
            note = "The TVs will keep using the door cameras we found."
            loaded = true
            return
        }
        found = page.found
        chosen = Set(page.cameras)
        quietFrom = page.quiet.from
        quietTo = page.quiet.to
        motion = page.motion
        loaded = true
    }

    private func save() async {
        do {
            let _: DoorSettings = try await HubClient.shared.call("PUT", "/v1/tv/alerts/settings", body: [
                "cameras": Array(chosen), "motion": motion, "quiet": ["from": quietFrom, "to": quietTo],
            ])
            UINotificationFeedbackGenerator().notificationOccurred(.success)
            note = "Saved."
        } catch {
            note = "The TVs will keep using the door cameras we found."
        }
    }
}

struct DoorCamera: Decodable, Identifiable { var id: String; var name: String; var door: Bool? }
struct DoorSettings: Decodable {
    var cameras: [String]
    var quiet: Quiet
    var motion: Bool
    var found: [DoorCamera]
    struct Quiet: Codable { var from: String; var to: String }
}
