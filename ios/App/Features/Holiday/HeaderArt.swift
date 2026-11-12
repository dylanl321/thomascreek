import SwiftUI

/// Recraft header art with a title over the empty sky.
struct HeaderArt: View {
    let image: String, title: String, subtitle: String?
    var body: some View {
        ZStack(alignment: .topLeading) {
            Image(image).resizable().scaledToFill().frame(height: 210).clipped()
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.system(.largeTitle, design: .rounded, weight: .bold))
                if let subtitle { Text(subtitle).font(.subheadline).foregroundStyle(.white.opacity(0.75)) }
            }
            .foregroundStyle(.white).padding(18)
        }
        .clipShape(RoundedRectangle(cornerRadius: 22))
        .padding(.top, 8)
    }
}
