import SwiftUI

@main
struct ThomasCreekApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) var delegate
    @State private var store = HomeStore.shared
    @State private var router = Router.shared
    @Environment(\.scenePhase) private var phase

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(store)
                .environment(router)
                .tint(Theme.accent)
                .onChange(of: phase, initial: true) { _, p in
                    if p == .active { store.becameActive() }
                    else if p == .background { store.wentToBackground() }
                }
                .onOpenURL { router.open($0) }
        }
    }
}
