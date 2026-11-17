import ActivityKit
import AppIntents
import SwiftUI
import WidgetKit

/// What's on every TV this person can use, on the Lock Screen and in the Dynamic Island.
/// Pause works from here; Move to opens the app on that TV.
struct NowPlayingLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: NowPlayingAttributes.self) { ctx in
            VStack(alignment: .leading, spacing: 8) {
                ForEach(ctx.state.shown.prefix(4)) { tv in
                    NowPlayingRow(tv: tv)
                }
                if ctx.state.shown.count > 4 {
                    Text("and \(ctx.state.shown.count - 4) more").font(.caption).foregroundStyle(.white.opacity(0.6))
                }
            }
            .padding(.horizontal, 4)
            .padding(.vertical, 2)
            .activityBackgroundTint(Color(red: 0.07, green: 0.1, blue: 0.13))
            .widgetURL(URL(string: "thomascreek://tvs"))
        } dynamicIsland: { ctx in
            DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    Image(systemName: "tv.fill").foregroundStyle(.white).padding(.leading, 6)
                }
                DynamicIslandExpandedRegion(.center) {
                    Text(ctx.state.title).font(.headline).lineLimit(1)
                }
                DynamicIslandExpandedRegion(.bottom) {
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(ctx.state.shown.prefix(2)) { tv in NowPlayingRow(tv: tv, compact: true) }
                    }
                }
            } compactLeading: {
                Image(systemName: "tv.fill")
            } compactTrailing: {
                let n = ctx.state.shown.count
                Text(n > 1 ? "\(n) on" : ctx.state.title).lineLimit(1).frame(maxWidth: 72)
            } minimal: {
                Image(systemName: "tv.fill")
            }
            .widgetURL(URL(string: "thomascreek://tvs"))
        }
    }
}

private struct NowPlayingRow: View {
    let tv: NowPlayingTV
    var compact = false

    var body: some View {
        HStack(spacing: 10) {
            if !compact {
                Image(systemName: "tv.fill").font(.body).foregroundStyle(.white)
                    .frame(width: 34, height: 34)
                    .background(.white.opacity(0.14), in: RoundedRectangle(cornerRadius: 9, style: .continuous))
            }
            VStack(alignment: .leading, spacing: 1) {
                Text(tv.title).font(.subheadline.weight(.semibold)).foregroundStyle(.white).lineLimit(1)
                Text(tv.name).font(.caption).foregroundStyle(.white.opacity(0.7)).lineLimit(1)
            }
            Spacer(minLength: 6)
            if !tv.id.isEmpty {
                Button(intent: PauseTVIntent(tvID: tv.id)) {
                    Image(systemName: "pause.fill").font(.footnote.weight(.bold))
                }
                .buttonStyle(.bordered).tint(.white)
                .accessibilityLabel("Pause \(tv.name)")
                Link(destination: URL(string: "thomascreek://tvs/\(tv.id)/move")!) {
                    Label("Move to", systemImage: "arrow.right.square").font(.caption.weight(.semibold))
                        .labelStyle(.titleAndIcon)
                        .padding(.horizontal, 8).padding(.vertical, 5)
                        .background(.white.opacity(0.14), in: Capsule())
                        .foregroundStyle(.white)
                }
            }
        }
    }
}
