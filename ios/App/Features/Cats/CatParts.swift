import SwiftUI

// The pieces a scene moves around: whole cats, and the bits that show around the Dynamic Island while
// a cat hides behind it (ears above, tail and paws below), plus the mouse, the bird and the fur ball.

enum TailMode { case still, sway, flick, tip }

enum CatArt: Equatable {
    case cat(CatKind, CatPose, CatGait)
    case ears(CatKind, swivel: Bool)
    case tail(CatKind, TailMode)
    case paws(CatKind, knead: Bool)
    case mouse
    case bird(flapping: Bool)
    case brawl

    var size: CGSize {
        switch self {
        case .cat(let k, _, _): CGSize(width: k.width, height: k.height)
        case .ears(let k, _): CGSize(width: k.fat ? 46 : 38, height: (k.fat ? 46 : 38) * 40 / 60)
        case .tail(let k, _): CGSize(width: k.fat ? 30 : 20, height: (k.fat ? 30 : 20) * 90 / 40)
        case .paws(let k, _): CGSize(width: k.fat ? 28 : 22, height: (k.fat ? 28 : 22) * 24 / 40)
        case .mouse: CGSize(width: 30, height: 16)
        case .bird: CGSize(width: 28, height: 22)
        case .brawl: CGSize(width: 110, height: 110 * 2 / 3)
        }
    }

    /// The cat this piece belongs to, for taps.
    var cat: CatKind? {
        switch self {
        case .cat(let k, _, _), .ears(let k, _), .tail(let k, _), .paws(let k, _): k
        default: nil
        }
    }

    /// Draws the piece into a box of `size`, at clock `t`.
    func draw(_ g: GraphicsContext, size: CGSize, t: Double) {
        switch self {
        case .cat(let k, let p, let gait): CatPainter(kind: k, pose: p, gait: gait, t: t).draw(g, size: size)
        case .ears(let k, let swivel): PartPainter.ears(g, size, k, swivel ? t : nil)
        case .tail(let k, let m): PartPainter.tail(g, size, k, m, t)
        case .paws(let k, let knead): PartPainter.paws(g, size, k, knead ? t : nil)
        case .mouse: PartPainter.mouse(g, size, t)
        case .bird(let flapping): PartPainter.bird(g, size, flapping ? t : nil)
        case .brawl: PartPainter.brawl(g, size, t)
        }
    }
}

enum PartPainter {
    private static func wave(_ t: Double, _ period: Double, _ phase: Double = 0) -> Double { sin((t / period + phase) * 2 * .pi) }

    private static func turn(_ g: inout GraphicsContext, _ deg: Double, _ x: CGFloat, _ y: CGFloat) {
        g.translateBy(x: x, y: y); g.rotate(by: .degrees(deg)); g.translateBy(x: -x, y: -y)
    }

    private static func fill(_ g: GraphicsContext, _ pts: [(CGFloat, CGFloat)], _ c: Color) {
        var p = Path(); p.move(to: CGPoint(x: pts[0].0, y: pts[0].1))
        pts.dropFirst().forEach { p.addLine(to: CGPoint(x: $0.0, y: $0.1)) }
        p.closeSubpath(); g.fill(p, with: .color(c))
    }

    private static func oval(_ g: GraphicsContext, _ cx: CGFloat, _ cy: CGFloat, _ rx: CGFloat, _ ry: CGFloat, _ c: Color) {
        g.fill(Path(ellipseIn: CGRect(x: cx - rx, y: cy - ry, width: rx * 2, height: ry * 2)), with: .color(c))
    }

    /// Ears and the top of the head, in a 60×40 box. Seen poking up above the island.
    static func ears(_ g0: GraphicsContext, _ size: CGSize, _ k: CatKind, _ t: Double?) {
        var g = g0; g.scaleBy(x: size.width / 60, y: size.width / 60)
        for (i, e) in [(CGFloat(8), CGFloat(26), CGFloat(12)), (34, 52, 47)].enumerated() {
            var ear = g
            if let t { turn(&ear, wave(t, 2.2, i == 0 ? 0 : 0.5) * 16, (e.0 + e.1) / 2, 28) }
            fill(ear, [(e.0, 30), (e.2, 4), (e.1, 26)], CatInk.fur)
            fill(ear, [(e.0 + 3, 27), (e.2, 9), (e.1 - 2, 25)], CatInk.ear)
            if k.fat {
                var tuft = Path()
                tuft.move(to: CGPoint(x: e.2, y: 12)); tuft.addLine(to: CGPoint(x: e.2 - 1, y: 6))
                tuft.move(to: CGPoint(x: e.2 + 2, y: 13)); tuft.addLine(to: CGPoint(x: e.2 + 3, y: 7))
                ear.stroke(tuft, with: .color(CatInk.white.opacity(0.8)), lineWidth: 0.9)
            }
        }
        if k.fat {
            for piece in CatPainter.blobPieces(30, 32, 22, 12, 16, 3) { g.fill(piece, with: .color(CatInk.fur)) }
        } else {
            oval(g, 30, 32, 20, 11, CatInk.fur)
        }
    }

    /// A dangling tail in three joints, in a 40×90 box, hanging from its top.
    static func tail(_ g0: GraphicsContext, _ size: CGSize, _ k: CatKind, _ mode: TailMode, _ t: Double) {
        var g = g0; g.scaleBy(x: size.width / 40, y: size.width / 40)
        var period = 1.0, amp: [Double] = [0, 0, 0]
        switch mode {
        case .still: break
        case .sway: period = 5.2; amp = [9, 18, 30]
        case .flick: period = 1.0; amp = [9, 18, 30]
        case .tip: period = 0.6; amp = [0, 0, 32]
        }
        let w: CGFloat = k.fat ? 11 : 4.5
        func seg(_ c: GraphicsContext, _ a: CGPoint, _ b: CGPoint, _ bend: CGFloat = 0) {
            let c1 = CGPoint(x: a.x + (b.x - a.x) / 3 + bend, y: a.y + (b.y - a.y) / 3)
            let c2 = CGPoint(x: a.x + 2 * (b.x - a.x) / 3 + bend, y: a.y + 2 * (b.y - a.y) / 3)
            CatPainter.furryStroke(c, [a, c1, c2, b], width: w, fluffy: k.fat)
        }
        turn(&g, wave(t, period) * amp[0], 20, 0)
        seg(g, CGPoint(x: 20, y: 0), CGPoint(x: 20, y: 31))
        turn(&g, wave(t, period, -0.1) * amp[1], 20, 30)
        seg(g, CGPoint(x: 20, y: 30), CGPoint(x: 20, y: 57))
        turn(&g, wave(t, period, -0.2) * amp[2], 20, 56)
        seg(g, CGPoint(x: 20, y: 56), CGPoint(x: 18, y: 78), 2)
    }

    /// Two front legs with white mitts, in a 40×24 box, hooked over the island's bottom edge.
    static func paws(_ g0: GraphicsContext, _ size: CGSize, _ k: CatKind, _ t: Double?) {
        var g = g0; g.scaleBy(x: size.width / 40, y: size.width / 40)
        let w: CGFloat = k.fat ? 9 : 6
        for (i, x) in [CGFloat(12), 28].enumerated() {
            var p = g
            if let t { p.translateBy(x: 0, y: -1 + 2 * wave(t, 0.9, i == 0 ? 0 : 0.5)) }  // kneading
            p.fill(Path(roundedRect: CGRect(x: x - w / 2, y: 0, width: w, height: 16), cornerRadius: w / 2), with: .color(CatInk.fur))
            oval(p, x, 17, w * 0.62, w * 0.45, CatInk.white)
        }
    }

    /// A small grey mouse, in a 30×16 box, facing right. It always gets away.
    static func mouse(_ g0: GraphicsContext, _ size: CGSize, _ t: Double) {
        var g = g0; g.scaleBy(x: size.width / 30, y: size.width / 30)
        g.translateBy(x: 0, y: -abs(wave(t, 0.24)) * 1.5)
        var tail = Path(); tail.move(to: CGPoint(x: 3, y: 11))
        tail.addCurve(to: CGPoint(x: 2, y: 6), control1: CGPoint(x: -2, y: 10), control2: CGPoint(x: -1, y: 4))
        g.stroke(tail, with: .color(Color(uiColor: UIColor(hex: 0xC58F8F))), lineWidth: 1.1)
        oval(g, 13, 10, 9, 5.2, Color(uiColor: UIColor(hex: 0x9C918B)))
        oval(g, 21, 9, 5, 4, Color(uiColor: UIColor(hex: 0xA99E98)))
        oval(g, 18, 5.4, 2.8, 2.8, Color(uiColor: UIColor(hex: 0xC9A5A5)))
        oval(g, 18, 5.4, 1.6, 1.6, Color(uiColor: UIColor(hex: 0xE8BCBC)))
        oval(g, 23, 8.2, 0.9, 0.9, .black)
        oval(g, 26, 9.6, 0.9, 0.9, Color(uiColor: UIColor(hex: 0xDD8888)))
        oval(g, 10, 15, 1.3, 1, Color(uiColor: UIColor(hex: 0xC9A5A5)))
        oval(g, 18, 15, 1.3, 1, Color(uiColor: UIColor(hex: 0xC9A5A5)))
    }

    /// A little songbird, in a 28×22 box, facing right.
    static func bird(_ g0: GraphicsContext, _ size: CGSize, _ t: Double?) {
        var g = g0; g.scaleBy(x: size.width / 28, y: size.width / 28)
        fill(g, [(3, 12), (-2, 9), (0, 14)], Color(uiColor: UIColor(hex: 0x5D6C7A)))
        oval(g, 12, 13, 9, 6.5, Color(uiColor: UIColor(hex: 0x6F8FB0)))
        oval(g, 13, 15.5, 6, 3.6, Color(uiColor: UIColor(hex: 0xF1E2C4)))
        oval(g, 20, 8, 5, 5, Color(uiColor: UIColor(hex: 0x2B2E33)))
        oval(g, 21.5, 9, 2.4, 2.4, CatInk.white)
        oval(g, 21.6, 7, 0.9, 0.9, .white)
        fill(g, [(24.6, 8), (28, 8.8), (24.6, 9.8)], Color(uiColor: UIColor(hex: 0xE0A43A)))
        var wing = g
        if let t { turn(&wing, wave(t, 0.24) * 28, 10, 10) }
        var w = Path(); w.move(to: CGPoint(x: 7, y: 11)); w.addQuadCurve(to: CGPoint(x: 17, y: 10), control: CGPoint(x: 12, y: 2)); w.closeSubpath()
        wing.fill(w, with: .color(Color(uiColor: UIColor(hex: 0x5A7798))))
        var legs = Path()
        legs.move(to: CGPoint(x: 11, y: 19.5)); legs.addLine(to: CGPoint(x: 11, y: 22))
        legs.move(to: CGPoint(x: 14, y: 19.5)); legs.addLine(to: CGPoint(x: 14, y: 22))
        g.stroke(legs, with: .color(Color(uiColor: UIColor(hex: 0x9B7A52))), lineWidth: 1)
    }

    /// Bri and Kiki rolling around in a cartoon cloud of fur.
    static func brawl(_ g0: GraphicsContext, _ size: CGSize, _ t: Double) {
        var g = g0; g.scaleBy(x: size.width / 120, y: size.width / 120)
        g.translateBy(x: wave(t, 0.18) * 2, y: wave(t, 0.14, 0.3) * 1)
        var spin = g
        turn(&spin, t * 720, 60, 42)
        let puff = Color(white: 0.86).opacity(0.9)
        for (x, y, r) in [(CGFloat(60), CGFloat(42), CGFloat(26)), (40, 36, 16), (82, 40, 17), (60, 24, 14), (52, 60, 13)] {
            oval(spin, x, y, r, r, puff)
        }
        func limb(_ a: CGPoint, _ b: CGPoint, _ w: CGFloat) {
            var p = Path(); p.move(to: a); p.addLine(to: b)
            spin.stroke(p, with: .color(CatInk.fur), style: StrokeStyle(lineWidth: w, lineCap: .round))
            oval(spin, b.x, b.y, w * 0.6, w * 0.6, CatInk.white)
        }
        limb(CGPoint(x: 30, y: 54), CGPoint(x: 14, y: 66), 6)
        limb(CGPoint(x: 88, y: 26), CGPoint(x: 104, y: 14), 5)
        limb(CGPoint(x: 70, y: 62), CGPoint(x: 78, y: 76), 6)
        CatPainter.furryStroke(spin, [CGPoint(x: 86, y: 58), CGPoint(x: 98, y: 66), CGPoint(x: 106, y: 60), CGPoint(x: 110, y: 50)], width: 8, fluffy: true)
        CatPainter.furryStroke(spin, [CGPoint(x: 34, y: 22), CGPoint(x: 24, y: 14), CGPoint(x: 18, y: 18), CGPoint(x: 12, y: 10)], width: 4, fluffy: false)
        for piece in CatPainter.blobPieces(54, 44, 10, 10, 12, 2.5) { spin.fill(piece, with: .color(CatInk.fur)) }
        oval(spin, 68, 38, 8, 8, CatInk.fur)
        for (x, y, r) in [(CGFloat(50), CGFloat(42), CGFloat(2.4)), (56, 42, 2.4), (66, 36, 2), (71, 36, 2)] { oval(spin, x, y, r, r, CatInk.eye) }
        let flash = Int(t * 6) % 2 == 0
        g.draw(Text("!").font(.system(size: 14, weight: .heavy)).foregroundColor(Theme.crit), at: CGPoint(x: flash ? 18 : 24, y: 14))
        g.draw(Text("#").font(.system(size: 11, weight: .heavy)).foregroundColor(Theme.star), at: CGPoint(x: 8, y: flash ? 44 : 40))
        g.draw(Text("*").font(.system(size: 14, weight: .heavy)).foregroundColor(Theme.star), at: CGPoint(x: flash ? 100 : 96, y: 72))
    }
}
