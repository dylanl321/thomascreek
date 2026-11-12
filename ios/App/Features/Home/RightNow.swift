import SwiftUI

/// "Right now": the few cards the hub picked for this person at this moment (dinner and the oven,
/// lock up, weather, bedtime, a package at the door…). The hub decides; this only draws them.
struct RightNowSection: View {
    @Environment(HomeStore.self) private var store
    var hide: Set<String> = []   // rules another part of the screen already covers
    var kid = false
    @State private var showAll = false

    private var cards: [SmartCard] { (store.home?.smart ?? []).filter { !hide.contains($0.rule) } }

    var body: some View {
        let all = cards
        if !all.isEmpty {
            let shown = showAll ? all : Array(all.prefix(3))
            VStack(alignment: .leading, spacing: 10) {
                if !kid {
                    SectionHeader(title: "Right now") {
                        Text(partOfDay).font(.footnote).foregroundStyle(Theme.dim)
                    }
                }
                ForEach(shown) { c in
                    if c.style == "picture" { PictureCard(card: c) } else { SmartCardView(card: c, lead: c.id == shown.first?.id && c.priority >= 85) }
                }
                if all.count > 3 {
                    Button(showAll ? "Show less" : "Show \(all.count - 3) more") { withAnimation(.snappy) { showAll.toggle() } }
                        .font(.subheadline.weight(.semibold)).frame(maxWidth: .infinity)
                }
            }
            .animation(.snappy, value: shown.map(\.id))
        }
    }

    private var partOfDay: String {
        let h = Calendar.current.component(.hour, from: .now)
        switch h {
        case 4..<12: return "Morning"
        case 12..<17: return "Afternoon"
        case 17..<21: return "Evening"
        default: return "Night"
        }
    }
}

extension SmartCard {
    var color: Color {
        switch tint {
        case "bad": Theme.crit
        case "warn": Theme.warn
        case "kid": Theme.kid
        case "night": Theme.dyn(0x3D4FA8, 0x8C9BFF)
        case "sun": Theme.dyn(0xC97A00, 0xF5B544)
        default: Theme.accent
        }
    }
}

struct SmartCardView: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    let card: SmartCard
    var lead = false

    var body: some View {
        let color = card.color
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top, spacing: 12) {
                IconBadge(symbol: card.symbol, color: color, size: 40)
                VStack(alignment: .leading, spacing: 2) {
                    Text(card.eyebrow.uppercased()).font(.caption2.weight(.bold)).tracking(0.4).foregroundStyle(color)
                    Text(card.title).font(.headline).foregroundStyle(Theme.fg).fixedSize(horizontal: false, vertical: true)
                    if let d = card.detail { Text(d).font(.subheadline).foregroundStyle(Theme.dim).fixedSize(horizontal: false, vertical: true) }
                }
                Spacer(minLength: 24)
            }
            if let img = card.image {
                HubImage(path: img) { Theme.card2 }
                    .frame(height: 130).frame(maxWidth: .infinity).clipped()
                    .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                    .padding(.leading, 52)
                    .onTapGesture { if let r = card.ref, let u = URL(string: r) { router.open(u) } }
            }
            if let items = card.items, !items.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(items, id: \.self) { it in
                        Button {
                            if let u = it.url.flatMap(URL.init(string:)) { router.open(u) }
                        } label: {
                            HStack(spacing: 8) {
                                Circle().fill(color).frame(width: 6, height: 6)
                                Text(it.title).font(.subheadline).foregroundStyle(Theme.fg)
                                Spacer()
                                if let v = it.value, !v.isEmpty { Text(v).font(.footnote).foregroundStyle(Theme.dim) }
                            }
                        }
                        .buttonStyle(.plain).disabled(it.url == nil)
                    }
                }
                .padding(.leading, 52)
            }
            if !card.buttons.isEmpty {
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: 8) { buttonViews }
                    VStack(alignment: .leading, spacing: 8) { buttonViews }
                }
                .controlSize(.regular).font(.subheadline.weight(.semibold))
                .padding(.leading, 52)
            }
        }
        .card(padding: 14)
        .overlay {
            if lead { RoundedRectangle(cornerRadius: 20, style: .continuous).strokeBorder(color.opacity(0.6), lineWidth: 1.5) }
        }
        .overlay(alignment: .topTrailing) {
            Button { store.dismissSmart(card, handled: false) } label: {
                Image(systemName: "xmark").font(.caption.weight(.bold)).foregroundStyle(Theme.dim)
                    .frame(width: 30, height: 30).contentShape(Rectangle())
            }
            .accessibilityLabel("Not now")
            .padding(6)
        }
        .contextMenu {
            if let why = card.why, !why.isEmpty { Text("Because: \(why)") }
            Button("Not now", systemImage: "clock") { store.dismissSmart(card, handled: false) }
        }
        .transition(.asymmetric(insertion: .opacity.combined(with: .move(edge: .top)), removal: .opacity.combined(with: .scale(scale: 0.96))))
    }
}

extension SmartCardView {
    @ViewBuilder var buttonViews: some View {
        ForEach(card.buttons, id: \.self) { b in
            if b.primary == true {
                Button(b.title) { store.use(b, on: card, router: router) }
                    .buttonStyle(.borderedProminent).tint(card.color)
            } else {
                Button(b.title) { store.use(b, on: card, router: router) }
                    .buttonStyle(.bordered).tint(Theme.fg)
            }
        }
    }
}

/// Adeline's version: one big picture, a few words, a big colour.
struct PictureCard: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    let card: SmartCard

    var body: some View {
        Button {
            if let b = card.buttons.first { store.use(b, on: card, router: router) }
        } label: {
            HStack(spacing: 16) {
                Text(card.emoji ?? "⭐️").font(.system(size: 52))
                VStack(alignment: .leading, spacing: 2) {
                    Text(card.title).font(Theme.kidFont(24)).foregroundStyle(.white)
                    if let d = card.detail { Text(d).font(Theme.kidFont(15, .semibold)).foregroundStyle(.white.opacity(0.9)).lineLimit(2) }
                }
                Spacer(minLength: 0)
                if card.buttons.first != nil { Image(systemName: "chevron.right").font(.title3.weight(.bold)).foregroundStyle(.white.opacity(0.8)) }
            }
            .padding(18)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(card.color.gradient, in: RoundedRectangle(cornerRadius: 26, style: .continuous))
        }
        .buttonStyle(.plain)
        .accessibilityLabel(card.title)
    }
}
