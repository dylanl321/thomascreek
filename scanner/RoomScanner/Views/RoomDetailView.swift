import SwiftUI

struct RoomDetailView: View {
    @EnvironmentObject private var library: RoomLibrary
    @Environment(\.dismiss) private var dismiss
    let scan: ScanSummary

    @State private var loaded: LoadedScan?
    @State private var loadError: String?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                ZStack {
                    if let loaded {
                        DollhouseView(rooms: loaded.rooms, colors: loaded.colors?.byIdentifier ?? [:])
                    } else if let loadError {
                        Text(loadError).foregroundStyle(.secondary).padding()
                    } else {
                        ProgressView()
                    }
                }
                .frame(height: 420)
                .frame(maxWidth: .infinity)
                .background(Color(red: 0.93, green: 0.95, blue: 0.98))
                .clipShape(RoundedRectangle(cornerRadius: 20))

                if !scan.description.isEmpty {
                    Text("“\(scan.description)”").font(.title3)
                }

                HStack(spacing: 18) {
                    stat("\(scan.roomCount)", scan.roomCount == 1 ? "room" : "rooms")
                    stat("\(scan.photoCount)", "photos")
                    if let loaded {
                        stat("\(loaded.rooms.reduce(0) { $0 + $1.objects.count })", "objects")
                    }
                }

                if let zip = scan.zipURL {
                    ShareLink(item: zip) {
                        Label("Send scan (AirDrop, Files…)", systemImage: "square.and.arrow.up")
                            .font(.headline)
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 12)
                    }
                    .buttonStyle(.borderedProminent)
                    .buttonBorderShape(.capsule)
                    Text("Also saved in Files › On My iPhone › Room Scanner › Exports.")
                        .font(.footnote).foregroundStyle(.secondary)
                }

                if let entries = loaded?.colors?.rooms.flatMap(\.entries).filter({ $0.hex != nil }), !entries.isEmpty {
                    Text("Sampled colors").font(.headline)
                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 92), spacing: 10)], spacing: 10) {
                        ForEach(entries) { entry in
                            VStack(spacing: 4) {
                                RoundedRectangle(cornerRadius: 8)
                                    .fill(Color(uiColor: UIColor(hex: entry.hex ?? "") ?? .gray))
                                    .frame(height: 40)
                                Text(entry.category ?? entry.kind).font(.caption2).foregroundStyle(.secondary)
                            }
                        }
                    }
                }
            }
            .padding()
        }
        .navigationTitle(scan.name)
        .toolbar {
            ToolbarItem(placement: .destructiveAction) {
                Button(role: .destructive) {
                    library.delete(scan)
                    dismiss()
                } label: { Image(systemName: "trash") }
            }
        }
        .task {
            do { loaded = try LoadedScan.load(scan) } catch { loadError = "Couldn't open this scan: \(error.localizedDescription)" }
        }
    }

    private func stat(_ value: String, _ label: String) -> some View {
        VStack(alignment: .leading) {
            Text(value).font(.title2.bold())
            Text(label).font(.caption).foregroundStyle(.secondary)
        }
    }
}
