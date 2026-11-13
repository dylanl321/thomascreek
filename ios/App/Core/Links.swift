import SwiftUI

// The web of links (Dylan, Nov 13): anything the app mentions, such as a temperature, a device, a room,
// a person or a printer, opens that thing when tapped. Every reference goes through `.links(to:)` so they
// all behave the same way. The screen opens on top of the current one (Back returns to where the person
// was), the person's role decides what may open, and VoiceOver reads it as a link.

extension Destination {
    /// The screen for a device: its own page, or the richer screen that kind already has.
    static func forDevice(_ d: DeviceItem) -> Destination {
        switch d.kind {
        case .oven: .kitchen(d.id)
        case .media: .remote(d.id)
        case .camera: .camera(d.id)
        default: .device(d.id)
        }
    }

    /// Something the hub logged or mentioned by id ("lock.front_door", a camera name, "nas:plex"…).
    @MainActor
    static func forTarget(_ target: String?) -> Destination? {
        guard let t = target, !t.isEmpty else { return nil }
        let home = HomeStore.shared.home
        if let d = home?.device(t) { return forDevice(d) }
        if home?.cameras?.contains(where: { $0.id == t }) == true { return .camera(t) }
        if home?.room(t) != nil { return .room(t) }
        if t.hasPrefix("weather") { return .weather }
        // Things Home Assistant logs that aren't on a screen of their own.
        let domain = t.split(separator: ".").first.map(String.init) ?? ""
        switch domain {
        case "climate": return .climate
        case "lock", "cover", "binary_sensor", "alarm_control_panel": return .doors
        case "camera":
            let id = String(t.dropFirst(7))
            return home?.cameras?.contains(where: { $0.id == id }) == true ? .camera(id) : .cameras
        case "person":
            return forPerson(named: String(t.dropFirst(7)).replacingOccurrences(of: "_", with: " "))
        case "valve": return .outdoor
        default: return nil
        }
    }

    /// A person by first name, as calendar events and chore lists name them.
    @MainActor
    static func forPerson(named name: String?) -> Destination? {
        guard let n = name?.trimmingCharacters(in: .whitespaces).lowercased(), !n.isEmpty else { return nil }
        let people = FamilyStore.shared.family
        if let p = people.first(where: { $0.first.lowercased() == n || $0.name.lowercased() == n }) { return .person(p.id) }
        if let p = HomeStore.shared.home?.people?.first(where: { $0.name.lowercased() == n || $0.name.lowercased().hasPrefix(n + " ") }) {
            return .person(p.id)
        }
        return nil
    }
}

/// A reference: a screen in the app, or a thomascreek:// link from the hub.
enum LinkTarget: Hashable {
    case screen(Destination)
    case url(URL)

    init?(_ d: Destination?) { guard let d else { return nil }; self = .screen(d) }
    init?(ref: String?) { guard let r = ref, let u = URL(string: r) else { return nil }; self = .url(u) }
}

extension Router {
    func follow(_ t: LinkTarget) {
        switch t {
        case .screen(let d): follow(d)
        case .url(let u): follow(u)
        }
    }

    func allows(_ t: LinkTarget) -> Bool {
        switch t {
        case .screen(let d): allows(d)
        case .url: true   // the URL rules in `open` apply the role
        }
    }
}

private struct LinkModifier: ViewModifier {
    @Environment(Router.self) private var router
    let target: LinkTarget?
    let name: String?
    /// Containers with their own buttons inside (cards) take a tap gesture rather than becoming a button,
    /// so the inner buttons keep working.
    let container: Bool
    @State private var pressed = false

    func body(content: Content) -> some View {
        if let t = target, router.allows(t) {
            if container {
                content
                    .contentShape(Rectangle())
                    .onTapGesture { router.follow(t) }
                    .accessibilityAddTraits(.isLink)
                    .accessibilityAction(named: name.map { "Open \($0)" } ?? "Open") { router.follow(t) }
            } else {
                Button { router.follow(t) } label: { content.contentShape(Rectangle()) }
                    .buttonStyle(LinkPressStyle())
                    .accessibilityAddTraits(.isLink)
                    .accessibilityHint(name.map { "Opens \($0)" } ?? "")
            }
        } else {
            content
        }
    }
}

/// A light press: the reference dims while held, without changing its colours.
struct LinkPressStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .opacity(configuration.isPressed ? 0.55 : 1)
            .animation(.easeOut(duration: 0.12), value: configuration.isPressed)
    }
}

extension View {
    /// Tapping this opens `destination` on top of the current screen, if this person may open it.
    /// `name` is what VoiceOver says it opens ("Weather", "Kitchen lights").
    func links(to destination: Destination?, _ name: String? = nil, container: Bool = false) -> some View {
        modifier(LinkModifier(target: LinkTarget(destination), name: name, container: container))
    }

    /// Same, for a thomascreek:// reference the hub sent (cards, status lines).
    func links(ref: String?, _ name: String? = nil, container: Bool = false) -> some View {
        modifier(LinkModifier(target: LinkTarget(ref: ref), name: name, container: container))
    }

    /// Any temperature or weather mention: opens Weather (for a trip, that trip's forecast).
    func linksToWeather(place: String? = nil) -> some View {
        links(to: place.map { Destination.weatherPlace($0) } ?? .weather, "Weather")
    }
}

/// Markdown links in text the app shows (Ask Thomas Creek's answers, notes) go through the router:
/// thomascreek:// opens in place, anything else goes to Safari.
struct InAppLinks: ViewModifier {
    @Environment(Router.self) private var router
    var before: (() -> Void)? = nil
    func body(content: Content) -> some View {
        content.environment(\.openURL, OpenURLAction { url in
            guard url.scheme == "thomascreek" else { return .systemAction }
            before?()
            Task { @MainActor in
                if before != nil { try? await Task.sleep(for: .milliseconds(400)) }
                router.follow(url)
            }
            return .handled
        })
    }
}

extension View {
    /// Route thomascreek:// links in this view's text through the app. `before` runs first (close a sheet).
    func inAppLinks(before: (() -> Void)? = nil) -> some View { modifier(InAppLinks(before: before)) }
}
