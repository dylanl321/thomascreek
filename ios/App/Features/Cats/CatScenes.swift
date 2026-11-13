import SwiftUI

// The scenes, as in the approved prototype. Each is a short script (ten seconds or so). The mouse and
// the bird always get away. Anything behind the Dynamic Island is hidden by iOS drawing the island
// over the app, so "hiding behind it" is just moving there.

extension CatDirector {
    func run(_ s: Scene) async throws {
        switch s {
        case .kikiSun: try await kikiSun()
        case .kikiHide: try await kikiHide()
        case .kikiYawn: try await kikiYawn()
        case .kikiFood: try await kikiFood()
        case .briMouse: try await briMouse()
        case .briBird: try await briBird()
        case .briHang: try await briHang()
        case .briFight: try await briFight()
        case .briDash: try await briDash()
        case .celebrate: try await celebrate()
        }
    }

    // MARK: pieces that show while a cat is behind the island

    struct Hidden {
        let ears: CatActor, tail: CatActor, paws: CatActor
        let earsIn: CGPoint, earsUp: CGPoint, earsHalf: CGPoint
        let tailIn: CGPoint, tailOut: CGPoint
        let pawsIn: CGPoint, pawsOut: CGPoint
    }

    func hide(_ k: CatKind) -> Hidden {
        let c = stage.cover
        let x = c.minX + c.width * 0.16
        let ears = stage.add(.ears(k, swivel: false), at: .zero)
        let tail = stage.add(.tail(k, .still), at: .zero)
        let paws = stage.add(.paws(k, knead: false), at: .zero)
        let eh = ears.size.height, th = tail.size.height, ph = paws.size.height
        let h = Hidden(ears: ears, tail: tail, paws: paws,
                       earsIn: CGPoint(x: x + 6, y: c.maxY - eh - 2), earsUp: CGPoint(x: x + 6, y: c.minY - eh * 0.5),
                       earsHalf: CGPoint(x: x + 6, y: c.minY - eh * 0.25),
                       tailIn: CGPoint(x: x + (k.fat ? 66 : 56), y: c.maxY - th - 2), tailOut: CGPoint(x: x + (k.fat ? 66 : 56), y: c.maxY - th * 0.38),
                       pawsIn: CGPoint(x: x + 14, y: c.maxY - ph - 1), pawsOut: CGPoint(x: x + 14, y: c.maxY - ph * 0.3))
        stage.place(ears, h.earsIn); stage.place(tail, h.tailIn); stage.place(paws, h.pawsIn)
        return h
    }

    func remove(_ h: Hidden) { stage.remove(h.ears); stage.remove(h.tail); stage.remove(h.paws) }

    /// Where a cat's peeking head lines up with the middle of the island.
    func peekY(_ k: CatKind) -> CGFloat { stage.cover.midY - k.height * 42 / 80 }

    // MARK: Kiki

    func kikiSun() async throws {
        let k = CatKind.kiki
        stage.sunbeam = true
        let c = stage.add(.cat(k, .walk, .waddle), at: CGPoint(x: offRight, y: groundTop(k)), facing: -1)
        try await stage.move(c, to: CGPoint(x: stage.screen.width * 0.42, y: groundTop(k)), 3.4)
        c.art = .cat(k, .sit, .waddle); try await pause(0.9)
        c.art = .cat(k, .yawn, .waddle); try await pause(1.1)
        c.art = .cat(k, .sleep, .waddle)
        for _ in 0..<4 { zzz(CGPoint(x: c.origin.x + 8, y: c.origin.y + 8)); try await pause(1.3) }
        stage.sunbeam = false
        try await pause(0.8)
        try await fade(c, to: 0, 0.9)
    }

    func kikiHide() async throws {
        let k = CatKind.kiki, cv = stage.cover
        let c = stage.add(.cat(k, .walk, .waddle), at: CGPoint(x: offRight, y: groundTop(k)), facing: -1)
        try await stage.move(c, to: CGPoint(x: cv.midX - k.width * 0.1, y: groundTop(k)), 2.8)
        c.art = .cat(k, .sit, .waddle); try await pause(0.7)
        c.art = .cat(k, .crouch, .waddle); try await pause(1.1)
        c.art = .cat(k, .leap, .waddle)
        try await stage.move(c, to: CGPoint(x: cv.minX + 14, y: cv.minY - 6), 0.62, arc: 24, ease: .easeOut)
        stage.remove(c)

        // Behind the island: tail swinging below, ears up, paws kneading the edge.
        let h = hide(k)
        h.tail.art = .tail(k, .flick); h.ears.art = .ears(k, swivel: true); h.paws.art = .paws(k, knead: true)
        stage.glide(h.tail, to: h.tailOut, 0.52, ease: .bounce)
        stage.glide(h.ears, to: h.earsUp, 0.38, ease: .bounce)
        try await stage.move(h.paws, to: h.pawsOut, 0.42, ease: .bounce)
        try await pause(1.9)
        h.tail.art = .tail(k, .sway)
        try await pause(0.9)

        // Something moved: ears down, paws in. Only the tail tip gives it away.
        h.ears.art = .ears(k, swivel: false); h.tail.art = .tail(k, .tip); h.paws.art = .paws(k, knead: false)
        stage.glide(h.ears, to: h.earsIn, 0.14, ease: .easeIn)
        try await stage.move(h.paws, to: h.pawsIn, 0.16, ease: .easeIn)
        try await pause(1.7)

        // A careful peek out of the left end.
        let pk = stage.add(.cat(k, .peek, .waddle), at: CGPoint(x: cv.minX + 6, y: peekY(k)), facing: -1)
        try await stage.move(pk, to: CGPoint(x: cv.minX - k.width * 0.4, y: peekY(k)), 0.7, ease: .easeOut)
        try await pause(2.2)
        try await stage.move(pk, to: CGPoint(x: cv.minX + 6, y: peekY(k)), 0.45, ease: .easeIn)
        stage.remove(pk)

        // All clear: dozes off.
        h.tail.art = .tail(k, .sway); h.paws.art = .paws(k, knead: true)
        stage.glide(h.ears, to: h.earsHalf, 0.6, ease: .easeOut)
        try await stage.move(h.paws, to: h.pawsOut, 0.6, ease: .easeOut)
        for _ in 0..<3 { zzz(CGPoint(x: cv.maxX - 8, y: cv.minY + 2)); try await pause(1.3) }
        stage.glide(h.ears, to: h.earsIn, 0.5); stage.glide(h.paws, to: h.pawsIn, 0.5)
        try await stage.move(h.tail, to: h.tailIn, 0.7, ease: .easeIn)
        remove(h)
    }

    func kikiYawn() async throws {
        let k = CatKind.kiki, cv = stage.cover, y = peekY(k)
        let h = hide(k); stage.remove(h.ears); stage.remove(h.paws)
        h.tail.art = .tail(k, .sway)
        stage.glide(h.tail, to: h.tailOut, 0.8, ease: .easeOut)
        let c = stage.add(.cat(k, .peek, .waddle), at: CGPoint(x: cv.maxX - k.width - 4, y: y))
        try await stage.move(c, to: CGPoint(x: cv.maxX - k.width * 0.55, y: y), 1.4, ease: .easeOut)
        try await pause(0.5)
        c.art = .cat(k, .peekYawn, .waddle); try await pause(1.3)
        c.art = .cat(k, .peek, .waddle); try await pause(0.8)
        stage.glide(h.tail, to: h.tailIn, 1.2, ease: .easeIn)
        try await stage.move(c, to: CGPoint(x: cv.maxX - k.width - 4, y: y), 1.6, ease: .easeIn)
    }

    func kikiFood() async throws {
        let k = CatKind.kiki
        let c = stage.add(.cat(k, .walk, .waddle), at: CGPoint(x: offLeft(k.width), y: groundTop(k)))
        try await stage.move(c, to: CGPoint(x: stage.screen.width * 0.3, y: groundTop(k)), 2.6)
        c.art = .cat(k, .sit, .waddle); try await pause(0.5)
        stage.effect(.bubble("Mrrp?"), at: CGPoint(x: c.origin.x + 54, y: c.origin.y - 14)); try await pause(1.3)
        c.art = .cat(k, .yawn, .waddle)
        stage.effect(.bubble("MEOW."), at: CGPoint(x: c.origin.x + 54, y: c.origin.y - 14)); try await pause(1.3)
        c.art = .cat(k, .sit, .waddle); try await pause(0.8)
        // Nobody answers. A dramatic flop, right where you'd have to step over.
        c.art = .cat(k, .sleep, .waddle)
        for _ in 0..<3 { zzz(CGPoint(x: c.origin.x + 8, y: c.origin.y + 8)); try await pause(1.3) }
        try await fade(c, to: 0, 0.8)
    }

    // MARK: Bri

    func briMouse() async throws {
        let k = CatKind.bri, cv = stage.cover, L = stage.ledge, W = stage.screen.width
        let m = stage.add(.mouse, at: CGPoint(x: -34, y: L - 15)); m.tappable = false
        let c = stage.add(.cat(k, .walk, .trot), at: CGPoint(x: offLeft(k.width), y: groundTop(k)))
        stage.glide(m, to: CGPoint(x: W * 0.6, y: L - 15), 1.5)
        try await pause(0.5)
        try await stage.move(c, to: CGPoint(x: W * 0.1, y: groundTop(k)), 1.5)
        c.art = .cat(k, .crouch, .trot); try await pause(1.5)

        // The mouse bolts up and hides behind the island. Bri pounces where it was.
        c.art = .cat(k, .leap, .trot)
        stage.glide(m, to: CGPoint(x: cv.midX - 15, y: cv.minY + 10), 0.52, ease: .easeIn)
        try await stage.move(c, to: CGPoint(x: W * 0.46, y: groundTop(k)), 0.52, arc: 40)
        c.art = .cat(k, .sit, .trot); try await pause(0.7)

        // Up to the island: hangs off the edge, kicking.
        c.art = .cat(k, .hang, .trot)
        try await stage.move(c, to: CGPoint(x: cv.minX + 20, y: cv.maxY - 8), 0.56, arc: 30, ease: .easeOut)
        try await pause(1.3)

        // Scrambles up behind it: ears swivelling above, tail lashing below.
        c.art = .cat(k, .sit, .trot)
        try await stage.move(c, to: CGPoint(x: cv.minX + 20, y: cv.minY - 4), 0.26, ease: .easeOut)
        c.opacity = 0
        let h = hide(k); stage.remove(h.paws)
        h.ears.art = .ears(k, swivel: true); h.tail.art = .tail(k, .flick)
        stage.glide(h.ears, to: h.earsUp, 0.3, ease: .bounce)
        try await stage.move(h.tail, to: h.tailOut, 0.4, ease: .bounce)
        try await pause(1.5)

        // Too slow: the mouse slips out the other end and gets away.
        h.ears.art = .ears(k, swivel: false)
        stage.place(m, CGPoint(x: cv.maxX - 24, y: cv.maxY - 6))
        try await stage.move(m, to: CGPoint(x: W + 10, y: L + 120), 0.9, ease: .easeIn)
        stage.remove(m)
        try await pause(0.3)
        stage.glide(h.ears, to: h.earsIn, 0.2)
        try await stage.move(h.tail, to: h.tailIn, 0.25)
        remove(h)
        c.opacity = 1; c.art = .cat(k, .leap, .trot); c.facing = -1
        try await stage.move(c, to: CGPoint(x: W * 0.06, y: groundTop(k)), 0.7, arc: 10)
        c.art = .cat(k, .walk, .trot)
        try await stage.move(c, to: CGPoint(x: offLeft(k.width), y: groundTop(k)), 0.9)
    }

    func briBird() async throws {
        let k = CatKind.bri, cv = stage.cover, L = stage.ledge, W = stage.screen.width
        let b = stage.add(.bird(flapping: true), at: CGPoint(x: W + 10, y: L - 80), facing: -1); b.tappable = false
        try await stage.move(b, to: CGPoint(x: W * 0.7, y: L - 21), 1.1, arc: 20, ease: .easeOut)
        b.art = .bird(flapping: false)

        // Bri peeks out of the island's left end, tail tip twitching below it.
        let c = stage.add(.cat(k, .peek, .trot), at: CGPoint(x: cv.minX + 2, y: peekY(k)), facing: -1)
        let h = hide(k); stage.remove(h.paws); stage.remove(h.ears)
        h.tail.art = .tail(k, .tip)
        stage.glide(h.tail, to: h.tailOut, 0.5, ease: .bounce)
        try await stage.move(c, to: CGPoint(x: cv.minX - k.width * 0.42, y: peekY(k)), 0.9, ease: .easeOut)
        try await pause(1.6)

        // Drops down, crouches and wiggles...
        stage.remove(h.ears); stage.remove(h.tail)
        c.art = .cat(k, .leap, .trot); c.facing = 1
        try await stage.move(c, to: CGPoint(x: W * 0.18, y: groundTop(k)), 0.52, arc: 6, ease: .easeIn)
        c.art = .cat(k, .crouch, .trot); try await pause(1.3)

        // ...and pounces. The bird is long gone.
        c.art = .cat(k, .leap, .trot)
        b.art = .bird(flapping: true); b.facing = 1
        stage.glide(b, to: CGPoint(x: W + 30, y: -30), 0.7, ease: .easeIn)
        try await stage.move(c, to: CGPoint(x: W * 0.56, y: groundTop(k)), 0.52, arc: 36)
        c.art = .cat(k, .sit, .trot); try await pause(1.4)
        stage.remove(b)
        c.art = .cat(k, .walk, .trot)
        try await stage.move(c, to: CGPoint(x: offRight, y: groundTop(k)), 1.2)
    }

    func briHang() async throws {
        let k = CatKind.bri, cv = stage.cover
        let c = stage.add(.cat(k, .walk, .trot), at: CGPoint(x: offRight, y: groundTop(k)), facing: -1)
        try await stage.move(c, to: CGPoint(x: cv.midX - 20, y: groundTop(k)), 1.3)
        c.art = .cat(k, .sit, .trot); try await pause(0.7)
        c.art = .cat(k, .crouch, .trot); try await pause(0.7)
        c.art = .cat(k, .hang, .trot)
        try await stage.move(c, to: CGPoint(x: cv.minX + 18, y: cv.maxY - 8), 0.6, arc: 20, ease: .easeOut)
        try await pause(1.8)
        c.art = .cat(k, .sit, .trot)
        try await stage.move(c, to: CGPoint(x: cv.minX + 18, y: groundTop(k)), 0.5, ease: .easeIn)
        try await pause(0.9)
        c.art = .cat(k, .walk, .trot); c.facing = 1
        try await stage.move(c, to: CGPoint(x: offRight, y: groundTop(k)), 1.1)
    }

    /// Bri gets bored and picks a fight with a napping Kiki.
    func briFight() async throws {
        let W = stage.screen.width
        let ki = stage.add(.cat(.kiki, .sleep, .waddle), at: CGPoint(x: W * 0.52, y: groundTop(.kiki)), facing: -1)
        zzz(CGPoint(x: ki.origin.x + 4, y: ki.origin.y + 6))
        let br = stage.add(.cat(.bri, .walk, .trot), at: CGPoint(x: offLeft(CatKind.bri.width), y: groundTop(.bri)))
        try await stage.move(br, to: CGPoint(x: W * 0.06, y: groundTop(.bri)), 1.3)
        br.art = .cat(.bri, .sit, .trot); try await pause(0.7)
        zzz(CGPoint(x: ki.origin.x + 4, y: ki.origin.y + 6))
        br.art = .cat(.bri, .crouch, .trot); try await pause(1.4)
        br.art = .cat(.bri, .leap, .trot)
        try await stage.move(br, to: CGPoint(x: W * 0.36, y: groundTop(.bri)), 0.42, arc: 30)

        // Fur flies.
        br.opacity = 0; ki.opacity = 0
        let fx = stage.add(.brawl, at: CGPoint(x: W * 0.34, y: groundTop(.kiki) - 14)); fx.tappable = false
        UIImpactFeedbackGenerator(style: .rigid).impactOccurred()
        try await pause(1.7)
        stage.remove(fx)

        // Kiki sits up, ears flat, unimpressed. Bri bolts.
        ki.opacity = 1; br.opacity = 1
        ki.art = .cat(.kiki, .grump, .waddle); stage.place(ki, CGPoint(x: W * 0.6, y: groundTop(.kiki)))
        br.art = .cat(.bri, .walk, .dash); br.facing = -1; stage.place(br, CGPoint(x: W * 0.36, y: groundTop(.bri)))
        try await stage.move(br, to: CGPoint(x: offLeft(CatKind.bri.width), y: groundTop(.bri)), 0.55)
        stage.remove(br)
        try await pause(1.5)
        ki.art = .cat(.kiki, .sleep, .waddle)
        for _ in 0..<2 { zzz(CGPoint(x: ki.origin.x + 4, y: ki.origin.y + 6)); try await pause(1.3) }
        try await fade(ki, to: 0, 0.8)
    }

    /// Long-press on the Home header: Bri sprints across.
    func briDash() async throws {
        let k = CatKind.bri
        let c = stage.add(.cat(k, .walk, .dash), at: CGPoint(x: offLeft(k.width), y: groundTop(k)))
        try await stage.move(c, to: CGPoint(x: offRight, y: groundTop(k)), 1.1)
    }

    /// The kids finished every chore: both cats come out and hop.
    func celebrate() async throws {
        let W = stage.screen.width
        let ki = stage.add(.cat(.kiki, .walk, .waddle), at: CGPoint(x: offRight, y: groundTop(.kiki)), facing: -1)
        let br = stage.add(.cat(.bri, .walk, .trot), at: CGPoint(x: offLeft(CatKind.bri.width), y: groundTop(.bri)))
        stage.glide(ki, to: CGPoint(x: W * 0.55, y: groundTop(.kiki)), 1.6)
        try await stage.move(br, to: CGPoint(x: W * 0.15, y: groundTop(.bri)), 1.3)
        try await pause(0.3)
        ki.art = .cat(.kiki, .sit, .waddle); br.art = .cat(.bri, .sit, .trot)
        for i in 0..<3 {
            br.art = .cat(.bri, .leap, .trot)
            if i > 0 { ki.art = .cat(.kiki, .leap, .waddle) }
            stage.glide(ki, to: ki.origin, 0.5, arc: i > 0 ? 14 : 0)
            try await stage.move(br, to: br.origin, 0.45, arc: 26)
            br.art = .cat(.bri, .sit, .trot); ki.art = .cat(.kiki, .sit, .waddle)
            stage.effect(.heart, at: CGPoint(x: br.origin.x + 40, y: br.origin.y))
            stage.effect(.heart, at: CGPoint(x: ki.origin.x + 40, y: ki.origin.y))
            try await pause(0.25)
        }
        stage.effect(.bubble("Mrrp!"), at: CGPoint(x: ki.origin.x + 50, y: ki.origin.y - 14))
        try await pause(1.2)
        ki.art = .cat(.kiki, .walk, .waddle); br.art = .cat(.bri, .walk, .trot); br.facing = -1; ki.facing = 1
        stage.glide(ki, to: CGPoint(x: offRight, y: groundTop(.kiki)), 1.8)
        try await stage.move(br, to: CGPoint(x: offLeft(CatKind.bri.width), y: groundTop(.bri)), 1.1)
        try await pause(0.7)
    }
}
