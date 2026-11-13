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
        .personalStyle()
        .onAppear { Appearance.apply() }
        .onChange(of: SettingsStore.shared.string(SettingKey.appearance, "system")) { Appearance.apply() }
    }

}

/// The person's appearance choice, set on the app's windows so sheets follow it too and
/// "Match iPhone" really goes back to the system setting (preferredColorScheme(nil) leaves sheets stuck).
@MainActor
enum Appearance {
    static func apply() {
        let style: UIUserInterfaceStyle = switch SettingsStore.shared.string(SettingKey.appearance, "system") {
        case "light": .light
        case "dark": .dark
        default: .unspecified
        }
        for scene in UIApplication.shared.connectedScenes {
            for w in (scene as? UIWindowScene)?.windows ?? [] { w.overrideUserInterfaceStyle = style }
        }
    }
}

/// The person's text size. Sheets don't inherit it from the screen behind them, so each sheet applies it too.
struct PersonalStyle: ViewModifier {
    func body(content: Content) -> some View { content.dynamicTypeSize(range) }
    private var range: ClosedRange<DynamicTypeSize> {
        switch SettingsStore.shared.string(SettingKey.textSize, "standard") {
        case "large": .xLarge ... .accessibility3
        case "larger": .xxxLarge ... .accessibility5
        default: .xSmall ... .accessibility5
        }
    }
}

extension View {
    func personalStyle() -> some View { modifier(PersonalStyle()) }
}

struct MainTabs: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router

    var body: some View {
        @Bindable var router = router
        VStack(spacing: 0) {
        PreviewBanner()
        TabView(selection: $router.tab) {
            switch store.role {
            case .admin, .adult, .tablet:
                Tab("Home", image: "tc.home", value: AppTab.home) { stack(.home) { if simple { SimpleHomeView() } else { HomeView() } } }
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
        .tabViewStyle(.sidebarAdaptable)
        }
        .familyWelcome()
        .kidBedtime()
        .tellDylan()
        .animation(.spring(duration: 0.3), value: store.previewing)
        .tint(store.role == .child ? (store.me?.style == .little ? Theme.little : Theme.kid) : Theme.accent)
        .overlay(alignment: .bottom) { UpdateBanner() }
        .overlay(alignment: .bottomTrailing) { AskButton() }
        .animation(.spring(duration: 0.35), value: AppUpdate.shared.showBanner)
        .animation(.spring(duration: 0.3), value: router.showAssistant)
        .sheet(isPresented: $router.showAssistant) { AssistantSheet().personalStyle() }
        .task { await AssistantModel.shared.checkReady(); openAskIfFlagged() }
        .onReceive(NotificationCenter.default.publisher(for: .openAssistant)) { _ in openAskIfFlagged() }
        .onReceive(NotificationCenter.default.publisher(for: UIApplication.didBecomeActiveNotification)) { _ in openAskIfFlagged() }
        .sheet(isPresented: $router.showInbox) { InboxView().personalStyle() }
        .sheet(isPresented: $router.showSearch) { SearchView().personalStyle() }
        .sheet(isPresented: $router.showProfile) { ProfileView().personalStyle() }
        .sheet(isPresented: $router.showEditHome) { EditHomeSheet().personalStyle() }
        .sheet(isPresented: $router.showViewAs) { ViewAsSheet().personalStyle() }
        .sheet(isPresented: $router.showOffline) {
            NavigationStack { OfflineStorageView().toolbar { Button("Done") { router.showOffline = false } } }.personalStyle()
        }
        .sheet(item: Binding(get: { router.vpnImport.map(IdentifiedURL.init) }, set: { router.vpnImport = $0?.url })) { f in VPNImportSheet(file: f.url).personalStyle() }
        .sheet(item: Binding(get: { router.youtubeAdd.map(YouTubeLink.init) }, set: { router.youtubeAdd = $0?.text })) { l in
            // A shared-in link goes straight to its preview; the + button opens the search wizard.
            Group { if l.text.isEmpty { YouTubeWizard() } else { YouTubeAddSheet(link: l.text) } }.personalStyle()
        }
        .sheet(item: Binding(get: { store.askParent }, set: { store.askParent = $0 })) { a in AskParentSheet(ask: a).personalStyle() }
    }

    private var little: Bool { store.me?.style == .little }
    private var simple: Bool { store.role != .tablet && SettingsStore.shared.bool(SettingKey.simpleMode) }

    /// The Control Center button, Action Button and Siri leave this flag: open the panel, listening.
    private func openAskIfFlagged() {
        guard AppSettings.defaults.bool(forKey: OpenAskIntent.flag) else { return }
        AppSettings.defaults.set(false, forKey: OpenAskIntent.flag)
        router.showAssistant = true
        Task { await AssistantModel.shared.startListening(screen: router.screenName) }
    }

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
        case .workshop: WorkshopView()
        case .printLibrary: PrintLibraryView()
        case .holiday: HolidayView()
        case .holidayControllers: ControllersView()
        case .house3D: House3DScreen()
        case .remote(let id): RemoteView(deviceID: id)
        case .music: MusicView()
        case .watch: WatchView()
        case .requests: RequestsView()
        case .pipeline: PipelineView()
        case .kidsIPads: KidsIPadsView()
        case .askMedia: AskMediaView()
        case .mediaTitle(let id): MediaTitleView(mediaId: id)
        case .mediaRequest(let id): MediaRequestView(requestID: id)
        case .calendar: CalendarView()
        case .meals, .dinners: DinnersView()
        case .recipes: RecipesView()
        case .kitchen(let id): KitchenView(focus: id)
        case .recipe(let id): RecipeView(recipeID: id)
        case .shopping: ShoppingView()
        case .chores: ChoresManageView()
        case .inventory: InventoryView()
        case .trips: TripsView()
        case .trip(let id): TripView(tripID: id)
        case .tripDay(let id, let date): TripDayView(tripID: id, date: date)
        case .systems: SystemsView()
        case .storage: NASView()
        case .nasApps(let f): NASAppsView(filter: f)
        case .nasApp(let n): NASAppView(name: n)
        case .nasLogs(let n, let c): NASLogsView(app: n, container: c)
        case .nasUpdates: NASUpdatesView()
        case .nasSettings: NASSettingsView()
        case .activity: ActivityView()
        case .people: PeopleView()
        case .kidNet: KidNetView()
        case .vpnPool: VPNPoolView()
        case .hubWeb: HubWebPageView()
        case .youtube: YouTubeManageView()
        case .today: TodayView()
        case .leaving: DepartureView()
        case .intercom: IntercomView()
        case .guestPasses: GuestPassesView()
        case .bedtimes: BedtimesView()
        case .feedback: FeedbackListView()
        case .weather: WeatherView()
        case .weatherAlert(let id): WeatherAlertView(alertID: id)
        case .weatherRadar: WeatherRadarView()
        case .weatherOutlook: WeatherOutlookView()
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
            // Below the View as banner, so its Exit button stays reachable.
            .padding(.top, store.previewing == nil ? 6 : 52)
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
            ToolbarItemGroup(placement: .topBarTrailing) {
                SearchButton()
                BellButton()
            }
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

