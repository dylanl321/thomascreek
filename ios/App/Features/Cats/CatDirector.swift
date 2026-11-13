import SwiftUI
import UIKit

/// Decides when Kiki and Bri come out, and makes sure they never get in the way.
///
/// Every so often (how often is the person's setting) a random short scene plays, but only when the
/// app is open and still: no touch for a few seconds, no sheet or keyboard up, no camera or video on
/// screen, VoiceOver off. Tapping near a cat sends it away at once. With Reduce Motion on, a cat
/// just sits by the island for a moment instead.
///
/// Scrolling never moves a cat or sends it away: while the page moves, every cat holds its spot on
/// screen and the scene picks up again once the page settles. The one exception is a cat standing on
/// the Home header art, which travels with the header (if the header has moved by the time scrolling
/// stops, that cat slips away).
@MainActor
final class CatDirector {
    static let shared = CatDirector()

    enum Frequency: String, CaseIterable, Identifiable {
        case off, rarely, sometimes, often
        var id: String { rawValue }
        var label: String {
            switch self { case .off: "Off"; case .rarely: "Rarely"; case .sometimes: "Now and then"; case .often: "Often" }
        }
        /// Shortest wait between two scenes.
        var gap: TimeInterval {
            switch self { case .off: .infinity; case .rarely: 12 * 60; case .sometimes: 4 * 60; case .often: 75 }
        }
    }

    enum Scene: CaseIterable {
        case kikiSun, kikiHide, kikiYawn, kikiFood, briMouse, briBird, briHang, briFight, briDash, celebrate
        var cat: CatKind { switch self { case .kikiSun, .kikiHide, .kikiYawn, .kikiFood, .celebrate: .kiki; default: .bri } }
        /// Scenes that can come up on their own (the rest answer something the person did).
        static let random: [Scene] = [.kikiSun, .kikiHide, .kikiYawn, .kikiFood, .briMouse, .briBird, .briHang, .briFight]
        /// Quiet enough for late evening.
        var quiet: Bool { self == .kikiHide || self == .kikiYawn }
    }

    let stage = CatStage.shared
    private var window: CatWindow?
    private var loop: Task<Void, Never>?
    private var scene: Task<Void, Never>?
    private var lastTouch = Date.distantPast
    private var lastScene = Date()
    private var lastPlayed: Scene?
    private var stayAway = 0
    private var keyboardUp = false
    private var observers: [NSObjectProtocol] = []
    private let scrollWatch = CatScrollWatch()
    private var scrolling = false
    private var lastScroll = Date.distantPast
    private var nearTouch: Task<Void, Never>?

    var frequency: Frequency {
        if let s = SettingsStore.shared.optionalString(SettingKey.cats), let f = Frequency(rawValue: s) { return f }
        return defaultFrequency
    }

    var defaultFrequency: Frequency {
        switch HomeStore.shared.role {
        case .child: .often
        case .guest, .tablet, .pending: .rarely
        default: .sometimes
        }
    }

    var isOn: Bool { frequency != .off }

    // MARK: setup

    /// Called once the main tabs are up. Safe to call again.
    func install() {
        guard window == nil,
              let scene = UIApplication.shared.connectedScenes.compactMap({ $0 as? UIWindowScene })
                .first(where: { $0.activationState == .foregroundActive }) ?? UIApplication.shared.connectedScenes.first as? UIWindowScene
        else { return }
        let w = CatWindow(windowScene: scene)
        w.isHidden = false
        window = w
        let nc = NotificationCenter.default
        observers = [
            nc.addObserver(forName: UIResponder.keyboardWillShowNotification, object: nil, queue: .main) { _ in
                MainActor.assumeIsolated { self.keyboardUp = true; self.stop() }
            },
            nc.addObserver(forName: UIResponder.keyboardWillHideNotification, object: nil, queue: .main) { _ in
                MainActor.assumeIsolated { self.keyboardUp = false }
            },
            nc.addObserver(forName: UIApplication.didEnterBackgroundNotification, object: nil, queue: .main) { _ in
                MainActor.assumeIsolated { self.stop(now: true) }
            },
        ]
        scrollWatch.onChange = { [weak self] on in self?.scrollChanged(on) }
        watchScrolls()
        lastScene = Date().addingTimeInterval(-min(frequency.gap, 15 * 60) + Double.random(in: 25...70))  // the first visit comes fairly soon
        loop = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(Double.random(in: 10...20)))
                self?.maybePlay()
            }
        }
    }

    private var appWindow: UIWindow? {
        window?.windowScene?.windows.first { !($0 is CatWindow) && $0.isKeyWindow }
            ?? window?.windowScene?.windows.first { !($0 is CatWindow) }
    }

    // MARK: scrolling

    private func watchScrolls() {
        if let w = appWindow { scrollWatch.attach(to: w) }
    }

    private func scrollChanged(_ on: Bool) {
        scrolling = on
        lastScroll = Date()
        if on { nearTouch?.cancel(); nearTouch = nil }
        stage.hold(on)
        // A cat standing on the header went off with it: its ground has moved, so it leaves.
        if !on, scene != nil, stage.carriedOff { stop() }
    }

    // MARK: staying out of the way

    /// Screens where cats must not appear (live cameras, video). Balanced by `allowAgain`.
    func keepAway() { stayAway += 1; stop() }
    func allowAgain() { stayAway = max(0, stayAway - 1) }

    /// True when nothing else is going on.
    private var calm: Bool {
        UIApplication.shared.applicationState == .active
            && stayAway == 0 && !keyboardUp
            && !UIAccessibility.isVoiceOverRunning
            && appWindow?.rootViewController?.presentedViewController == nil
            && HomeStore.shared.role != .pending
    }

    /// Every touch on the screen comes through here first. Returns true when it lands on a cat.
    func touch(at point: CGPoint) -> Bool {
        lastTouch = Date()
        guard scene != nil else { return false }
        if stage.cat(at: point) != nil { return true }
        // A finger landing near a cat sends it away, unless it turns out to be a scroll (then the cat
        // just holds still until the page settles).
        if stage.near(point, within: 44) {
            nearTouch?.cancel()
            nearTouch = Task { [weak self] in
                try? await Task.sleep(for: .seconds(0.3))
                guard let self, !Task.isCancelled, !self.scrolling else { return }
                self.stop()
            }
        }
        return false
    }

    // MARK: playing

    private func maybePlay() {
        watchScrolls()
        guard isOn, scene == nil, calm, !scrolling, Date().timeIntervalSince(lastScroll) > 2,
              Date().timeIntervalSince(lastTouch) > 4,
              Date().timeIntervalSince(lastScene) > frequency.gap * .random(in: 0.8...1.3),
              Bool.random() else { return }
        play(pick())
    }

    private func pick() -> Scene {
        let h = Calendar.current.component(.hour, from: .now)
        let night = h >= 21 || h < 6
        let day = (8..<18).contains(h)
        let options = Scene.random.filter { s in
            s != lastPlayed && (!night || s.quiet) && (s != .kikiSun || day)
        }
        return options.randomElement() ?? .kikiHide
    }

    /// Plays a scene now if the person allows cats and nothing is going on. Used by the hidden spots.
    func perform(_ s: Scene, chance: Double = 1) {
        guard isOn, scene == nil, calm, Double.random(in: 0..<1) < chance else { return }
        play(s)
    }

    private func play(_ s: Scene) {
        guard let window else { return }
        stage.measure(appWindow ?? window)
        stage.clear()
        stage.freezeLedge()
        stage.playing = true
        lastPlayed = s
        scene = Task { [weak self] in
            guard let self else { return }
            do {
                if UIAccessibility.isReduceMotionEnabled { try await still(s.cat) } else { try await run(s) }
            } catch {}
            await self.tidy()
        }
    }

    /// Ends whatever is playing: everything fades out quickly.
    func stop(now: Bool = false) {
        guard let s = scene else { return }
        s.cancel()
        if now { stage.clear(); stage.playing = false; scene = nil; lastScene = Date() }
    }

    private func tidy() async {
        if !stage.actors.isEmpty || stage.sunbeam {
            withAnimation(.easeOut(duration: 0.25)) { for a in stage.actors { a.opacity = 0 } }
            stage.sunbeam = false
            try? await Task.sleep(for: .seconds(0.3))
        }
        stage.clear()
        stage.playing = false
        scene = nil
        lastScene = Date()
    }

    // MARK: taps

    func tap(at point: CGPoint) {
        guard let a = stage.cat(at: point), let kind = a.art.cat else { return }
        let p = stage.shown(a, at: CatStage.now)
        switch kind {
        case .kiki:
            // Rolls over and purrs.
            for i in 0..<3 { stage.effect(.heart, at: CGPoint(x: p.x + a.size.width * 0.35 + CGFloat(i * 8), y: p.y)) }
            purr()
            if case .cat(_, let pose, let gait) = a.art, pose != .sleep {
                a.art = .cat(.kiki, .sleep, gait)
                Task { try? await Task.sleep(for: .seconds(1.6)); if case .cat(_, .sleep, _) = a.art { a.art = .cat(.kiki, pose, gait) } }
            }
        case .bri:
            // Gone in a flash.
            UIImpactFeedbackGenerator(style: .light).impactOccurred()
            if case .cat = a.art {
                a.art = .cat(.bri, .walk, .dash); a.tappable = false
                let off = p.x < stage.screen.width / 2 ? -a.size.width - 10 : stage.screen.width + 10
                a.facing = off < 0 ? -1 : 1
                stage.glide(a, to: CGPoint(x: off, y: p.y), 0.45)
            }
        }
    }

    private func purr() {
        let gen = UIImpactFeedbackGenerator(style: .soft)
        gen.prepare()
        Task {
            for _ in 0..<18 {
                gen.impactOccurred(intensity: 0.45)
                try? await Task.sleep(for: .milliseconds(45))
            }
        }
    }

    // MARK: scene helpers

    /// Waits, and gives up (ending the scene) if something else started meanwhile.
    func pause(_ s: Double) async throws {
        try await stage.wait(s)
        if !calm { throw CancellationError() }
    }

    func groundTop(_ k: CatKind) -> CGFloat { stage.ledge - k.height * 76 / 80 }
    func offLeft(_ w: CGFloat) -> CGFloat { -w - 12 }
    var offRight: CGFloat { stage.screen.width + 12 }

    func fade(_ a: CatActor, to v: Double, _ s: Double) async throws {
        withAnimation(.easeInOut(duration: s)) { a.opacity = v }
        try await stage.wait(s)
    }

    func zzz(_ p: CGPoint) { stage.effect(.zzz, at: p) }

    /// Reduce Motion: no walking or jumping. The cat is simply there for a moment.
    private func still(_ k: CatKind) async throws {
        let c = stage.cover
        let spot = stage.hasCover
            ? CGPoint(x: c.maxX + 6, y: c.maxY + 2)
            : CGPoint(x: stage.screen.width - k.width - 24, y: groundTop(k))
        let a = stage.add(.cat(k, .sit, .trot), at: spot, facing: -1, opacity: 0)
        try await fade(a, to: 1, 0.5)
        try await pause(4)
        try await fade(a, to: 0, 0.6)
    }
}

// MARK: - Spots in the app

private struct CatsKeepAway: ViewModifier {
    func body(content: Content) -> some View {
        content
            .onAppear { CatDirector.shared.keepAway() }
            .onDisappear { CatDirector.shared.allowAgain() }
    }
}

extension View {
    /// No cats while this screen is up (live cameras, video).
    func catsStayAway() -> some View { modifier(CatsKeepAway()) }

    /// Cats walk along the bottom of this view (the Home header art) while it's on screen.
    func catLedge() -> some View {
        onGeometryChange(for: CGFloat.self) { $0.frame(in: .global).maxY } action: { CatStage.shared.registeredLedge = $0 }
            .onDisappear { CatStage.shared.registeredLedge = nil }
    }
}

// MARK: - Noticing scrolls

/// Notices any scroll in the app, from the moment a finger drags until the page has stopped gliding,
/// without getting in its way: a pan recogniser on the app window that runs alongside everything and
/// cancels nothing, plus a watch on the scroll view under the finger for the glide after it lifts.
@MainActor
final class CatScrollWatch: NSObject, UIGestureRecognizerDelegate {
    var onChange: ((Bool) -> Void)?
    private(set) var scrolling = false
    private let pan = UIPanGestureRecognizer()
    private weak var window: UIWindow?
    private var offset: NSKeyValueObservation?
    private var settle: Task<Void, Never>?
    private var dragging = false

    func attach(to w: UIWindow) {
        guard window !== w else { return }
        pan.view?.removeGestureRecognizer(pan)
        pan.removeTarget(nil, action: nil)
        pan.addTarget(self, action: #selector(panned(_:)))
        pan.cancelsTouchesInView = false
        pan.delaysTouchesBegan = false
        pan.delaysTouchesEnded = false
        pan.delegate = self
        w.addGestureRecognizer(pan)
        window = w
    }

    func gestureRecognizer(_ g: UIGestureRecognizer, shouldRecognizeSimultaneouslyWith other: UIGestureRecognizer) -> Bool { true }
    func gestureRecognizer(_ g: UIGestureRecognizer, shouldRequireFailureOf other: UIGestureRecognizer) -> Bool { false }
    func gestureRecognizer(_ g: UIGestureRecognizer, shouldBeRequiredToFailBy other: UIGestureRecognizer) -> Bool { false }

    @objc private func panned(_ g: UIPanGestureRecognizer) {
        switch g.state {
        case .began:
            dragging = true
            follow(scrollView(at: g.location(in: g.view), in: g.view))
            moved()
        case .changed:
            moved()
        default:
            dragging = false
            settleSoon()
        }
    }

    /// The scroll view under the finger, if any.
    private func scrollView(at p: CGPoint, in v: UIView?) -> UIScrollView? {
        var view = v?.hitTest(p, with: nil)
        while let cur = view {
            if let s = cur as? UIScrollView, s.isScrollEnabled { return s }
            view = cur.superview
        }
        return nil
    }

    private func follow(_ s: UIScrollView?) {
        offset = nil
        guard let s else { return }
        offset = s.observe(\.contentOffset, options: [.new]) { [weak self] _, _ in
            MainActor.assumeIsolated { self?.moved() }
        }
    }

    private func moved() {
        if !scrolling { scrolling = true; onChange?(true) }
        settleSoon()
    }

    /// Scrolling has stopped once the finger is up and the page hasn't moved for a moment.
    private func settleSoon() {
        settle?.cancel()
        settle = Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(220))
            guard let self, !Task.isCancelled, !self.dragging, self.scrolling else { return }
            self.scrolling = false
            self.offset = nil
            self.onChange?(false)
        }
    }
}
