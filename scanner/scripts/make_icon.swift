// Draws the Room Scanner app icon: a little isometric dollhouse cutaway inside scanner brackets.
// Usage (macOS):  swift scripts/make_icon.swift RoomScanner/Assets.xcassets/AppIcon.appiconset/AppIcon.png
import AppKit
import CoreGraphics

let size = 1024
let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "AppIcon.png"
let space = CGColorSpace(name: CGColorSpace.sRGB)!
let ctx = CGContext(data: nil, width: size, height: size, bitsPerComponent: 8, bytesPerRow: 0, space: space,
                    bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)!
// Top-left origin.
ctx.translateBy(x: 0, y: CGFloat(size))
ctx.scaleBy(x: 1, y: -1)

func rgb(_ hex: UInt32) -> CGColor {
    CGColor(srgbRed: CGFloat((hex >> 16) & 0xFF) / 255, green: CGFloat((hex >> 8) & 0xFF) / 255,
            blue: CGFloat(hex & 0xFF) / 255, alpha: 1)
}

// Background gradient.
let gradient = CGGradient(colorsSpace: space, colors: [rgb(0x7FB8F5), rgb(0x9FE3D0)] as CFArray, locations: [0, 1])!
ctx.drawLinearGradient(gradient, start: .zero, end: CGPoint(x: size, y: size), options: [])

// Isometric projection: x runs down-right, y runs down-left, z is up.
let k: CGFloat = 330
let origin = CGPoint(x: 512, y: 370)
func iso(_ x: CGFloat, _ y: CGFloat, _ z: CGFloat) -> CGPoint {
    CGPoint(x: origin.x + (x - y) * k * 0.866, y: origin.y + (x + y) * k * 0.5 - z * k)
}
func fill(_ pts: [CGPoint], _ color: UInt32) {
    ctx.beginPath()
    ctx.addLines(between: pts)
    ctx.closePath()
    ctx.setFillColor(rgb(color))
    ctx.fillPath()
}
func box(_ x: CGFloat, _ y: CGFloat, _ z: CGFloat, _ w: CGFloat, _ d: CGFloat, _ h: CGFloat,
         top: UInt32, left: UInt32, right: UInt32) {
    fill([iso(x, y, z + h), iso(x + w, y, z + h), iso(x + w, y + d, z + h), iso(x, y + d, z + h)], top)
    fill([iso(x, y + d, z), iso(x + w, y + d, z), iso(x + w, y + d, z + h), iso(x, y + d, z + h)], left)
    fill([iso(x + w, y, z), iso(x + w, y + d, z), iso(x + w, y + d, z + h), iso(x + w, y, z + h)], right)
}

let wallH: CGFloat = 0.62
let t: CGFloat = 0.07
// Base slab.
box(-0.06, -0.06, -0.08, 1.12, 1.12, 0.08, top: 0xFFF6E8, left: 0xE9D9C2, right: 0xD9C6AC)
// Floor.
fill([iso(0, 0, 0), iso(1, 0, 0), iso(1, 1, 0), iso(0, 1, 0)], 0xD8A877)
// Rug.
fill([iso(0.35, 0.3, 0.001), iso(0.8, 0.3, 0.001), iso(0.8, 0.75, 0.001), iso(0.35, 0.75, 0.001)], 0xF2C6B4)
// Back walls (cutaway: only the two far walls stand).
box(0, 0, 0, t, 1, wallH, top: 0x5A5560, left: 0xF3EEE6, right: 0xFBF8F2)
box(0, 0, 0, 1, t, wallH, top: 0x5A5560, left: 0xE8E1D6, right: 0xE8E1D6)
// Window on the right-hand back wall.
fill([iso(0.45, t + 0.001, 0.25), iso(0.8, t + 0.001, 0.25), iso(0.8, t + 0.001, 0.5), iso(0.45, t + 0.001, 0.5)], 0xFFE79A)
// Door on the left-hand back wall.
fill([iso(t + 0.001, 0.55, 0), iso(t + 0.001, 0.8, 0), iso(t + 0.001, 0.8, 0.45), iso(t + 0.001, 0.55, 0.45)], 0x9A6B47)
// Sofa.
box(0.12, 0.2, 0, 0.18, 0.5, 0.12, top: 0xF07F6E, left: 0xD8695A, right: 0xC75D50)
box(0.12, 0.2, 0.12, 0.07, 0.5, 0.12, top: 0xF07F6E, left: 0xD8695A, right: 0xC75D50)
// Table.
box(0.5, 0.45, 0, 0.22, 0.2, 0.1, top: 0x8C6A4F, left: 0x765840, right: 0x6A4E39)
// Plant.
box(0.82, 0.12, 0, 0.08, 0.08, 0.1, top: 0xC9B8A6, left: 0xB5A390, right: 0xA59382)
ctx.setFillColor(rgb(0x4FAF6A))
let plant = iso(0.86, 0.16, 0.22)
ctx.fillEllipse(in: CGRect(x: plant.x - 44, y: plant.y - 44, width: 88, height: 88))

// Scanner corner brackets.
ctx.setStrokeColor(CGColor(srgbRed: 1, green: 1, blue: 1, alpha: 0.95))
ctx.setLineWidth(34)
ctx.setLineCap(.round)
ctx.setLineJoin(.round)
let m: CGFloat = 120, l: CGFloat = 150, s = CGFloat(size)
for (cx, cy, dx, dy) in [(m, m, 1.0, 1.0), (s - m, m, -1.0, 1.0), (m, s - m, 1.0, -1.0), (s - m, s - m, -1.0, -1.0)] as [(CGFloat, CGFloat, CGFloat, CGFloat)] {
    ctx.beginPath()
    ctx.move(to: CGPoint(x: cx, y: cy + dy * l))
    ctx.addLine(to: CGPoint(x: cx, y: cy))
    ctx.addLine(to: CGPoint(x: cx + dx * l, y: cy))
    ctx.strokePath()
}

let image = ctx.makeImage()!
let rep = NSBitmapImageRep(cgImage: image)
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: out))
print("wrote \(out)")
