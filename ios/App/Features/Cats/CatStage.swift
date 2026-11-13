import SwiftUI
import UIKit

// Where Kiki and Bri live: a clear window over the app. Touches pass straight through it everywhere
// except on a cat, so no button is ever blocked. The window only redraws while a scene is playing.

/// One thing on the stage. `origin` is its top-left corner in screen points.
@MainActor
@Observable
final class CatActor: Identifiable {
    let id = UUID()
    var art: CatArt
    var origin: CGPoint
    /// 1 faces right, -1 faces left.
    var facing: CGFloat = 1
    var opacity: Double = 1
    var tappable = true
    fileprivate var path: Glide?

    init(_ art: CatArt, at origin: CGPoint, facing: CGFloat = 1) {
        self.art = art; self.origin = origin; self.facing = facing
    }

    var size: CGSize { art.size }

    /// Where it is at clock time `now` (mid-glide or resting).
    func position(at now: Double) -> CGPoint { path?.point(at: now) ?? origin }
}

/// A move from one spot to another, optionally hopping in an arc.
struct Glide {
    enum Ease { case linear, easeIn, easeOut, bounce }
    var from: CGPoint, to: CGPoint, start: Double, duration: Double, arc: CGFloat, ease: Ease

    func point(at now: Double) -> CGPoint {
        let u = max(0, min(1, (now - start) / duration))
        let e: Double = switch ease {
        case .linear: u
        case .easeIn: u * u
        case .easeOut: 1 - (1 - u) * (1 - u)
        case .bounce: 1 + 2.2 * pow(u - 1, 3) + 1.2 * pow(u - 1, 2)  // overshoots a little, then settles
        }
        let ce = CGFloat(e), cu = CGFloat(u)
        let x = from.x + (to.x - from.x) * ce
        guard arc != 0 else { return CGPoint(x: x, y: from.y + (to.y - from.y) * ce) }
        // A quadratic hop whose top is `arc` above the higher end.
        let peak = min(from.y, to.y) - arc
        let cy = 2 * peak - (from.y + to.y) / 2
        let v = 1 - cu
        return CGPoint(x: x, y: v * v * from.y + 2 * v * cu * cy + cu * cu * to.y)
    }
}

/// Floating extras: Zzz, hearts, "Mrrp?".
struct CatEffect: Identifiable {
    enum Kind { case zzz, heart, bubble(String) }
    let id = UUID()
    let kind: Kind
    let at: CGPoint
    let start: Double
    var duration: Double {
        switch kind { case .bubble: 1.3; default: 2.4 }
    }
}

@MainActor
@Observable
final class CatStage {
    static let shared = CatStage()

    var actors: [CatActor] = []
    var effects: [CatEffect] = []
    var sunbeam = false
    var playing = false

    /// The Dynamic Island or notch, in screen points. On iPad and older iPhones it sits just above the
    /// screen, so a cat "behind" it is simply above the top edge.
    var cover = CGRect(x: 133, y: 11, width: 126, height: 37)
    var hasCover = true
    var screen = CGSize(width: 393, height: 852)
    var safeTop: CGFloat = 59
    /// The bottom of the Home header art, live, in screen points (it moves as Home scrolls).
    var registeredLedge: CGFloat?
    /// The ledge a scene started with. A scene keeps it to the end, so a cat never jumps to a new
    /// ground halfway through because the page scrolled.
    @ObservationIgnored private var sceneLedge: CGFloat?
    /// Set when that ledge was the header art: cats standing on it ride along with the header.
    @ObservationIgnored private var anchorLedge: CGFloat?

    /// Where cats walk: the bottom of the Home header art when it's on screen, else under the title.
    var ledge: CGFloat { sceneLedge ?? liveLedge }

    private var headerLedge: CGFloat? {
        if let l = registeredLedge, l > safeTop + 60, l < screen.height * 0.5 { return l }
        return nil
    }
    private var liveLedge: CGFloat { headerLedge ?? safeTop + 96 }

    /// Fixes the ledge for the scene about to play.
    func freezeLedge() {
        anchorLedge = headerLedge
        sceneLedge = liveLedge
    }

    /// How far the header art has scrolled since the scene started.
    var ride: CGFloat {
        guard let a = anchorLedge, let r = registeredLedge else { return 0 }
        return r - a
    }

    /// True for a whole cat standing on the header art (not one behind the island or mid-jump).
    func onLedge(_ a: CatActor, _ p: CGPoint) -> Bool {
        guard anchorLedge != nil, let l = sceneLedge, case .cat = a.art else { return false }
        return abs(p.y + a.size.height * 76 / 80 - l) < 4
    }

    /// Where an actor shows on screen now: its spot in the scene, plus the header's scroll if it's
    /// standing on the header.
    func shown(_ a: CatActor, at now: Double) -> CGPoint {
        let p = a.position(at: now)
        guard onLedge(a, p) else { return p }
        return CGPoint(x: p.x, y: p.y + ride)
    }

    /// Cats on the header that a scroll carried away with it slip out of sight (their ground has
    /// moved). Any other cat in the scene stays and carries on. Returns false when no cat is left
    /// showing, so the scene can end.
    func dropCarried() -> Bool {
        if abs(ride) > 8 {
            let now = clock
            for a in actors where a.opacity > 0 && onLedge(a, a.position(at: now)) {
                a.tappable = false
                withAnimation(.easeOut(duration: 0.4)) { a.opacity = 0 }
            }
        }
        return actors.contains { $0.art.cat != nil && $0.tappable && $0.opacity > 0.3 }
    }

    // MARK: clock

    // Scenes run on their own clock, which stops while the person scrolls. Everything (walks, hops,
    // tail swishes, Zzz) freezes in place on screen and picks up where it left off once the page
    // settles.

    /// When the clock stopped, if it's stopped.
    private(set) var heldSince: Double?
    @ObservationIgnored private var heldTotal: Double = 0

    var held: Bool { heldSince != nil }

    var clock: Double {
        let real = Date().timeIntervalSinceReferenceDate
        return real - heldTotal - (heldSince.map { real - $0 } ?? 0)
    }

    static var now: Double { shared.clock }

    func hold(_ on: Bool) {
        let real = Date().timeIntervalSinceReferenceDate
        if on, heldSince == nil { heldSince = real }
        if !on, let h = heldSince { heldTotal += real - h; heldSince = nil }
    }

    /// Like Task.sleep, but counted on the scene clock: time spent scrolling doesn't count.
    func wait(_ seconds: Double) async throws {
        let end = clock + seconds
        while true {
            let left = end - clock
            if left <= 0.001 && !held { return }
            try await Task.sleep(for: .seconds(held ? 0.05 : max(left, 0.01)))
        }
    }

    func measure(_ window: UIWindow) {
        screen = window.bounds.size
        safeTop = window.safeAreaInsets.top
        let w = screen.width
        let phone = UIDevice.current.userInterfaceIdiom == .phone
        if phone && safeTop >= 51 {           // Dynamic Island
            cover = CGRect(x: (w - 126) / 2, y: 11, width: 126, height: 37); hasCover = true
        } else if phone && safeTop >= 44 {    // notch
            let nw = min(210, w * 0.42)
            cover = CGRect(x: (w - nw) / 2, y: 0, width: nw, height: 32); hasCover = true
        } else {                              // no cutout: hide just above the screen
            cover = CGRect(x: (w - 140) / 2, y: -44, width: 140, height: 44); hasCover = false
        }
    }

    @discardableResult
    func add(_ art: CatArt, at p: CGPoint, facing: CGFloat = 1, opacity: Double = 1) -> CatActor {
        let a = CatActor(art, at: p, facing: facing); a.opacity = opacity
        actors.append(a); return a
    }

    func remove(_ a: CatActor) { actors.removeAll { $0 === a } }

    func clear() { actors = []; effects = []; sunbeam = false; sceneLedge = nil; anchorLedge = nil }

    func effect(_ k: CatEffect.Kind, at p: CGPoint) {
        let now = Self.now
        effects.removeAll { now - $0.start > $0.duration }
        effects.append(CatEffect(kind: k, at: p, start: now))
    }

    /// Starts a glide and returns when it's done.
    func move(_ a: CatActor, to p: CGPoint, _ seconds: Double, arc: CGFloat = 0, ease: Glide.Ease = .linear) async throws {
        let now = Self.now
        a.path = Glide(from: a.position(at: now), to: p, start: now, duration: seconds, arc: arc, ease: ease)
        a.origin = p
        try await wait(seconds)
        a.path = nil
    }

    /// The same, without waiting (two things moving at once).
    func glide(_ a: CatActor, to p: CGPoint, _ seconds: Double, arc: CGFloat = 0, ease: Glide.Ease = .linear) {
        let now = Self.now
        a.path = Glide(from: a.position(at: now), to: p, start: now, duration: seconds, arc: arc, ease: ease)
        a.origin = p
    }

    func place(_ a: CatActor, _ p: CGPoint) { a.path = nil; a.origin = p }

    /// The cat or cat part under a point, if any.
    func cat(at point: CGPoint) -> CatActor? {
        let now = Self.now
        return actors.reversed().first { a in
            guard a.tappable, a.opacity > 0.3, a.art.cat != nil else { return false }
            return CGRect(origin: shown(a, at: now), size: a.size).insetBy(dx: -6, dy: -6).contains(point)
        }
    }

    func near(_ point: CGPoint, within d: CGFloat) -> Bool {
        let now = Self.now
        return actors.contains { a in
            a.art.cat != nil && a.opacity > 0.3 && CGRect(origin: shown(a, at: now), size: a.size).insetBy(dx: -d, dy: -d).contains(point)
        }
    }
}

// MARK: - Drawing

struct CatStageView: View {
    @State private var stage = CatStage.shared

    var body: some View {
        // Paused while scrolling too: the cats hold still, and a cat riding the header is redrawn by
        // the header's own moves.
        TimelineView(.animation(minimumInterval: 1 / 30, paused: !stage.playing || stage.held)) { _ in
            let now = stage.clock
            ZStack(alignment: .topLeading) {
                if stage.sunbeam {
                    LinearGradient(colors: [Color(red: 1, green: 0.8, blue: 0.47).opacity(0),
                                            Color(red: 1, green: 0.78, blue: 0.41).opacity(0.32),
                                            Color(red: 1, green: 0.8, blue: 0.47).opacity(0)],
                                   startPoint: .top, endPoint: .bottom)
                        .frame(width: stage.screen.width * 1.4, height: 150)
                        .rotationEffect(.degrees(-12))
                        .position(x: stage.screen.width / 2, y: stage.ledge + stage.ride - 40)
                        .transition(.opacity)
                }
                ForEach(stage.actors) { a in
                    let p = stage.shown(a, at: now)
                    Canvas { g, size in a.art.draw(g, size: size, t: now) }
                        .frame(width: a.size.width, height: a.size.height)
                        .scaleEffect(x: a.facing, y: 1)
                        .catOutline()
                        .opacity(a.opacity)
                        .offset(x: p.x, y: p.y)
                }
                ForEach(stage.effects) { e in effect(e, now) }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        }
        .ignoresSafeArea()
        .allowsHitTesting(false)
        .animation(.easeInOut(duration: 1.2), value: stage.sunbeam)
        .accessibilityHidden(true)
    }

    @ViewBuilder
    private func effect(_ e: CatEffect, _ now: Double) -> some View {
        let u = max(0, min(1, (now - e.start) / e.duration))
        switch e.kind {
        case .zzz:
            Text("z").font(.system(size: 13 + 4 * u, weight: .bold, design: .rounded)).foregroundStyle(Theme.dim)
                .opacity(u < 0.2 ? u * 5 : 1 - u).offset(x: e.at.x + 14 * u, y: e.at.y - 34 * u)
        case .heart:
            Image(systemName: "heart.fill").font(.system(size: 12)).foregroundStyle(Theme.little)
                .opacity(1 - u).offset(x: e.at.x + 6 * sin(u * 6), y: e.at.y - 30 * u)
        case .bubble(let text):
            Text(text).font(.caption.weight(.semibold)).foregroundStyle(Theme.fg)
                .padding(.horizontal, 8).padding(.vertical, 3)
                .background(Theme.card, in: Capsule()).shadow(color: .black.opacity(0.15), radius: 3, y: 1)
                .opacity(u > 0.85 ? (1 - u) / 0.15 : 1).offset(x: e.at.x, y: e.at.y)
        }
    }
}

// MARK: - Window

/// Hosts the stage, and leaves the status bar to the app underneath.
final class CatHost: UIHostingController<CatStageView> {
    weak var scene: UIWindowScene?
    private var app: UIViewController? {
        var vc = scene?.windows.first { !($0 is CatWindow) }?.rootViewController
        while let p = vc?.presentedViewController { vc = p }
        return vc
    }
    override var childForStatusBarStyle: UIViewController? { app }
    override var childForStatusBarHidden: UIViewController? { app }
}

/// Lets every touch through except one on a cat; tells the director about all of them.
final class CatWindow: UIWindow {
    let catcher = UIView()

    override init(windowScene: UIWindowScene) {
        super.init(windowScene: windowScene)
        backgroundColor = .clear
        windowLevel = UIWindow.Level(rawValue: UIWindow.Level.normal.rawValue + 1)
        let host = CatHost(rootView: CatStageView())
        host.scene = windowScene
        host.view.backgroundColor = .clear
        rootViewController = host
        catcher.backgroundColor = .clear
        catcher.addGestureRecognizer(UITapGestureRecognizer(target: self, action: #selector(tapped(_:))))
        host.view.addSubview(catcher)
        catcher.frame = bounds
        catcher.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        catcher.isAccessibilityElement = false
        accessibilityElementsHidden = true
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    override var canBecomeKey: Bool { false }

    override func hitTest(_ point: CGPoint, with event: UIEvent?) -> UIView? {
        guard event?.type == .touches else { return nil }
        let onCat = MainActor.assumeIsolated { CatDirector.shared.touch(at: point) }
        return onCat ? catcher : nil
    }

    @objc private func tapped(_ r: UITapGestureRecognizer) {
        CatDirector.shared.tap(at: r.location(in: self))
    }
}
