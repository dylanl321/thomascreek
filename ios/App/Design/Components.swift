import SwiftUI

// MARK: - Cards and sections

extension View {
    func card(padding: CGFloat = 16, color: Color = Theme.card) -> some View {
        self.padding(padding).frame(maxWidth: .infinity, alignment: .leading)
            .background(color, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
    }

    /// Standard screen background and padding for a scrolling stack of cards.
    func screen() -> some View {
        self.padding(.horizontal, 16).padding(.bottom, 28)
            .frame(maxWidth: 720)
            .frame(maxWidth: .infinity)
    }
}

struct Backdrop: View {
    var body: some View { Theme.bg.ignoresSafeArea() }
}

struct SectionHeader<Trailing: View>: View {
    let title: String
    var fresh: Date? = nil
    var live = false
    @ViewBuilder var trailing: () -> Trailing

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(title).font(.title3.weight(.semibold)).foregroundStyle(Theme.fg)
            if live || fresh != nil { Freshness(date: fresh, live: live) }
            Spacer()
            trailing()
        }
        .padding(.top, 14).padding(.horizontal, 2)
    }
}

extension SectionHeader where Trailing == EmptyView {
    init(_ title: String, fresh: Date? = nil, live: Bool = false) {
        self.title = title; self.fresh = fresh; self.live = live; self.trailing = { EmptyView() }
    }
}

/// "Live" with a green dot, or "12 min ago" when the data is old. Every card says how fresh it is.
struct Freshness: View {
    var date: Date?
    var live = false
    @Environment(HomeStore.self) private var store

    var body: some View {
        let stale = !store.isLive || (date.map { -$0.timeIntervalSinceNow > 120 } ?? false)
        HStack(spacing: 4) {
            Circle().fill(stale ? Theme.dim : Color.green).frame(width: 6, height: 6)
            if stale, let d = date ?? store.lastUpdate {
                Text(d, format: .relative(presentation: .named, unitsStyle: .abbreviated))
            } else if !store.reachable {
                Text("Offline")
            } else {
                Text("Live")
            }
        }
        .font(.caption2.weight(.medium)).foregroundStyle(Theme.dim)
    }
}

struct MoreButton: View {
    let title: String
    let action: () -> Void
    init(_ title: String = "See all", action: @escaping () -> Void) { self.title = title; self.action = action }
    var body: some View {
        Button(title, action: action).font(.subheadline.weight(.medium)).foregroundStyle(Theme.accent)
    }
}

// MARK: - Tiles

/// A lit tile is on, a dim tile is off, with the value in words.
struct StateTile: View {
    let symbol: String
    let title: String
    let detail: String
    let on: Bool
    var tint: Color? = nil
    var big = false
    let action: () -> Void
    @State private var taps = 0

    var body: some View {
        Button { taps += 1; action() } label: {
            VStack(alignment: .leading, spacing: big ? 14 : 10) {
                Image(systemName: symbol)
                    .font(big ? .system(size: 30, weight: .semibold) : .title3)
                    .foregroundStyle(on ? (tint ?? Theme.warn) : Theme.dim)
                    .symbolEffect(.bounce, value: taps)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).font(big ? Theme.kidFont(20, .semibold) : .subheadline.weight(.semibold)).lineLimit(1)
                    Text(detail).font(big ? .body : .caption).foregroundStyle(on ? Theme.litFg.opacity(0.7) : Theme.dim).lineLimit(1)
                }
            }
            .foregroundStyle(on ? Theme.litFg : Theme.fg)
            .padding(big ? 18 : 14)
            .frame(maxWidth: .infinity, minHeight: big ? 130 : 96, alignment: .leading)
            .background(on ? Theme.lit : Theme.off, in: RoundedRectangle(cornerRadius: big ? 26 : 18, style: .continuous))
        }
        .buttonStyle(.plain)
        .sensoryFeedback(.impact(weight: .light), trigger: taps)
        .accessibilityValue(on ? "On" : "Off")
    }
}

struct IconBadge: View {
    let symbol: String
    var color: Color = Theme.accent
    var size: CGFloat = 34
    var body: some View {
        Image(systemName: symbol).font(.system(size: size * 0.45, weight: .semibold))
            .foregroundStyle(color)
            .frame(width: size, height: size)
            .background(color.opacity(0.15), in: RoundedRectangle(cornerRadius: size * 0.3, style: .continuous))
    }
}

struct Avatar: View {
    let name: String
    var color: Color? = nil
    var size: CGFloat = 36
    var body: some View {
        Text(String(name.prefix(1)).uppercased())
            .font(.system(size: size * 0.42, weight: .bold, design: .rounded))
            .foregroundStyle(.white)
            .frame(width: size, height: size)
            .background(color ?? Self.color(for: name), in: Circle())
    }
    static func color(for name: String) -> Color {
        let palette: [Color] = [.teal, .indigo, .orange, .pink, .purple, .blue, .green]
        return palette[abs(name.hashValue) % palette.count]
    }
}

/// A row in a grouped list: icon, title, subtitle, trailing view, optional chevron.
struct InfoRow<Trailing: View>: View {
    let symbol: String?
    var color: Color = Theme.accent
    let title: String
    var subtitle: String? = nil
    var chevron = false
    @ViewBuilder var trailing: () -> Trailing

    var body: some View {
        HStack(spacing: 12) {
            if let symbol { IconBadge(symbol: symbol, color: color, size: 32) }
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.body.weight(.medium)).foregroundStyle(Theme.fg)
                if let subtitle { Text(subtitle).font(.footnote).foregroundStyle(Theme.dim) }
            }
            Spacer(minLength: 8)
            trailing()
            if chevron { Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(Theme.dim) }
        }
        .contentShape(Rectangle())
        .padding(.vertical, 6)
    }
}

extension InfoRow where Trailing == EmptyView {
    init(_ symbol: String?, color: Color = Theme.accent, _ title: String, _ subtitle: String? = nil, chevron: Bool = false) {
        self.symbol = symbol; self.color = color; self.title = title; self.subtitle = subtitle; self.chevron = chevron
        self.trailing = { EmptyView() }
    }
}

/// A card listing rows separated by hairlines.
struct RowsCard<Content: View>: View {
    @ViewBuilder var content: () -> Content
    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Group(subviews: content()) { subviews in
                ForEach(Array(subviews.enumerated()), id: \.offset) { i, v in
                    if i > 0 { Divider().overlay(Theme.line).padding(.leading, 44) }
                    v
                }
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 6)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
    }
}

struct ProgressRing: View {
    let value: Double
    var lineWidth: CGFloat = 10
    var color: Color = Theme.kid
    var body: some View {
        ZStack {
            Circle().stroke(color.opacity(0.18), lineWidth: lineWidth)
            Circle().trim(from: 0, to: max(0.001, min(1, value)))
                .stroke(color, style: StrokeStyle(lineWidth: lineWidth, lineCap: .round))
                .rotationEffect(.degrees(-90))
                .animation(.spring(duration: 0.6), value: value)
        }
    }
}

struct Pill: View {
    let text: String
    var color: Color = Theme.accent
    var body: some View {
        Text(text).font(.caption.weight(.semibold)).padding(.horizontal, 8).padding(.vertical, 3)
            .foregroundStyle(color).background(color.opacity(0.15), in: Capsule())
    }
}

struct ProgressBar: View {
    let value: Double
    var color: Color = Theme.accent
    var body: some View {
        GeometryReader { g in
            ZStack(alignment: .leading) {
                Capsule().fill(color.opacity(0.18))
                Capsule().fill(color).frame(width: g.size.width * max(0, min(1, value)))
            }
        }
        .frame(height: 6)
    }
}

struct PrimaryButton: View {
    let title: String
    var symbol: String? = nil
    var color: Color = Theme.accent
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            Group {
                if let symbol { Label(title, systemImage: symbol) } else { Text(title) }
            }
            .font(.headline).frame(maxWidth: .infinity, minHeight: 50)
        }
        .buttonStyle(.borderedProminent).tint(color)
        .buttonBorderShape(.roundedRectangle(radius: 16))
    }
}

/// Small rounded button used in rows of controls.
struct ChipButton: View {
    let title: String
    var symbol: String? = nil
    var active = false
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            HStack(spacing: 6) {
                if let symbol { Image(systemName: symbol) }
                if !title.isEmpty { Text(title).lineLimit(1) }
            }
            .font(.subheadline.weight(.semibold))
            .padding(.horizontal, 12).frame(minHeight: 36)
            .foregroundStyle(active ? Theme.onAccent : Theme.fg)
            .background(active ? Theme.accent : Theme.card2, in: Capsule())
        }
        .buttonStyle(.plain)
    }
}

// MARK: - Images from the hub (they need the bearer token, so AsyncImage won't do)

struct HubImage<Placeholder: View>: View {
    let path: String?
    var contentMode: ContentMode = .fill
    @ViewBuilder var placeholder: () -> Placeholder
    @State private var image: UIImage?

    var body: some View {
        Group {
            if let image { Image(uiImage: image).resizable().aspectRatio(contentMode: contentMode) }
            else { placeholder() }
        }
        .task(id: path) { await load() }
    }

    private func load() async {
        guard let path, !path.isEmpty else { return }
        if let cached = ImageCache.shared.object(forKey: path as NSString) { image = cached; return }
        // A saved copy shows at once (and offline); posters and photos rarely change.
        if let d = OfflineCache.loadImage(path: path), let img = UIImage(data: d) {
            ImageCache.shared.setObject(img, forKey: path as NSString); image = img
            return
        }
        do {
            var req: URLRequest
            if path.hasPrefix("http") { req = URLRequest(url: URL(string: path)!) }
            else { req = try await HubClient.shared.authorizedRequest(path) }
            req.timeoutInterval = 15
            let (data, resp) = try await HubClient.session.data(for: req)
            guard (resp as? HTTPURLResponse).map({ (200..<300).contains($0.statusCode) }) ?? true else { return }
            if let img = UIImage(data: data) {
                ImageCache.shared.setObject(img, forKey: path as NSString); image = img
                OfflineCache.storeImage(data, path: path)
            }
        } catch {}
    }
}

enum ImageCache { static let shared = NSCache<NSString, UIImage>() }

// MARK: - Empty and offline states

struct EmptyCard: View {
    let symbol: String
    let title: String
    var detail: String? = nil
    var body: some View {
        VStack(spacing: 8) {
            Image(systemName: symbol).font(.title).foregroundStyle(Theme.dim)
            Text(title).font(.headline)
            if let detail { Text(detail).font(.footnote).foregroundStyle(Theme.dim).multilineTextAlignment(.center) }
        }
        .frame(maxWidth: .infinity).padding(24)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
    }
}

struct LoadingCard: View {
    var body: some View {
        ProgressView().frame(maxWidth: .infinity, minHeight: 80)
            .background(Theme.card, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
    }
}

func greeting(_ name: String? = nil) -> String {
    let h = Calendar.current.component(.hour, from: .now)
    let g = h < 5 ? "Good evening" : h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening"
    return name.map { "\(g), \($0)" } ?? g
}

func clock(_ iso: String?) -> String {
    guard let d = iso.flatMap(ISO.parse) else { return iso ?? "" }
    return d.formatted(date: .omitted, time: .shortened)
}
