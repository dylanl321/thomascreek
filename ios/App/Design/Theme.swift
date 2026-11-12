import SwiftUI
import UIKit

/// Colours from the approved mockups. One accent (teal); amber and red only for attention, never decoration.
enum Theme {
    static let bg = dyn(0xF2F4F3, 0x0C1214)
    static let card = dyn(0xFFFFFF, 0x161F22)
    static let card2 = dyn(0xE9EEED, 0x1F2A2E)
    static let sheet = dyn(0xF7F9F8, 0x141C1F)
    static let fg = dyn(0x111818, 0xEEF3F2)
    static let dim = dyn(0x5E6D6C, 0x8D9E9D)
    static let accent = dyn(0x0F8A7B, 0x4FC3B0)
    static let onAccent = dyn(0xFFFFFF, 0x06201C)
    static let warn = dyn(0xB86E00, 0xF5A524)
    static let crit = dyn(0xD93A2F, 0xFF5B4F)
    static let lit = dyn(0xFFFFFF, 0xEEF1EA)
    static let litFg = dyn(0x111818, 0x152020)
    static let off = dyn(0xE4E9E8, 0x1A2427)
    static let line = Color(UIColor { $0.userInterfaceStyle == .dark ? UIColor(white: 0.94, alpha: 0.08) : UIColor(white: 0.07, alpha: 0.09) })
    static let star = dyn(0xE0A800, 0xFFD84D)
    /// The 9-year-old's colour and the 5-year-old's.
    static let kid = dyn(0x4867E8, 0x6B8CFF)
    static let little = dyn(0xE2457A, 0xFF6F9F)

    static func dyn(_ light: UInt32, _ dark: UInt32) -> Color {
        Color(UIColor { $0.userInterfaceStyle == .dark ? UIColor(hex: dark) : UIColor(hex: light) })
    }

    static func level(_ l: String?) -> Color {
        switch l { case "critical", "act_now", "crit": crit; case "attention", "warn", "decision", "maintenance": warn; default: accent }
    }

    /// Rounded "friendly" display face for the children's app (the family board uses Fredoka; SF Rounded is the native match).
    static func kidFont(_ size: CGFloat, _ weight: Font.Weight = .bold) -> Font { .system(size: size, weight: weight, design: .rounded) }
}

extension UIColor {
    convenience init(hex: UInt32, alpha: CGFloat = 1) {
        self.init(red: CGFloat((hex >> 16) & 0xFF) / 255, green: CGFloat((hex >> 8) & 0xFF) / 255,
                  blue: CGFloat(hex & 0xFF) / 255, alpha: alpha)
    }
}

extension Color {
    /// "#ff8800" from the hub (light colours, people's colours).
    init?(hexString: String?) {
        guard var s = hexString?.trimmingCharacters(in: .whitespaces), !s.isEmpty else { return nil }
        if s.hasPrefix("#") { s.removeFirst() }
        guard s.count == 6, let v = UInt32(s, radix: 16) else { return nil }
        self = Color(UIColor(hex: v))
    }
}
