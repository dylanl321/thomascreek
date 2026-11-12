import SwiftUI

struct RootView: View {
    @Environment(HomeStore.self) private var store

    var body: some View {
        Group {
            switch store.phase {
            case .launching: LaunchView()
            case .findingHome, .enrolling: JoinHomeView()
            case .pending: PendingView()
            case .failed(let m): JoinHomeView(error: m)
            case .ready: MainTabs()
            }
        }
        .overlay {
            if AppUpdate.shared.required { UpdateRequiredView().transition(.opacity) }
        }
        .overlay(alignment: .top) { ToastView() }
        .animation(.spring(duration: 0.35), value: store.toast)
    }
}

struct MainTabs: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router

    var body: some View {
        @Bindable var router = router
        TabView(selection: $router.tab) {
            switch store.role {
            case .admin, .adult, .tablet:
                Tab("Home", image: "tc.home", value: AppTab.home) { stack(.home) { HomeView() } }
                Tab("House", systemImage: "square.grid.2x2.fill", value: AppTab.house) { stack(.house) { HouseView() } }
                Tab("Media", systemImage: "play.tv.fill", value: AppTab.media) { stack(.media) { MediaView() } }
                Tab("Family", systemImage: "person.2.fill", value: AppTab.family) { stack(.family) { FamilyView() } }
                if store.me?.can(.systems) == true {
                    Tab("Systems", systemImage: "server.rack", value: AppTab.systems) { stack(.systems) { SystemsView() } }
                        .badge(store.home?.systems?.apps?.crashed ?? 0)
                }
            case .child:
                Tab("Home", image: "tc.home", value: AppTab.home) { stack(.home) { KidHomeView() } }
                Tab("My Room", image: "tc.window", value: AppTab.myRoom) { stack(.myRoom) { MyRoomView() } }
                Tab(little ? "Shows" : "Watch", systemImage: "tv.fill", value: AppTab.watch) { stack(.watch) { KidWatchView() } }
                Tab(little ? "Jobs" : "Chores", systemImage: "star.fill", value: AppTab.chores) { stack(.chores) { KidChoresView() } }
            case .guest:
                Tab("Welcome", image: "tc.home", value: AppTab.home) { stack(.home) { GuestHomeView() } }
                Tab("My Space", systemImage: "lightbulb.fill", value: AppTab.space) { stack(.space) { MySpaceView() } }
            case .pending:
                Tab("Home", image: "tc.home", value: AppTab.home) { PendingView() }
            }
        }
        .tint(store.role == .child ? (store.me?.style == .little ? Theme.little : Theme.kid) : Theme.accent)
        .overlay(alignment: .bottom) { UpdateBanner() }
        .animation(.spring(duration: 0.35), value: AppUpdate.shared.showBanner)
        .sheet(isPresented: $router.showInbox) { InboxView() }
        .sheet(isPresented: $router.showProfile) { ProfileView() }
        .sheet(isPresented: $router.showOffline) {
            NavigationStack { OfflineStorageView().toolbar { Button("Done") { router.showOffline = false } } }
        }
        .sheet(item: Binding(get: { router.vpnImport.map(IdentifiedURL.init) }, set: { router.vpnImport = $0?.url })) { f in VPNImportSheet(file: f.url) }
        .sheet(item: Binding(get: { router.youtubeAdd.map(YouTubeLink.init) }, set: { router.youtubeAdd = $0?.text })) { YouTubeAddSheet(link: $0.text) }
        .sheet(item: Binding(get: { store.askParent }, set: { store.askParent = $0 })) { a in AskParentSheet(ask: a) }
    }

    private var little: Bool { store.me?.style == .little }

    private func stack<V: View>(_ tab: AppTab, @ViewBuilder _ root: () -> V) -> some View {
        NavigationStack(path: router.path(tab)) {
            root()
                .offlineStrip()
                .navigationDestination(for: Destination.self) { DestinationView(destination: $0).offlineStrip() }
        }
    }
}

struct DestinationView: View {
    let destination: Destination
    var body: some View {
        switch destination {
        case .room(let id): RoomDetailView(roomID: id)
        case .climate: ClimateView()
        case .doors: DoorsView()
        case .cameras: CamerasView()
        case .camera(let id): CameraView(cameraID: id)
        case .cameraEvent(let id): CameraEventView(eventID: id)
        case .appliances: DeviceListView(title: "Appliances", kinds: [.oven, .vacuum, .sensor, .shower])
        case .outdoor: DeviceListView(title: "Outdoor", kinds: [.valve])
        case .workshop: DeviceListView(title: "Workshop", kinds: [.printer])
        case .holiday: HolidayView()
        case .holidayControllers: ControllersView()
        case .house3D: House3DScreen()
        case .remote(let id): RemoteView(deviceID: id)
        case .music: MusicView()
        case .watch: WatchView()
        case .requests: RequestsView()
        case .pipeline: PipelineView()
        case .calendar: CalendarView()
        case .meals: MealsView()
        case .recipe(let id): RecipeView(recipeID: id)
        case .shopping: ShoppingView()
        case .chores: ChoresManageView()
        case .inventory: InventoryView()
        case .trips: TripsView()
        case .trip(let id): TripView(tripID: id)
        case .tripDay(let id, let date): TripDayView(tripID: id, date: date)
        case .systems: SystemsView()
        case .storage: StorageView()
        case .activity: ActivityView()
        case .people: PeopleView()
        case .kidNet: KidNetView()
        case .vpnPool: VPNPoolView()
        case .hubWeb: HubWebPageView()
        case .youtube: YouTubeManageView()
        }
    }
}

/// Top-of-screen confirmation, e.g. "Kitchen lights off" or "Garage didn't respond".
struct ToastView: View {
    @Environment(HomeStore.self) private var store
    var body: some View {
        if let t = store.toast {
            HStack(spacing: 10) {
                Image(systemName: t.isError ? "exclamationmark.triangle.fill" : "checkmark.circle.fill")
                    .foregroundStyle(t.isError ? Theme.warn : Theme.accent)
                VStack(alignment: .leading, spacing: 1) {
                    Text(t.title).font(.subheadline.weight(.semibold))
                    if let d = t.detail { Text(d).font(.caption).foregroundStyle(Theme.dim) }
                }
            }
            .padding(.horizontal, 16).padding(.vertical, 10)
            .background(.ultraThinMaterial, in: Capsule())
            .padding(.top, 6)
            .transition(.move(edge: .top).combined(with: .opacity))
            .onTapGesture { store.toast = nil }
        }
    }
}

/// Common top bar: avatar (profile) on the left, bell (inbox) on the right.
struct TopBar: ViewModifier {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    func body(content: Content) -> some View {
        content.toolbar {
            ToolbarItem(placement: .topBarLeading) {
                Button { router.showProfile = true } label: {
                    Avatar(name: store.me?.person.name ?? "?", color: Color(hexString: store.me?.person.color), size: 32)
                }
                .accessibilityLabel("Profile")
            }
            ToolbarItem(placement: .topBarTrailing) { BellButton() }
        }
    }
}

struct BellButton: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    var body: some View {
        Button { router.showInbox = true } label: {
            Image(systemName: store.inboxCount > 0 ? "bell.badge.fill" : "bell")
                .symbolRenderingMode(.palette)
                .foregroundStyle(store.inboxCount > 0 ? Theme.warn : Theme.fg, Theme.fg)
        }
        .accessibilityLabel(store.inboxCount > 0 ? "Inbox, \(store.inboxCount) waiting" : "Inbox")
    }
}

extension View {
    func topBar() -> some View { modifier(TopBar()) }
}

