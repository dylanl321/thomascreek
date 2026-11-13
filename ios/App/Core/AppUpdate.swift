import StoreKit
import SwiftUI
import UIKit

/// What the hub knows about newer builds (GET /v1/app/update).
struct AppUpdateInfo: Codable, Equatable {
    var latestBuild: Int?
    var latestVersion: String?
    var minBuild: Int?
    var notes: String?
    var testflightUrl: String?
}

/// Newer TestFlight builds: an Update button when one is out, a full-screen stop below the required build.
/// iOS apps can't install themselves; TestFlight does that (on its own when Automatic Updates is on).
@MainActor
@Observable
final class AppUpdate {
    static let shared = AppUpdate()

    static let build = Int(Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "") ?? 0
    static let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? ""
    static var current: String { "\(version) (\(build))" }
    /// TestFlight in the App Store, for a device that doesn't have it.
    private static let testFlightStore = URL(string: "https://apps.apple.com/app/testflight/id899247664")!

    var info: AppUpdateInfo? = AppUpdate.cached
    var checking = false
    private var lastCheck: Date?
    private var dismissed: Int = AppSettings.defaults.integer(forKey: "updateDismissedBuild")

    var latestBuild: Int { info?.latestBuild ?? 0 }
    var available: Bool { latestBuild > Self.build }
    var required: Bool { (info?.minBuild ?? 0) > Self.build }
    var showBanner: Bool { available && !required && dismissed < latestBuild }
    var latestLabel: String {
        guard let b = info?.latestBuild else { return "" }
        if let v = info?.latestVersion, !v.isEmpty { return "\(v) (\(b))" }
        return "build \(b)"
    }

    /// Ask the hub. At most every five minutes unless forced (the hub pushes an `app_update` event on changes).
    func check(force: Bool = false) async {
        if !force, let lastCheck, Date().timeIntervalSince(lastCheck) < 300 { return }
        guard !checking else { return }
        checking = true
        defer { checking = false }
        do {
            var query = ["build": "\(Self.build)", "version": Self.version]
            // The hub learns new builds from TestFlight phones, so it needs to know this isn't a Simulator or Xcode build.
            query["channel"] = await Self.channel() ?? "unknown"
            let i: AppUpdateInfo = try await HubClient.shared.call(
                "GET", "/v1/app/update", query: query,
                auth: AppSettings.deviceID != nil)
            lastCheck = .now
            info = i
            Self.cached = i
        } catch {
            // Keep the last answer: a required update stays required while the hub is out of reach.
        }
    }

    /// How this copy was installed: testflight, appstore, xcode or simulator. Nil when StoreKit can't say yet (sent as unknown, which the hub doesn't learn from).
    private static var installChannel: String?
    static func channel() async -> String? {
        #if targetEnvironment(simulator)
        return "simulator"
        #else
        if let installChannel { return installChannel }
        guard let result = try? await AppTransaction.shared else { return nil }
        let c: String = switch result.unsafePayloadValue.environment {
        case .production: "appstore"
        case .sandbox: "testflight"
        case .xcode: "xcode"
        default: "other"
        }
        installChannel = c
        return c
        #endif
    }

    func dismissBanner() {
        dismissed = latestBuild
        AppSettings.defaults.set(dismissed, forKey: "updateDismissedBuild")
    }

    /// Open this app's page in TestFlight (its Update button), or TestFlight's App Store page if it's missing.
    func openTestFlight() {
        let url = info?.testflightUrl.flatMap(URL.init(string:)) ?? URL(string: "itms-beta://")!
        // Without TestFlight there is nothing to update with: send them to get it.
        guard UIApplication.shared.canOpenURL(URL(string: "itms-beta://")!) else {
            UIApplication.shared.open(Self.testFlightStore)
            return
        }
        UIApplication.shared.open(url) { ok in
            guard !ok else { return }
            UIApplication.shared.open(URL(string: "itms-beta://")!) { ok2 in
                if !ok2 { UIApplication.shared.open(Self.testFlightStore) }
            }
        }
    }

    private static var cached: AppUpdateInfo? {
        get { AppSettings.defaults.data(forKey: "appUpdate").flatMap { try? JSONDecoder().decode(AppUpdateInfo.self, from: $0) } }
        set { AppSettings.defaults.set(newValue.flatMap { try? JSONEncoder().encode($0) }, forKey: "appUpdate") }
    }
}

/// A small card above the tab bar: "Version 2.0 (17) is ready  [Update]  ✕".
struct UpdateBanner: View {
    @Environment(\.horizontalSizeClass) private var size
    var update = AppUpdate.shared

    var body: some View {
        if update.showBanner {
            HStack(spacing: 12) {
                Image(systemName: "arrow.down.app.fill").font(.title2).foregroundStyle(Theme.accent)
                VStack(alignment: .leading, spacing: 1) {
                    Text("New version ready").font(.subheadline.weight(.semibold))
                    Text("\(update.latestLabel) · you have \(AppUpdate.build)").font(.caption).foregroundStyle(Theme.dim)
                }
                Spacer(minLength: 4)
                Button("Update") { update.openTestFlight() }
                    .buttonStyle(.borderedProminent).buttonBorderShape(.capsule).tint(Theme.accent).font(.subheadline.weight(.semibold))
                Button { withAnimation { update.dismissBanner() } } label: {
                    Image(systemName: "xmark").font(.footnote.weight(.bold)).foregroundStyle(Theme.dim).padding(6)
                }
                .accessibilityLabel("Not now")
            }
            .padding(.leading, 14).padding(.trailing, 8).padding(.vertical, 10)
            .background(Theme.card, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous).stroke(Theme.line))
            .shadow(color: .black.opacity(0.12), radius: 12, y: 4)
            .frame(maxWidth: 520)
            .padding(.horizontal, 12)
            // Above the tab bar on iPhone; the iPad tab bar sits at the top.
            .padding(.bottom, size == .regular ? 16 : 58)
            .transition(.move(edge: .bottom).combined(with: .opacity))
        }
    }
}

/// Below the required build: nothing else works until the app is updated.
struct UpdateRequiredView: View {
    var update = AppUpdate.shared

    var body: some View {
        VStack(spacing: 18) {
            Spacer()
            Image(systemName: "arrow.down.app.fill").font(.system(size: 64, weight: .semibold)).foregroundStyle(Theme.accent)
                .symbolEffect(.bounce, options: .repeat(2))
            Text("Time to update").font(.system(.largeTitle, design: .rounded, weight: .bold))
            Text("This version of Thomas Creek is too old to keep working with the house. Open TestFlight and tap Update. It takes about a minute.")
                .multilineTextAlignment(.center).foregroundStyle(Theme.dim)
            if let notes = update.info?.notes, !notes.isEmpty {
                Text(notes).font(.callout).multilineTextAlignment(.center).padding(12)
                    .frame(maxWidth: .infinity).background(Theme.card, in: RoundedRectangle(cornerRadius: 14))
            }
            Text("You have \(AppUpdate.current). The newest is \(update.latestLabel).")
                .font(.footnote).foregroundStyle(Theme.dim)
            Spacer()
            PrimaryButton(title: "Open TestFlight", symbol: "arrow.down.circle.fill") { update.openTestFlight() }
            Button(update.checking ? "Checking…" : "Check again") { Task { await update.check(force: true) } }
                .font(.subheadline).disabled(update.checking)
        }
        .padding(28)
        .frame(maxWidth: 520)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Backdrop())
    }
}
