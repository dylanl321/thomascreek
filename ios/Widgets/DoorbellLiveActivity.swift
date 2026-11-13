import ActivityKit
import SwiftUI
import WidgetKit

/// "Someone's at the front door" on the Lock Screen and in the Dynamic Island, started by the hub.
struct DoorbellLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: DoorbellAttributes.self) { ctx in
            DoorbellLockScreen(ctx: ctx)
                .activityBackgroundTint(Color(red: 0.07, green: 0.1, blue: 0.13))
                .activitySystemActionForegroundColor(.white)
                .widgetURL(url(ctx.attributes))
        } dynamicIsland: { ctx in
            DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    Image(systemName: "bell.and.waves.left.and.right.fill").font(.title2).foregroundStyle(.orange)
                        .padding(.leading, 6)
                }
                DynamicIslandExpandedRegion(.trailing) {
                    Text(ctx.state.at, style: .relative).font(.caption).foregroundStyle(.secondary).monospacedDigit()
                        .frame(maxWidth: 70)
                }
                DynamicIslandExpandedRegion(.center) {
                    Text("Someone's at the \(ctx.attributes.door.lowercased())").font(.headline).lineLimit(1)
                }
                DynamicIslandExpandedRegion(.bottom) {
                    Link(destination: url(ctx.attributes)) {
                        Label("See who it is", systemImage: "video.fill").font(.subheadline.weight(.semibold))
                            .frame(maxWidth: .infinity).padding(.vertical, 8)
                            .background(.orange.opacity(0.25), in: Capsule())
                    }
                }
            } compactLeading: {
                Image(systemName: "bell.fill").foregroundStyle(.orange)
            } compactTrailing: {
                Text(ctx.state.at, style: .timer).monospacedDigit().frame(maxWidth: 40).font(.caption2)
            } minimal: {
                Image(systemName: "bell.fill").foregroundStyle(.orange)
            }
            .widgetURL(url(ctx.attributes))
            .keylineTint(.orange)
        }
    }

    private func url(_ a: DoorbellAttributes) -> URL {
        URL(string: a.camera.isEmpty ? "thomascreek://doors" : "thomascreek://camera/\(a.camera)")!
    }
}

private struct DoorbellLockScreen: View {
    let ctx: ActivityViewContext<DoorbellAttributes>
    var body: some View {
        HStack(spacing: 14) {
            ZStack {
                Circle().fill(.orange.opacity(0.2)).frame(width: 54, height: 54)
                Image(systemName: "bell.and.waves.left.and.right.fill").font(.title2).foregroundStyle(.orange)
            }
            VStack(alignment: .leading, spacing: 3) {
                Text("Someone's at the \(ctx.attributes.door.lowercased())").font(.headline).foregroundStyle(.white)
                HStack(spacing: 4) {
                    Text(ctx.attributes.cameraName)
                    Text("·")
                    Text(ctx.state.at, style: .relative)
                    Text("ago")
                }
                .font(.subheadline).foregroundStyle(.white.opacity(0.7))
            }
            Spacer()
            Image(systemName: "video.fill").foregroundStyle(.white).padding(10).background(.white.opacity(0.15), in: Circle())
        }
        .padding(16)
    }
}
