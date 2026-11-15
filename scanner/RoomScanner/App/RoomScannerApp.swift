import SwiftUI

@main
struct RoomScannerApp: App {
    @StateObject private var library = RoomLibrary()

    var body: some Scene {
        WindowGroup {
            HomeView()
                .environmentObject(library)
        }
    }
}
