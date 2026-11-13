import SwiftUI

// Kiki and Bri, the family's two tuxedo cats, drawn as vectors.
//
// Everything is drawn in a 120×80 box (the cat faces right, its feet on y = 76) and scaled to the
// cat's size, so there are no image assets. The shapes match the approved prototype
// (mockups/cats-prototype.html). Small motions (legs, tail, blinking, ears, breathing) are worked out
// from the clock `t`, so a sprite only needs redrawing while a scene plays.

enum CatKind: String, CaseIterable, Identifiable {
    /// Big, round and very fluffy. Naps, yawns, sunbathes, begs for food.
    case kiki
    /// Sleek and short-haired. Always hunting.
    case bri

    var id: String { rawValue }
    var fat: Bool { self == .kiki }
    var name: String { self == .kiki ? "Kiki" : "Bri" }
    /// Width of the 120×80 art box, in points.
    var width: CGFloat { fat ? 86 : 76 }
    var height: CGFloat { width * 2 / 3 }
}

enum CatPose: Equatable {
    case stand, walk, sit, yawn, sleep, crouch, leap, hang, peek, peekYawn, grump
}

/// How fast the legs move while walking.
enum CatGait { case waddle, trot, dash
    var period: Double { switch self { case .waddle: 1.4; case .trot: 0.84; case .dash: 0.36 } }
}

enum CatInk {
    static let fur = Color(uiColor: UIColor(hex: 0x17161C))
    static let white = Color(uiColor: UIColor(hex: 0xF4F2EC))
    static let eye = Color(uiColor: UIColor(hex: 0xC8D96A))
    static let pupil = Color(uiColor: UIColor(hex: 0x0D0D10))
    static let nose = Color(uiColor: UIColor(hex: 0x4A3238))
    static let ear = Color(uiColor: UIColor(hex: 0x5D4650))
    static let mouth = Color(uiColor: UIColor(hex: 0x2A0F15))
    static let tongue = Color(uiColor: UIColor(hex: 0xE48EA0))
    static let lid = Color(uiColor: UIColor(hex: 0x8B8A93))
}

/// A cat in one pose. `t` is the scene clock in seconds.
struct CatSprite: View {
    let kind: CatKind
    var pose: CatPose = .sit
    var gait: CatGait = .trot
    var t: Double = 0

    var body: some View {
        Canvas { g, size in
            CatPainter(kind: kind, pose: pose, gait: gait, t: t).draw(g, size: size)
        }
        .frame(width: kind.width, height: kind.height)
        .catOutline()
    }
}

/// A sprite that animates on its own (for the still spots: empty lists, the meal plan, settings).
struct LivingCat: View {
    let kind: CatKind
    var pose: CatPose = .sit
    var scale: CGFloat = 1
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        TimelineView(.animation(minimumInterval: 1 / 30, paused: reduceMotion)) { tl in
            Canvas { g, size in
                CatPainter(kind: kind, pose: pose, gait: .trot, t: reduceMotion ? 0 : tl.date.timeIntervalSinceReferenceDate)
                    .draw(g, size: size)
            }
        }
        .frame(width: kind.width * scale, height: kind.height * scale)
        .catOutline()
        .accessibilityLabel(kind.name)
    }
}

extension View {
    /// A black cat needs a faint light rim to read on a dark screen, and a soft shadow on a light one.
    func catOutline() -> some View {
        shadow(color: .white.opacity(0.45), radius: 0.6).shadow(color: .black.opacity(0.18), radius: 2, y: 2)
    }
}

// MARK: - Painter

struct CatPainter {
    let kind: CatKind
    let pose: CatPose
    var gait: CatGait = .trot
    let t: Double

    private var F: Bool { kind.fat }

    func draw(_ context: GraphicsContext, size: CGSize) {
        var g = context
        let s = size.width / 120
        g.scaleBy(x: s, y: s)
        switch pose {
        case .stand, .walk: walk(g, moving: pose == .walk)
        case .sit, .yawn: sit(g, yawn: pose == .yawn)
        case .grump: sit(g, yawn: false, grump: true)
        case .sleep: sleep(g)
        case .crouch: crouch(g)
        case .leap: leap(g)
        case .hang: hang(g)
        case .peek, .peekYawn: peek(g, yawn: pose == .peekYawn)
        }
    }

    // MARK: motions (all from the clock)

    private func wave(_ period: Double, _ phase: Double = 0) -> Double { sin((t / period + phase) * 2 * .pi) }
    private var swish: Double { wave(3.2) * 12 }
    private var legSwing: Double { pose == .walk ? wave(gait.period) * 16 : 0 }
    private var blinking: Bool { t.truncatingRemainder(dividingBy: 4.2) > 3.98 }
    private func earTwitch(_ period: Double, _ offset: Double) -> Double {
        let p = (t + offset).truncatingRemainder(dividingBy: period) / period
        return p > 0.86 && p < 0.93 ? -16 * sin((p - 0.86) / 0.07 * .pi) : 0
    }

    // MARK: poses

    private func walk(_ g0: GraphicsContext, moving: Bool) {
        var g = g0
        if moving && F { rotate(&g, wave(1.4) * 3, 60, 76); g.translateBy(x: 0, y: -abs(wave(1.4)) * 1.5) }
        if moving && !F { g.translateBy(x: 0, y: -abs(wave(gait.period / 2)) * 1.5) }
        if F {
            tail(g, [(22, 46), (4, 44), (2, 26), (12, 14)], w: 10, base: (22, 46), angle: swish)
            leg(g, 36, 58, 9, swing: -legSwing); leg(g, 64, 58, 9, swing: legSwing)
            var belly = g
            if moving { stretch(&belly, 1 + 0.03 * abs(wave(0.7)), 1 + 0.06 * abs(wave(0.7)), 50, 30) }
            blob(belly, 50, 50, 34, 21)
            blob(belly, 54, 66, 16, 6, color: CatInk.white, n: 10, tuft: 2.4)
            leg(g, 44, 62, 9, swing: legSwing); leg(g, 72, 62, 9, swing: -legSwing)
            blob(g, 76, 52, 9, 13, color: CatInk.white, n: 12, tuft: 2.6)
            head(g, 86, 32, 14)
        } else {
            tail(g, [(28, 48), (14, 44), (10, 30), (16, 16)], w: 4.2, base: (28, 48), angle: swish)
            leg(g, 32, 50, 5.5, swing: -legSwing); leg(g, 66, 50, 5.5, swing: legSwing)
            ellipse(g, 52, 50, 27, 10.5, CatInk.fur)
            leg(g, 38, 52, 5.5, swing: legSwing); leg(g, 72, 52, 5.5, swing: -legSwing)
            ellipse(g, 73, 55, 5, 7, CatInk.white)
            head(g, 84, 36, 12)
        }
    }

    private func sit(_ g: GraphicsContext, yawn: Bool, grump: Bool = false) {
        let face = HeadStyle(eyes: yawn ? .closed : (grump ? .sleepy : .open), mouth: yawn, earsBack: grump)
        if F {
            tail(g, [(32, 74), (12, 78), (6, 68), (12, 58)], w: 9, base: (32, 74), angle: swish * 0.6)
            var b = g; stretch(&b, 1, 1 + 0.02 * wave(2.6), 50, 76)
            blob(b, 46, 58, 27, 19)
            blob(b, 58, 48, 21, 24, rot: 8)
            blob(b, 64, 50, 11, 17, rot: 6, color: CatInk.white, n: 14, tuft: 3)
            leg(g, 60, 62, 9); leg(g, 70, 62, 9)
            head(g, 64, 20, 14, face)
        } else {
            tail(g, [(40, 74), (24, 78), (16, 72), (20, 64)], w: 4.2, base: (40, 74), angle: swish * 0.6)
            ellipse(g, 46, 62, 14, 13, CatInk.fur)
            ellipse(g, 54, 52, 12, 20, CatInk.fur, rot: 14)
            ellipse(g, 60, 50, 6, 12, CatInk.white, rot: 8)
            leg(g, 58, 56, 5.5); leg(g, 64, 56, 5.5)
            head(g, 60, 26, 12, face)
        }
    }

    private func sleep(_ g: GraphicsContext) {
        var b = g
        stretch(&b, 1, 1 + 0.06 * (0.5 + 0.5 * wave(2.6)), 56, 76)
        if F {
            blob(b, 54, 60, 38, 18)
            blob(b, 60, 71, 16, 5, color: CatInk.white, n: 10, tuft: 2.2)
            tail(b, [(18, 70), (34, 84), (76, 84), (92, 77)], w: 9, base: (18, 70), angle: 0)
            ellipse(g, 96, 75, 5.5, 3.2, CatInk.white)
            head(g, 92, 62, 13.5, HeadStyle(eyes: .closed))
        } else {
            ellipse(b, 56, 66, 30, 10, CatInk.fur)
            ellipse(b, 62, 72, 12, 3, CatInk.white)
            tail(b, [(30, 70), (40, 80), (74, 80), (88, 76)], w: 4, base: (30, 70), angle: 0)
            ellipse(g, 92, 74, 4, 2.6, CatInk.white)
            head(g, 88, 65, 11, HeadStyle(eyes: .closed))
        }
    }

    private func crouch(_ g: GraphicsContext) {
        tail(g, [(26, 60), (14, 62), (6, 62), (2, 55)], w: F ? 9 : 4.2, base: (26, 60), angle: wave(1) * 9)
        var rear = g
        rear.translateBy(x: wave(0.32) * 1.2, y: 0)
        leg(rear, 34, 60, F ? 9 : 5.5); leg(rear, 40, 60, F ? 9 : 5.5)
        if F { blob(rear, 50, 62, 34, 15, rot: 8) } else { ellipse(rear, 50, 64, 26, 9, CatInk.fur, rot: 8) }
        leg(g, 68, 66, F ? 9 : 5.5, to: 76); leg(g, 74, 66, F ? 9 : 5.5, to: 76)
        ellipse(g, 72, 66, 5, 4, CatInk.white)
        head(g, 84, 58, F ? 13 : 11, HeadStyle(eyes: .wide))
    }

    private func leap(_ g0: GraphicsContext) {
        var g = g0
        rotate(&g, -16, 60, 50)
        tail(g, [(28, 48), (16, 44), (8, 40), (0, 36)], w: F ? 9 : 4.2, base: (28, 48), angle: 0)
        limb(g, 36, 54, 12, 64, F ? 7 : 5.5); limb(g, 42, 55, 20, 68, F ? 7 : 5.5)
        if F { blob(g, 58, 50, 36, 17) } else { ellipse(g, 58, 50, 32, 9, CatInk.fur) }
        limb(g, 78, 52, 102, 44, F ? 7 : 5.5); limb(g, 74, 54, 100, 52, F ? 7 : 5.5)
        head(g, 90, 38, F ? 12 : 11, HeadStyle(eyes: .wide))
    }

    private func hang(_ g: GraphicsContext) {
        limb(g, 50, 34, 46, 4, F ? 7 : 5.5); limb(g, 70, 34, 74, 4, F ? 7 : 5.5)
        tail(g, [(58, 62), (56, 72), (62, 78), (58, 80)], w: F ? 9 : 4, base: (58, 62), angle: swish)
        var kick = g; kick.translateBy(x: wave(0.32) * 1.5, y: 0)
        limb(kick, 52, 56, 48 + wave(0.4) * 3, 76, F ? 7 : 5.5)
        limb(kick, 66, 56, 70 - wave(0.4) * 3, 76, F ? 7 : 5.5)
        if F {
            blob(g, 60, 48, 21, 25)
            blob(g, 60, 44, 10, 14, color: CatInk.white, n: 12, tuft: 2.6)
        } else {
            ellipse(g, 60, 46, 12, 20, CatInk.fur)
            ellipse(g, 60, 44, 6, 11, CatInk.white)
        }
        head(g, 58, 24, F ? 12 : 11, HeadStyle(eyes: .wide))
    }

    private func peek(_ g: GraphicsContext, yawn: Bool) {
        if F { blob(g, 46, 48, 32, 17) } else { ellipse(g, 46, 48, 32, 11, CatInk.fur) }
        ellipse(g, 82, 60, 4.5, 2.8, CatInk.white); ellipse(g, 74, 61, 4.5, 2.8, CatInk.white)
        var h = g
        if !yawn { rotate(&h, wave(2.2) * 11, 84, 50) }
        head(h, 90, 42, F ? 13.5 : 12, HeadStyle(eyes: yawn ? .closed : .open, mouth: yawn))
    }

    // MARK: head

    struct HeadStyle {
        enum Eyes { case open, wide, closed, sleepy }
        var eyes: Eyes = .open
        var mouth = false
        var earsBack = false
    }

    private func head(_ g: GraphicsContext, _ x: CGFloat, _ y: CGFloat, _ r: CGFloat, _ st: HeadStyle = HeadStyle()) {
        let rx = F ? r * 1.13 : r
        if F { blob(g, x - 0.05 * r, y + 0.35 * r, r * 1.32, r * 0.85, n: 22, tuft: r * 0.26) }  // neck ruff
        // ears
        var l = g
        rotate(&l, (st.earsBack ? -28 : 0) + earTwitch(6.3, 1.7), x - 0.4 * rx, y - 0.6 * r)
        poly(l, [(x - 0.8 * rx, y - 0.3 * r), (x - 0.62 * rx, y - 1.3 * r), (x - 0.02 * rx, y - 0.84 * r)], CatInk.fur)
        poly(l, [(x - 0.62 * rx, y - 0.5 * r), (x - 0.56 * rx, y - 1.08 * r), (x - 0.2 * rx, y - 0.8 * r)], CatInk.ear)
        var rr = g
        rotate(&rr, (st.earsBack ? 30 : 0) + earTwitch(5, 0), x + 0.5 * rx, y - 0.6 * r)
        poly(rr, [(x + 0.12 * rx, y - 0.88 * r), (x + 0.74 * rx, y - 1.32 * r), (x + 0.9 * rx, y - 0.26 * r)], CatInk.fur)
        poly(rr, [(x + 0.3 * rx, y - 0.84 * r), (x + 0.7 * rx, y - 1.1 * r), (x + 0.76 * rx, y - 0.46 * r)], CatInk.ear)
        if F {
            var tufts = Path()
            tufts.move(to: CGPoint(x: x + 0.5 * rx, y: y - 0.6 * r)); tufts.addLine(to: CGPoint(x: x + 0.5 * rx + 0.08 * r, y: y - 1.1 * r))
            tufts.move(to: CGPoint(x: x + 0.58 * rx, y: y - 0.55 * r)); tufts.addLine(to: CGPoint(x: x + 0.58 * rx + 0.16 * r, y: y - 0.97 * r))
            rr.stroke(tufts, with: .color(CatInk.white.opacity(0.8)), style: StrokeStyle(lineWidth: 0.06 * r, lineCap: .round))
        }
        // face
        if F { blob(g, x, y, rx, r * 0.98, n: 18, tuft: r * 0.16) } else { ellipse(g, x, y, rx, r, CatInk.fur) }
        if F {
            blob(g, x + 0.32 * rx, y + 0.95 * r, r * 0.62, r * 0.42, color: CatInk.white, n: 12, tuft: r * 0.13)
            ellipse(g, x + 0.38 * rx, y + 0.66 * r, 0.26 * r, 0.15 * r, CatInk.white)
        }
        // eyes
        let er: CGFloat = F ? 0.25 : 0.22
        let eyes: [(CGFloat, CGFloat)] = [(x + 0.02 * rx, y - 0.08 * r), (x + 0.6 * rx, y - 0.08 * r)]
        let closed = st.eyes == .closed || (blinking && st.eyes != .sleepy)
        for (i, e) in eyes.enumerated() {
            let k: CGFloat = i == 1 ? 0.88 : 1
            if closed {
                var lid = Path()
                lid.move(to: CGPoint(x: e.0 - 0.2 * r, y: e.1))
                lid.addQuadCurve(to: CGPoint(x: e.0 + 0.2 * r, y: e.1), control: CGPoint(x: e.0, y: e.1 + 0.18 * r))
                g.stroke(lid, with: .color(CatInk.lid), style: StrokeStyle(lineWidth: 0.08 * r, lineCap: .round))
            } else {
                let squint: CGFloat = st.eyes == .sleepy ? 0.45 : 1
                let pw: CGFloat = st.eyes == .wide ? 0.15 : 0.07
                ellipse(g, e.0, e.1, er * r * k, (er + 0.05) * r * squint, CatInk.eye)
                ellipse(g, e.0 + 0.04 * r, e.1, pw * r * k, (er + 0.01) * r * squint, CatInk.pupil)
                if st.eyes != .sleepy { ellipse(g, e.0 + 0.09 * r, e.1 - 0.1 * r, 0.05 * r, 0.05 * r, .white) }
                if st.eyes == .sleepy {  // flat, unimpressed lids
                    var lid = Path()
                    lid.move(to: CGPoint(x: e.0 - er * r, y: e.1 - 0.06 * r)); lid.addLine(to: CGPoint(x: e.0 + er * r, y: e.1 - 0.1 * r))
                    g.stroke(lid, with: .color(CatInk.fur), style: StrokeStyle(lineWidth: 0.12 * r, lineCap: .round))
                }
            }
        }
        poly(g, [(x + 0.3 * rx, y + 0.26 * r), (x + 0.46 * rx, y + 0.26 * r), (x + 0.38 * rx, y + 0.36 * r)], CatInk.nose)
        if st.mouth {
            ellipse(g, x + 0.38 * rx, y + 0.62 * r, 0.2 * r, 0.3 * r, CatInk.mouth)
            ellipse(g, x + 0.38 * rx, y + 0.76 * r, 0.13 * r, 0.13 * r, CatInk.tongue)
        }
        // whiskers
        let wx = x + 0.38 * rx, wy = y + 0.4 * r
        var w = Path()
        let whiskers: [(CGFloat, CGFloat, CGFloat, CGFloat)] = [(1.05, -0.25, 0.0, 0.0), (1.1, 0.08, 0.0, 0.06), (0.95, 0.34, 0.0, 0.12),
                                 (-0.95, -0.2, -0.2, 0.0), (-0.95, 0.12, -0.2, 0.08)]
        for (dx, dy, ox, oy) in whiskers {
            w.move(to: CGPoint(x: wx + ox * r, y: wy + oy * r))
            w.addLine(to: CGPoint(x: wx + dx * r, y: wy + dy * r))
        }
        g.stroke(w, with: .color(CatInk.white.opacity(0.7)), style: StrokeStyle(lineWidth: 0.5, lineCap: .round))
    }

    // MARK: parts

    /// A leg with a white sock. `swing` turns it about its top (walking).
    private func leg(_ g0: GraphicsContext, _ x: CGFloat, _ top: CGFloat, _ w: CGFloat, to bot: CGFloat = 76, swing: Double = 0) {
        var g = g0
        if swing != 0 { rotate(&g, swing, x, top) }
        g.fill(Path(roundedRect: CGRect(x: x - w / 2, y: top, width: w, height: bot - top), cornerRadius: w / 2), with: .color(CatInk.fur))
        g.fill(Path(roundedRect: CGRect(x: x - w / 2 - 0.4, y: bot - 4.2, width: w + 0.8, height: 4.4), cornerRadius: 2.2), with: .color(CatInk.white))
    }

    private func limb(_ g: GraphicsContext, _ x1: CGFloat, _ y1: CGFloat, _ x2: CGFloat, _ y2: CGFloat, _ w: CGFloat) {
        var p = Path(); p.move(to: CGPoint(x: x1, y: y1)); p.addLine(to: CGPoint(x: x2, y: y2))
        g.stroke(p, with: .color(CatInk.fur), style: StrokeStyle(lineWidth: w, lineCap: .round))
        ellipse(g, x2, y2, w * 0.55, w * 0.55, CatInk.white)
    }

    /// A tail along a cubic curve. Kiki's is a thick, tufted brush.
    private func tail(_ g0: GraphicsContext, _ pts: [(CGFloat, CGFloat)], w: CGFloat, base: (CGFloat, CGFloat), angle: Double) {
        var g = g0
        if angle != 0 { rotate(&g, angle, base.0, base.1) }
        CatPainter.furryStroke(g, pts.map { CGPoint(x: $0.0, y: $0.1) }, width: w, fluffy: F)
    }

    static func furryStroke(_ g: GraphicsContext, _ p: [CGPoint], width w: CGFloat, fluffy: Bool) {
        var path = Path()
        path.move(to: p[0])
        if p.count == 4 { path.addCurve(to: p[3], control1: p[1], control2: p[2]) } else { p.dropFirst().forEach { path.addLine(to: $0) } }
        g.stroke(path, with: .color(CatInk.fur), style: StrokeStyle(lineWidth: fluffy ? w * 1.45 : w, lineCap: .round, lineJoin: .round))
        guard fluffy, p.count == 4 else { return }
        // Tufts sticking out both sides of the brush.
        let n = 16
        for i in 1..<n {
            let u = CGFloat(i) / CGFloat(n)
            let a = bezier(p, u), b = bezier(p, min(1, u + 0.02))
            let dx = b.x - a.x, dy = b.y - a.y, len = max(0.001, hypot(dx, dy))
            let nx = -dy / len, ny = dx / len
            let side: CGFloat = i % 2 == 0 ? 1 : -1
            let reach = w * (0.95 + 0.25 * CGFloat(abs(sin(Double(i) * 2.3))))
            var tri = Path()
            tri.move(to: CGPoint(x: a.x - dx / len * w * 0.35, y: a.y - dy / len * w * 0.35))
            tri.addLine(to: CGPoint(x: a.x + nx * side * reach + dx / len * w * 0.25, y: a.y + ny * side * reach + dy / len * w * 0.25))
            tri.addLine(to: CGPoint(x: a.x + dx / len * w * 0.35, y: a.y + dy / len * w * 0.35))
            tri.closeSubpath()
            g.fill(tri, with: .color(CatInk.fur))
        }
    }

    private static func bezier(_ p: [CGPoint], _ u: CGFloat) -> CGPoint {
        let v = 1 - u
        let a = v * v * v, b = 3 * v * v * u, c = 3 * v * u * u, d = u * u * u
        return CGPoint(x: a * p[0].x + b * p[1].x + c * p[2].x + d * p[3].x, y: a * p[0].y + b * p[1].y + c * p[2].y + d * p[3].y)
    }

    /// Fur: an ellipse ringed with tufts and fine spikes.
    private func blob(_ g0: GraphicsContext, _ cx: CGFloat, _ cy: CGFloat, _ rx: CGFloat, _ ry: CGFloat, rot: Double = 0,
                      color: Color = CatInk.fur, n: Int = 0, tuft: CGFloat = 0) {
        var g = g0
        if rot != 0 { rotate(&g, rot, cx, cy) }
        for piece in CatPainter.blobPieces(cx, cy, rx, ry, n, tuft) { g.fill(piece, with: .color(color)) }
    }

    private static var blobCache: [String: [Path]] = [:]

    static func blobPieces(_ cx: CGFloat, _ cy: CGFloat, _ rx: CGFloat, _ ry: CGFloat, _ n0: Int, _ tuft0: CGFloat) -> [Path] {
        let key = "\(cx),\(cy),\(rx),\(ry),\(n0),\(tuft0)"
        if let hit = blobCache[key] { return hit }
        let n = n0 > 0 ? n0 : max(12, Int(((rx + ry) / 2.2).rounded()))
        let tuft = tuft0 > 0 ? tuft0 : min(rx, ry) * 0.3
        var out = [Path(ellipseIn: CGRect(x: cx - rx, y: cy - ry, width: 2 * rx, height: 2 * ry))]
        func spike(_ a: Double, _ base: CGFloat, _ px: CGFloat, _ py: CGFloat, _ reach: CGFloat) -> Path {
            let tx = cx + CGFloat(cos(a + 0.12)) * (rx + reach), ty = cy + CGFloat(sin(a + 0.12)) * (ry + reach)
            var p = Path()
            p.move(to: CGPoint(x: px - CGFloat(sin(a)) * base, y: py + CGFloat(cos(a)) * base))
            p.addLine(to: CGPoint(x: tx, y: ty))
            p.addLine(to: CGPoint(x: px + CGFloat(sin(a)) * base, y: py - CGFloat(cos(a)) * base))
            p.closeSubpath()
            return p
        }
        for i in 0..<n {
            let a = Double(i) / Double(n) * 2 * .pi
            let j = CGFloat(1 + 0.08 * sin(Double(i) * 2.7))
            let tt = tuft * CGFloat(0.75 + 0.35 * abs(sin(Double(i) * 1.9)))
            let px = cx + CGFloat(cos(a)) * rx * j * 0.93, py = cy + CGFloat(sin(a)) * ry * j * 0.93
            out.append(Path(ellipseIn: CGRect(x: px - tt, y: py - tt, width: 2 * tt, height: 2 * tt)))
            out.append(spike(a, tt * 0.6, px, py, tt * 1.1))
            // a finer wisp between tufts
            let a2 = a + .pi / Double(n)
            let qx = cx + CGFloat(cos(a2)) * rx * 0.97, qy = cy + CGFloat(sin(a2)) * ry * 0.97
            out.append(spike(a2, tt * 0.28, qx, qy, tt * 0.95))
        }
        blobCache[key] = out
        return out
    }

    private func ellipse(_ g0: GraphicsContext, _ cx: CGFloat, _ cy: CGFloat, _ rx: CGFloat, _ ry: CGFloat, _ c: Color, rot: Double = 0) {
        var g = g0
        if rot != 0 { rotate(&g, rot, cx, cy) }
        g.fill(Path(ellipseIn: CGRect(x: cx - rx, y: cy - ry, width: 2 * rx, height: 2 * ry)), with: .color(c))
    }

    private func poly(_ g: GraphicsContext, _ pts: [(CGFloat, CGFloat)], _ c: Color) {
        var p = Path()
        p.move(to: CGPoint(x: pts[0].0, y: pts[0].1))
        pts.dropFirst().forEach { p.addLine(to: CGPoint(x: $0.0, y: $0.1)) }
        p.closeSubpath()
        g.fill(p, with: .color(c))
    }

    private func rotate(_ g: inout GraphicsContext, _ degrees: Double, _ x: CGFloat, _ y: CGFloat) {
        g.translateBy(x: x, y: y); g.rotate(by: .degrees(degrees)); g.translateBy(x: -x, y: -y)
    }

    private func stretch(_ g: inout GraphicsContext, _ sx: CGFloat, _ sy: CGFloat, _ x: CGFloat, _ y: CGFloat) {
        g.translateBy(x: x, y: y); g.scaleBy(x: sx, y: sy); g.translateBy(x: -x, y: -y)
    }
}
