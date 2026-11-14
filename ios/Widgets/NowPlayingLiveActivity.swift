import ActivityKit
import SwiftUI
import WidgetKit

/// What's on, on the Lock Screen and in the Dynamic Island.
struct NowPlayingLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: NowPlayingAttributes.self) { ctx in
            HStack(spacing: 14) {
                Image(systemName: "tv.fill").font(.title2).foregroundStyle(.white)
                    .frame(width: 44, height: 44)
                    .background(.white.opacity(0.14), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                VStack(alignment: .leading, spacing: 2) {
                    Text(ctx.state.title).font(.headline).foregroundStyle(.white).lineLimit(1)
                    Text(ctx.state.room).font(.subheadline).foregroundStyle(.white.opacity(0.7)).lineLimit(1)
                }
                Spacer()
            }
            .padding(.horizontal, 4)
            .activityBackgroundTint(Color(red: 0.07, green: 0.1, blue: 0.13))
            .widgetURL(URL(string: "thomascreek://"))
        } dynamicIsland: { ctx in
            DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    Image(systemName: "tv.fill").foregroundStyle(.white).padding(.leading, 6)
                }
                DynamicIslandExpandedRegion(.center) {
                    Text(ctx.state.title).font(.headline).lineLimit(1)
                }
                DynamicIslandExpandedRegion(.bottom) {
                    Text(ctx.state.room).font(.subheadline).foregroundStyle(.secondary).lineLimit(1)
                }
            } compactLeading: {
                Image(systemName: "tv.fill")
            } compactTrailing: {
                Text(ctx.state.title).lineLimit(1).frame(maxWidth: 72)
            } minimal: {
                Image(systemName: "tv.fill")
            }
            .widgetURL(URL(string: "thomascreek://"))
        }
    }
}
