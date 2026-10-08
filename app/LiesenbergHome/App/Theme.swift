import SwiftUI

/// Farben & Formen aus dem Design (dunkel & minimal).
enum Theme {
    static let bg      = Color(hex: 0x0A0B0D)
    static let card    = Color(hex: 0x15171B)
    static let card2   = Color(hex: 0x1C1F24)
    static let line    = Color(hex: 0x23262C)
    static let control = Color(hex: 0x23262C)
    static let text    = Color(hex: 0xF3F4F6)
    static let muted   = Color(hex: 0xA0A6B0)
    static let faint   = Color(hex: 0x5A606A)

    static let solar   = Color(hex: 0xF6B73C)
    static let battery = Color(hex: 0x46D39A)
    static let grid    = Color(hex: 0x8AB4FF)
    static let heat    = Color(hex: 0xFF8A5B)
    static let lightOnBG = Color(hex: 0x3A2F17)

    static let radius: CGFloat = 20
}

extension Color {
    init(hex: UInt32, opacity: Double = 1) {
        self.init(.sRGB,
                  red: Double((hex >> 16) & 0xFF) / 255,
                  green: Double((hex >> 8) & 0xFF) / 255,
                  blue: Double(hex & 0xFF) / 255,
                  opacity: opacity)
    }
}

extension View {
    func card(padding: CGFloat = 14) -> some View {
        self.padding(padding)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Theme.card, in: RoundedRectangle(cornerRadius: Theme.radius, style: .continuous))
    }
}

struct SectionLabel: View {
    let text: String
    var body: some View {
        Text(text.uppercased())
            .font(.system(size: 13, weight: .semibold))
            .kerning(0.8)
            .foregroundStyle(Theme.muted)
    }
}

/// Zahlen in kW mit einer Nachkommastelle.
func kw(_ watts: Double) -> String {
    String(format: "%.1f", abs(watts) / 1000)
}
