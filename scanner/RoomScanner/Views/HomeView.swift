import RoomPlan
import SwiftUI

struct HomeView: View {
    @EnvironmentObject private var library: RoomLibrary
    @State private var scanning = false
    @State private var path: [ScanSummary] = []

    private var lidarAvailable: Bool { RoomCaptureSession.isSupported }

    var body: some View {
        NavigationStack(path: $path) {
            Group {
                if library.scans.isEmpty {
                    ContentUnavailableView("No rooms yet", systemImage: "house",
                                           description: Text("Tap Scan a room, say which room it is, and walk around it."))
                } else {
                    List {
                        ForEach(library.scans) { scan in
                            NavigationLink(value: scan) { RoomRow(scan: scan) }
                        }
                        .onDelete { offsets in
                            for i in offsets { library.delete(library.scans[i]) }
                        }
                    }
                    .listStyle(.plain)
                }
            }
            .navigationTitle("Room Scanner")
            .navigationDestination(for: ScanSummary.self) { RoomDetailView(scan: $0) }
            .safeAreaInset(edge: .bottom) {
                VStack(spacing: 8) {
                    Button {
                        scanning = true
                    } label: {
                        Label("Scan a room", systemImage: "viewfinder")
                            .font(.title3.weight(.semibold))
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 14)
                    }
                    .buttonStyle(.borderedProminent)
                    .buttonBorderShape(.capsule)
                    .disabled(!lidarAvailable)
                    if !lidarAvailable {
                        Text("Scanning needs an iPhone or iPad with LiDAR.")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
                }
                .padding(.horizontal, 20)
                .padding(.vertical, 12)
                .background(.bar)
            }
            .fullScreenCover(isPresented: $scanning) {
                ScanFlowView { summary in
                    scanning = false
                    library.reload()
                    if let summary { path = [summary] }
                }
                .environmentObject(library)
            }
            .onAppear { library.reload() }
        }
    }
}

struct RoomRow: View {
    let scan: ScanSummary

    var body: some View {
        HStack(spacing: 14) {
            Group {
                if let url = scan.thumbnailURL, let image = UIImage(contentsOfFile: url.path) {
                    Image(uiImage: image).resizable().scaledToFill()
                } else {
                    Image(systemName: "house").font(.title2).foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                        .background(Color(.secondarySystemBackground))
                }
            }
            .frame(width: 88, height: 66)
            .clipShape(RoundedRectangle(cornerRadius: 10))

            VStack(alignment: .leading, spacing: 3) {
                Text(scan.name).font(.headline)
                Text(scan.createdAt, format: .dateTime.month().day().hour().minute())
                    .font(.subheadline).foregroundStyle(.secondary)
                if scan.roomCount > 1 {
                    Text("\(scan.roomCount) rooms").font(.caption).foregroundStyle(.secondary)
                }
            }
        }
        .padding(.vertical, 4)
    }
}
