import SwiftUI

/// Sources that have been played, worst first. Nothing here is a raw error from the provider.
struct ChannelHealthPage: Decodable { var sources: [ChannelHealthRow] }
struct ChannelHealthRow: Decodable, Identifiable {
    var channelKey: String
    var name: String
    var source: String
    var score: Double
    var lastOk: String?
    var `default`: Bool?
    var id: String { channelKey + source }
}

struct ChannelHealthList: View {
    @State private var rows: [ChannelHealthRow]?
    @State private var quiet = false

    var body: some View {
        Group {
            if let rows {
                if rows.isEmpty {
                    EmptyCard(symbol: "antenna.radiowaves.left.and.right", title: "No plays yet",
                              detail: "As people watch, the sources that struggle show up here. Nothing is checked until then.")
                        .padding(16)
                } else {
                    List(rows) { row in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(row.name).font(.body.weight(.semibold))
                            Text(line(row)).font(.caption).foregroundStyle(Theme.dim)
                        }
                        .accessibilityLabel("\(row.name), \(words(row.score))")
                    }
                }
            } else if quiet {
                EmptyCard(symbol: "antenna.radiowaves.left.and.right", title: "Health will wait",
                          detail: "The list shows up the next time the hub answers.")
                    .padding(16)
            } else {
                LoadingCard().padding(16)
            }
        }
        .navigationTitle("Channel health")
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
    }

    private func line(_ row: ChannelHealthRow) -> String {
        var text = "\(row.source) · \(words(row.score))"
        if row.default == false { text += " · left off the default" }
        return text
    }

    private func words(_ score: Double) -> String {
        if score >= 0.8 { return "Usually works" }
        if score >= 0.4 { return "Unsteady" }
        return "Not answering"
    }

    private func load() async {
        guard let page: ChannelHealthPage = try? await HubClient.shared.get("/v1/tv/health") else {
            quiet = true
            rows = []
            return
        }
        rows = page.sources
    }
}
