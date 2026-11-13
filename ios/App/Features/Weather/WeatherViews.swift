import SwiftUI
import MapKit
import CoreLocation

// MARK: - Models (GET /v1/weather; see docs/hub-contract.md "Weather")

struct WeatherPage: Codable, Hashable {
    var place: WxPlace
    var places: [WxPlaceRef]?
    var now: Now?
    var hourly: [Hour]?
    var daily: [Day]?
    var minutely: [Minute]?
    var nextHour: String?
    var localTime: String?
    var tz: String?
    var summary: String?
    var air: Air?
    var alerts: [WxAlert]?
    var alertsOk: Bool?
    var sources: [String]?
    var updated: String?
    var outlook: [Outlook]?
    var outlookSource: String?
    var radar: Radar?
    var wear: [Wear]?
    var error: String?

    struct Now: Codable, Hashable {
        var temp: Double?; var feels: Double?; var condition: String?; var symbol: String?; var code: Int?; var isDay: Bool?
        var humidity: Double?; var dewPoint: Double?; var wind: Double?; var windDir: String?; var gust: Double?
        var pressure: Double?; var pressureTrend: String?; var visibility: Double?; var uv: Double?; var uvWords: String?
        var cloud: Double?; var precip: Double?
    }
    struct Hour: Codable, Hashable { var t: String; var temp: Double?; var code: Int?; var symbol: String?; var rainChance: Double?; var precip: Double?; var wind: Double?; var gust: Double? }
    struct Day: Codable, Hashable, Identifiable {
        var date: String; var high: Double?; var low: Double?; var code: Int?; var symbol: String?; var condition: String?
        var rainChance: Double?; var precip: Double?; var snow: Double?; var wind: Double?; var gust: Double?; var windDir: String?
        var uv: Double?; var sunrise: String?; var sunset: String?; var textDay: String?; var textNight: String?
        var id: String { date }
    }
    struct Minute: Codable, Hashable { var t: String; var precip: Double? }
    struct Air: Codable, Hashable { var aqi: Double?; var words: String? }
    struct Outlook: Codable, Hashable, Identifiable {
        var id: String; var title: String; var dates: String?; var temp: Odds?; var precip: Odds?
        struct Odds: Codable, Hashable { var tone: String?; var words: String?; var prob: Int?; var valid: String? }
    }
    struct Radar: Codable, Hashable {
        var provider: String; var frames: [Frame]; var maxZoom: Int?; var stepMin: Int?; var attribution: String?
        struct Frame: Codable, Hashable { var id: String; var t: String; var future: Bool? }
    }
    struct Wear: Codable, Hashable { var emoji: String; var words: String }
}

struct WxPlace: Codable, Hashable {
    var id: String; var kind: String; var name: String; var lat: Double; var lon: Double
    var country: String?; var tz: String?; var spot: String?; var startDate: String?; var endDate: String?; var nearHome: Bool?
    var coordinate: CLLocationCoordinate2D { CLLocationCoordinate2D(latitude: lat, longitude: lon) }
}

struct WxPlaceRef: Codable, Hashable, Identifiable {
    var id: String; var kind: String; var name: String; var startDate: String?; var endDate: String?; var now: Bool?
}

struct WxAlert: Codable, Hashable, Identifiable {
    var id: String
    var source: String?
    var event: String
    var level: String
    var severity: String?
    var certainty: String?
    var urgent: Bool?
    var headline: String?
    var headlineLocal: String?
    var area: String?
    var sender: String?
    var onset: String?
    var ends: String?
    var description: String?
    var instruction: String?
    var polygon: [[Double]]?

    var color: Color { level == "warning" ? Theme.crit : level == "watch" ? Theme.warn : Theme.accent }
}

// MARK: - Store

/// One page per place, kept while the app runs; the hub caches the outside services.
@MainActor
@Observable
final class WeatherStore {
    static let shared = WeatherStore()
    var pages: [String: WeatherPage] = [:]
    var savedAt: [String: Date] = [:]
    var failed: [String: String] = [:]
    /// "home", "here" or "trip:<id>".
    var place = "home"
    var here: CLLocationCoordinate2D?

    var page: WeatherPage? { pages[place] }

    func load(force: Bool = false) async {
        let key = place
        if !force, let at = pages[key]?.updated.flatMap(ISO.parse), -at.timeIntervalSinceNow < 300 { return }
        var q = ["place": key]
        if key == "here" {
            var spot = here
            if spot == nil { spot = await HereLocation.shared.locate() }
            guard let c = spot else {
                failed[key] = "Location is off for Thomas Creek. Turn it on in Settings to see the weather where you are."
                return
            }
            here = c
            q["lat"] = String(format: "%.2f", c.latitude); q["lon"] = String(format: "%.2f", c.longitude)
        }
        do {
            let r: (value: WeatherPage, savedAt: Date?) = try await HubClient.shared.fetch("/v1/weather", q)
            pages[key] = r.value; savedAt[key] = r.savedAt; failed[key] = nil
        } catch {
            failed[key] = error.localizedDescription
        }
    }

    func select(_ p: String) async {
        place = p
        await load()
    }
}

/// The phone's rough location for "Here". Asks only when the person taps Here.
@MainActor
final class HereLocation: NSObject, CLLocationManagerDelegate {
    static let shared = HereLocation()
    private let manager = CLLocationManager()
    private var waiting: [CheckedContinuation<CLLocationCoordinate2D?, Never>] = []

    override init() {
        super.init()
        manager.delegate = self
        manager.desiredAccuracy = kCLLocationAccuracyKilometer
    }

    var allowed: Bool { [.authorizedWhenInUse, .authorizedAlways].contains(manager.authorizationStatus) }

    func locate() async -> CLLocationCoordinate2D? {
        if let l = manager.location, -l.timestamp.timeIntervalSinceNow < 900 { return l.coordinate }
        return await withCheckedContinuation { c in
            waiting.append(c)
            if manager.authorizationStatus == .notDetermined { manager.requestWhenInUseAuthorization() }
            else if allowed { manager.requestLocation() }
            else { finish(nil) }
        }
    }

    private func finish(_ c: CLLocationCoordinate2D?) {
        let w = waiting; waiting = []
        for x in w { x.resume(returning: c) }
    }

    nonisolated func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        Task { @MainActor in
            guard !self.waiting.isEmpty else { return }
            switch manager.authorizationStatus {
            case .authorizedWhenInUse, .authorizedAlways: manager.requestLocation()
            case .notDetermined: break
            default: self.finish(nil)
            }
        }
    }
    nonisolated func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        let c = locations.last?.coordinate
        Task { @MainActor in self.finish(c) }
    }
    nonisolated func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        Task { @MainActor in self.finish(nil) }
    }
}

// MARK: - Formatting

enum Wx {
    static func deg(_ v: Double?) -> String { v.map { "\(Int($0.rounded()))°" } ?? "–" }

    /// Hub times are the place's own wall-clock ("2026-11-12T15:00"), so read and show them in that zone.
    static func local(_ s: String?, tz: String?) -> Date? {
        guard let s else { return nil }
        if s.count > 16, let d = ISO.parse(s) { return d }
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = tz.flatMap(TimeZone.init(identifier:)) ?? .current
        f.dateFormat = s.count == 10 ? "yyyy-MM-dd" : "yyyy-MM-dd'T'HH:mm"
        return f.date(from: String(s.prefix(16)))
    }

    static func format(_ d: Date?, tz: String?, _ style: Date.FormatStyle) -> String {
        guard let d else { return "" }
        var s = style
        s.timeZone = tz.flatMap(TimeZone.init(identifier:)) ?? .current
        return d.formatted(s)
    }

    static func hour(_ s: String, tz: String?) -> String {
        format(local(s, tz: tz), tz: tz, .dateTime.hour())
    }

    static func time(_ s: String?, tz: String?) -> String {
        format(local(s, tz: tz), tz: tz, .dateTime.hour().minute())
    }

    static func weekday(_ s: String, tz: String?, first: Bool) -> String {
        first ? "Today" : format(local(s, tz: tz), tz: tz, .dateTime.weekday(.abbreviated))
    }

    static func tone(_ t: String?) -> Color {
        switch t {
        case "warm": Color(red: 0.86, green: 0.42, blue: 0.18)
        case "cold": Color(red: 0.2, green: 0.45, blue: 0.85)
        case "wet": Color(red: 0.12, green: 0.6, blue: 0.42)
        case "dry": Color(red: 0.62, green: 0.45, blue: 0.15)
        default: Theme.dim
        }
    }

    /// A sky to sit behind the current conditions.
    static func sky(_ code: Int?, day: Bool) -> [Color] {
        let c = code ?? 0
        if !day { return [Color(red: 0.1, green: 0.13, blue: 0.3), Color(red: 0.22, green: 0.27, blue: 0.5)] }
        if c >= 95 { return [Color(red: 0.23, green: 0.27, blue: 0.34), Color(red: 0.42, green: 0.48, blue: 0.55)] }
        if [71, 73, 75, 77, 85, 86].contains(c) { return [Color(red: 0.43, green: 0.54, blue: 0.66), Color(red: 0.66, green: 0.75, blue: 0.83)] }
        if c >= 45 { return [Color(red: 0.35, green: 0.43, blue: 0.52), Color(red: 0.55, green: 0.63, blue: 0.71)] }
        if c >= 3 { return [Color(red: 0.42, green: 0.5, blue: 0.6), Color(red: 0.62, green: 0.7, blue: 0.77)] }
        return [Color(red: 0.29, green: 0.56, blue: 0.84), Color(red: 0.5, green: 0.72, blue: 0.91)]
    }

    static func emoji(_ code: Int?, day: Bool = true) -> String {
        let c = code ?? 0
        switch c {
        case 0, 1: return day ? "☀️" : "🌙"
        case 2: return day ? "⛅️" : "☁️"
        case 3: return "☁️"
        case 45, 48: return "🌫️"
        case 71, 73, 75, 77, 85, 86: return "❄️"
        case 95, 96, 99: return "⛈️"
        case 51...67, 80...82: return "🌧️"
        default: return "🌤️"
        }
    }
}

// MARK: - Home card

/// Home's weather section: now, today's range and any official alert. Opens the Weather page.
struct HomeWeatherCard: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router

    var body: some View {
        if let w = store.home?.weather, w.temp != nil {
            VStack(spacing: 8) {
                if let a = w.alert {
                    Button { router.go(.weatherAlert(a.id)) } label: {
                        HStack(spacing: 10) {
                            Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.white)
                                .frame(width: 30, height: 30).background(a.level == "warning" ? Theme.crit : Theme.warn, in: RoundedRectangle(cornerRadius: 9))
                            VStack(alignment: .leading, spacing: 1) {
                                Text(a.event).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.fg)
                                if let n = w.alertCount, n > 1 { Text("and \(n - 1) more").font(.caption).foregroundStyle(Theme.dim) }
                            }
                            Spacer()
                            Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(Theme.dim)
                        }
                        .card(padding: 12, color: (a.level == "warning" ? Theme.crit : Theme.warn).opacity(0.14))
                    }
                    .buttonStyle(.plain)
                }
                Button { router.go(.weather) } label: {
                    HStack(spacing: 12) {
                        Image(systemName: w.symbol ?? "cloud.sun.fill").symbolRenderingMode(.multicolor).font(.system(size: 34))
                        VStack(alignment: .leading, spacing: 2) {
                            Text("\(Wx.deg(w.temp)) \(w.condition ?? "")").font(.headline).foregroundStyle(Theme.fg)
                            if let hi = w.high, let lo = w.low {
                                Text("High \(Wx.deg(hi)) · Low \(Wx.deg(lo))").font(.subheadline).foregroundStyle(Theme.dim)
                            }
                        }
                        Spacer()
                        Text("Forecast").font(.subheadline.weight(.medium)).foregroundStyle(Theme.accent)
                        Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(Theme.dim)
                    }
                    .card(padding: 14)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Weather: \(Wx.deg(w.temp)) \(w.condition ?? ""). Opens the forecast.")
            }
        }
    }
}

// MARK: - The Weather page

struct WeatherView: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    private let wx = WeatherStore.shared
    @State private var openDay: String?

    var body: some View {
        Group {
            if store.role == .child { KidWeatherView() } else { grownUp }
        }
    }

    private var grownUp: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                placeChips
                if let p = wx.page {
                    if let e = p.error { EmptyCard(symbol: "cloud.slash", title: "No forecast right now", detail: e) }
                    else { content(p) }
                } else if let f = wx.failed[wx.place] {
                    EmptyCard(symbol: "cloud.slash", title: "Weather isn't available", detail: f)
                } else { LoadingCard() }
            }
            .screen()
        }
        .background(Backdrop())
        .navigationTitle(wx.page?.place.kind == "home" || wx.page == nil ? "Weather" : wx.page?.place.name ?? "Weather")
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { await wx.load(force: true) }
        .task { await wx.load() }
    }

    private var placeChips: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                chip("home", "Home", "house.fill")
                ForEach((wx.page?.places ?? []).filter { $0.kind == "trip" }) { t in
                    chip(t.id, t.name, t.now == true ? "airplane.circle.fill" : "airplane")
                }
                chip("here", "Here", "location.fill")
            }
        }
    }

    private func chip(_ id: String, _ title: String, _ symbol: String) -> some View {
        ChipButton(title: title, symbol: symbol, active: wx.place == id) { Task { await wx.select(id) } }
    }

    @ViewBuilder private func content(_ p: WeatherPage) -> some View {
        WeatherHero(page: p)
        ForEach(p.alerts ?? []) { a in
            Button { router.go(.weatherAlert(a.id)) } label: { AlertBanner(alert: a) }.buttonStyle(.plain)
        }
        if p.alertsOk == false {
            Text("Couldn't check for official alerts just now.").font(.footnote).foregroundStyle(Theme.dim)
        }
        if let m = p.minutely, !m.isEmpty { NextHoursCard(line: p.nextHour, minutes: m, tz: p.tz) }
        if let h = p.hourly, !h.isEmpty { HoursCard(hours: h, tz: p.tz) }
        if let r = p.radar, !r.frames.isEmpty {
            SectionHeader(title: "Radar") { MoreButton("Open") { router.go(.weatherRadar) } }
            Button { router.go(.weatherRadar) } label: {
                RadarMap(place: p.place, radar: r, alerts: p.alerts ?? [], interactive: false)
                    .frame(height: 210)
                    .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Radar. Opens the full radar.")
        }
        if let d = p.daily, !d.isEmpty { DaysCard(days: d, tz: p.tz, open: $openDay) }
        if let o = p.outlook, !o.isEmpty {
            SectionHeader(title: "Weeks and months ahead") { MoreButton("More") { router.go(.weatherOutlook) } }
            OutlookRows(rows: Array(o.prefix(3))).card()
        }
        if let n = p.now { DetailsGrid(now: n, air: p.air, today: p.daily?.first, tz: p.tz) }
        if let s = p.sources {
            Text(s.joined(separator: " · ")).font(.caption2).foregroundStyle(Theme.dim)
                .multilineTextAlignment(.center).frame(maxWidth: .infinity).padding(.top, 4)
        }
    }
}

struct WeatherHero: View {
    let page: WeatherPage
    var body: some View {
        let n = page.now
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Image(systemName: page.place.kind == "trip" ? "airplane" : page.place.kind == "here" ? "location.fill" : "house.fill")
                Text(placeLine)
            }
            .font(.subheadline.weight(.semibold)).opacity(0.9)
            HStack(alignment: .center, spacing: 12) {
                Text(Wx.deg(n?.temp)).font(.system(size: 64, weight: .light)).monospacedDigit()
                VStack(alignment: .leading, spacing: 2) {
                    Text(n?.condition ?? "").font(.headline)
                    if let f = n?.feels { Text("Feels \(Wx.deg(f))").font(.subheadline).opacity(0.9) }
                    if let d = page.daily?.first { Text("H \(Wx.deg(d.high)) · L \(Wx.deg(d.low))").font(.subheadline).opacity(0.9) }
                }
                Spacer()
                Image(systemName: n?.symbol ?? "cloud.sun.fill").symbolRenderingMode(.multicolor).font(.system(size: 52))
            }
            if let s = page.summary { Text(s).font(.subheadline).opacity(0.92).fixedSize(horizontal: false, vertical: true) }
        }
        .foregroundStyle(.white)
        .padding(18)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(LinearGradient(colors: Wx.sky(n?.code, day: n?.isDay ?? true), startPoint: .topLeading, endPoint: .bottomTrailing),
                    in: RoundedRectangle(cornerRadius: 24, style: .continuous))
        .accessibilityElement(children: .combine)
    }

    private var placeLine: String {
        var s = page.place.kind == "home" ? "Home" : page.place.name
        if let spot = page.place.spot { s += " · \(spot)" }
        if page.place.kind != "home", let t = page.localTime, let d = Wx.local(t, tz: page.tz) {
            s += " · \(Wx.format(d, tz: page.tz, .dateTime.hour().minute())) there"
        }
        return s
    }
}

struct AlertBanner: View {
    let alert: WxAlert
    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.white)
                .frame(width: 30, height: 30).background(alert.color, in: RoundedRectangle(cornerRadius: 9))
            VStack(alignment: .leading, spacing: 2) {
                Text(alert.event).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.fg)
                Text([alert.ends.map { "Until \(Wx.time($0, tz: nil))" }, alert.headline].compactMap { $0 }.joined(separator: " · "))
                    .font(.caption).foregroundStyle(Theme.dim).lineLimit(3)
            }
            Spacer(minLength: 0)
            Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(Theme.dim)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(alert.color.opacity(0.13), in: RoundedRectangle(cornerRadius: 18, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous).stroke(alert.color, lineWidth: 1.5))
    }
}

struct NextHoursCard: View {
    let line: String?
    let minutes: [WeatherPage.Minute]
    let tz: String?
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("NEXT TWO HOURS").font(.caption.weight(.bold)).foregroundStyle(Theme.dim)
            if let line, !line.isEmpty { Text(line).font(.subheadline.weight(.semibold)) }
            let top = max(0.15, minutes.compactMap(\.precip).max() ?? 0)
            HStack(alignment: .bottom, spacing: 3) {
                ForEach(Array(minutes.enumerated()), id: \.offset) { _, m in
                    let v = m.precip ?? 0
                    RoundedRectangle(cornerRadius: 2).fill(Color.blue.opacity(v > 0 ? 0.8 : 0.18))
                        .frame(height: max(3, CGFloat(v / top) * 40))
                }
            }
            .frame(height: 40, alignment: .bottom)
            HStack {
                Text("Now"); Spacer()
                Text(Wx.hour(minutes[minutes.count / 2].t, tz: tz)); Spacer()
                Text(Wx.time(minutes.last?.t, tz: tz))
            }
            .font(.caption2).foregroundStyle(Theme.dim)
        }
        .card(padding: 14)
        .accessibilityElement(children: .combine)
    }
}

struct HoursCard: View {
    let hours: [WeatherPage.Hour]
    let tz: String?
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("NEXT 48 HOURS").font(.caption.weight(.bold)).foregroundStyle(Theme.dim)
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 6) {
                    ForEach(Array(hours.enumerated()), id: \.offset) { i, h in
                        VStack(spacing: 5) {
                            Text(i == 0 ? "Now" : Wx.hour(h.t, tz: tz)).font(.caption.weight(.semibold)).foregroundStyle(Theme.dim)
                            Text((h.rainChance ?? 0) >= 20 ? "\(Int(h.rainChance ?? 0))%" : " ").font(.caption2.weight(.bold)).foregroundStyle(.blue)
                            Image(systemName: h.symbol ?? "cloud").symbolRenderingMode(.multicolor).font(.title3).frame(height: 26)
                            Text(Wx.deg(h.temp)).font(.subheadline.weight(.semibold))
                        }
                        .frame(width: 46)
                        .accessibilityElement(children: .combine)
                    }
                }
            }
        }
        .card(padding: 14)
    }
}

struct DaysCard: View {
    let days: [WeatherPage.Day]
    let tz: String?
    @Binding var open: String?

    var body: some View {
        let lo = days.compactMap(\.low).min() ?? 0
        let hi = days.compactMap(\.high).max() ?? 1
        VStack(alignment: .leading, spacing: 0) {
            Text("10 DAYS").font(.caption.weight(.bold)).foregroundStyle(Theme.dim).padding(.bottom, 6)
            ForEach(Array(days.enumerated()), id: \.element.id) { i, d in
                if i > 0 { Divider().overlay(Theme.line) }
                Button { withAnimation(.snappy) { open = open == d.id ? nil : d.id } } label: {
                    HStack(spacing: 10) {
                        Text(Wx.weekday(d.date, tz: tz, first: i == 0)).font(.subheadline.weight(.semibold)).frame(width: 50, alignment: .leading)
                        VStack(spacing: 0) {
                            Image(systemName: d.symbol ?? "cloud").symbolRenderingMode(.multicolor)
                            if (d.rainChance ?? 0) >= 20 { Text("\(Int(d.rainChance ?? 0))%").font(.caption2.weight(.bold)).foregroundStyle(.blue) }
                        }
                        .frame(width: 34)
                        Text(Wx.deg(d.low)).foregroundStyle(Theme.dim).frame(width: 36, alignment: .trailing)
                        RangeBar(lo: lo, hi: hi, a: d.low ?? lo, b: d.high ?? hi)
                        Text(Wx.deg(d.high)).fontWeight(.semibold).frame(width: 36, alignment: .leading)
                    }
                    .font(.subheadline).padding(.vertical, 8).contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("\(Wx.weekday(d.date, tz: tz, first: i == 0)): \(d.condition ?? ""), high \(Wx.deg(d.high)), low \(Wx.deg(d.low))")
                if open == d.id { DayDetail(day: d, tz: tz).padding(.bottom, 10) }
            }
        }
        .card(padding: 14)
    }
}

struct RangeBar: View {
    let lo: Double; let hi: Double; let a: Double; let b: Double
    var body: some View {
        GeometryReader { g in
            let span = max(1, hi - lo)
            let x = (a - lo) / span * g.size.width
            let w = max(6, (b - a) / span * g.size.width)
            ZStack(alignment: .leading) {
                Capsule().fill(Theme.card2)
                Capsule().fill(LinearGradient(colors: [.blue, .teal, .yellow, .orange], startPoint: .leading, endPoint: .trailing))
                    .frame(width: w).offset(x: x)
            }
        }
        .frame(height: 6)
    }
}

struct DayDetail: View {
    let day: WeatherPage.Day
    let tz: String?
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let t = day.textDay { Text(t).font(.footnote) }
            if let t = day.textNight { Text("Night: \(t)").font(.footnote).foregroundStyle(Theme.dim) }
            if day.textDay == nil && day.textNight == nil, let c = day.condition { Text(c).font(.footnote) }
            let bits: [String] = [
                day.wind.map { (w: Double) -> String in
                    let dir = day.windDir.map { "\($0) " } ?? ""
                    let gust = day.gust.map { ", gusts \(Int($0))" } ?? ""
                    return "Wind \(dir)\(Int(w)) mph\(gust)"
                },
                day.precip.flatMap { $0 > 0 ? String(format: "%.2f in rain", $0) : nil },
                day.snow.flatMap { $0 > 0 ? String(format: "%.1f in snow", $0) : nil },
                day.uv.map { "UV \(Int($0.rounded()))" },
                day.sunrise.map { "Sunrise \(Wx.time($0, tz: tz))" },
                day.sunset.map { "Sunset \(Wx.time($0, tz: tz))" },
            ].compactMap { $0 }
            Text(bits.joined(separator: " · ")).font(.caption).foregroundStyle(Theme.dim)
        }
        .padding(.leading, 60)
    }
}

struct OutlookRows: View {
    let rows: [WeatherPage.Outlook]
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(rows) { o in
                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    VStack(alignment: .leading, spacing: 1) {
                        Text(o.title).font(.subheadline.weight(.semibold))
                        if let d = o.dates { Text(d).font(.caption2).foregroundStyle(Theme.dim) }
                    }
                    .frame(width: 100, alignment: .leading)
                    VStack(alignment: .leading, spacing: 4) {
                        if let t = o.temp { Pill(text: "🌡 " + (t.words ?? ""), color: Wx.tone(t.tone)) }
                        if let p = o.precip { Pill(text: "💧 " + (p.words ?? ""), color: Wx.tone(p.tone)) }
                    }
                }
            }
        }
    }
}

struct DetailsGrid: View {
    let now: WeatherPage.Now
    let air: WeatherPage.Air?
    let today: WeatherPage.Day?
    let tz: String?
    var body: some View {
        LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible(), spacing: 10)], spacing: 10) {
            tile("Wind", now.wind.map { "\(now.windDir ?? "") \(Int($0))" } ?? "–", now.gust.map { "Gusts \(Int($0)) mph" })
            tile("Humidity", now.humidity.map { "\(Int($0))%" } ?? "–", now.dewPoint.map { "Dew point \(Wx.deg($0))" })
            tile("UV index", now.uv.map { "\(Int($0.rounded()))" } ?? "–", now.uvWords)
            tile("Pressure", now.pressure.map { String(format: "%.2f in", $0) } ?? "–", now.pressureTrend?.capitalized)
            tile("Visibility", now.visibility.map { $0 >= 10 ? "10+ mi" : String(format: "%.1f mi", $0) } ?? "–", now.cloud.map { "\(Int($0))% cloud" })
            if let a = air?.aqi { tile("Air quality", "\(Int(a))", air?.words) }
            if let t = today { tile("Sunrise", Wx.time(t.sunrise, tz: tz), t.sunset.map { "Sunset \(Wx.time($0, tz: tz))" }) }
        }
    }

    private func tile(_ k: String, _ v: String, _ d: String?) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(k.uppercased()).font(.caption2.weight(.bold)).foregroundStyle(Theme.dim)
            Text(v).font(.title3.weight(.semibold)).lineLimit(1).minimumScaleFactor(0.7)
            if let d { Text(d).font(.caption).foregroundStyle(Theme.dim).lineLimit(1) }
        }
        .card(padding: 12)
        .accessibilityElement(children: .combine)
    }
}

// MARK: - Alert detail

struct WeatherAlertView: View {
    let alertID: String
    private let wx = WeatherStore.shared
    @State private var fetched: WxAlert?
    @State private var failed: String?

    private var alert: WxAlert? {
        fetched ?? wx.pages.values.lazy.compactMap { $0.alerts?.first { $0.id == alertID } }.first
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                if let a = alert { content(a) }
                else if let f = failed { EmptyCard(symbol: "checkmark.seal", title: "This alert has ended", detail: f) }
                else { LoadingCard() }
            }
            .screen()
        }
        .background(Backdrop())
        .navigationTitle("Alert")
        .navigationBarTitleDisplayMode(.inline)
        .task {
            guard alert == nil else { return }
            do {
                let path = "/v1/weather/alerts/" + (alertID.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? alertID)
                fetched = try await HubClient.shared.get(path)
            } catch { failed = error.localizedDescription }
        }
    }

    @ViewBuilder private func content(_ a: WxAlert) -> some View {
        AlertBanner(alert: a).allowsHitTesting(false)
        if let de = a.headlineLocal, a.source == "DWD" {
            VStack(alignment: .leading, spacing: 4) {
                Text("ORIGINAL (GERMAN)").font(.caption.weight(.bold)).foregroundStyle(Theme.dim)
                Text(de).font(.subheadline)
            }
            .card(padding: 14)
        }
        RowsCard {
            InfoRow(nil, "Issued by", a.sender ?? a.source)
            if let s = a.severity { InfoRow(nil, "Severity", [s, a.certainty].compactMap { $0 }.joined(separator: " · ")) }
            if let area = a.area { InfoRow(nil, "Area", area) }
            InfoRow(nil, "When", [a.onset.map { "From \(Wx.format(ISO.parse($0), tz: nil, .dateTime.weekday().hour().minute()))" },
                                   a.ends.map { "until \(Wx.format(ISO.parse($0), tz: nil, .dateTime.weekday().hour().minute()))" }]
                .compactMap { $0 }.joined(separator: " "))
        }
        if let d = a.description {
            VStack(alignment: .leading, spacing: 6) {
                Text("WHAT'S HAPPENING").font(.caption.weight(.bold)).foregroundStyle(Theme.dim)
                Text(Self.unwrap(d)).font(.subheadline).textSelection(.enabled)
            }
            .card(padding: 14)
        }
        if let i = a.instruction {
            VStack(alignment: .leading, spacing: 6) {
                Text("WHAT TO DO").font(.caption.weight(.bold)).foregroundStyle(Theme.dim)
                Text(Self.unwrap(i)).font(.subheadline.weight(.medium))
            }
            .card(padding: 14, color: a.color.opacity(0.1))
        }
    }

    /// NWS text comes hard-wrapped at ~70 columns; join the lines back into paragraphs.
    static func unwrap(_ s: String) -> String {
        s.components(separatedBy: "\n\n").map { $0.replacingOccurrences(of: "\n", with: " ") }.joined(separator: "\n\n")
    }
}

// MARK: - Outlook

struct WeatherOutlookView: View {
    private let wx = WeatherStore.shared
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                Text("How likely the coming weeks and months are to be warmer or cooler, and wetter or drier, than normal here. These are odds, not a forecast for a particular day.")
                    .font(.subheadline).foregroundStyle(Theme.dim)
                if let o = wx.page?.outlook, !o.isEmpty {
                    ForEach(o) { row in OutlookRows(rows: [row]).card() }
                    if let s = wx.page?.outlookSource { Text(s).font(.caption).foregroundStyle(Theme.dim) }
                } else { EmptyCard(symbol: "calendar", title: "No outlook right now") }
            }
            .screen()
        }
        .background(Backdrop())
        .navigationTitle("Outlook")
        .navigationBarTitleDisplayMode(.inline)
        .task { await wx.load() }
    }
}

// MARK: - Radar

struct WeatherRadarView: View {
    private let wx = WeatherStore.shared
    @State private var frame = 0
    @State private var playing = true

    var body: some View {
        VStack(spacing: 0) {
            if let p = wx.page, let r = p.radar, !r.frames.isEmpty {
                RadarMap(place: p.place, radar: r, alerts: p.alerts ?? [], interactive: true, frame: $frame, playing: $playing)
                    .ignoresSafeArea(edges: .horizontal)
                controls(r)
            } else if wx.page == nil { LoadingCard().padding() }
            else { EmptyCard(symbol: "map", title: "Radar isn't available here").padding() }
        }
        .background(Backdrop())
        .navigationTitle("Radar")
        .navigationBarTitleDisplayMode(.inline)
        .task { await wx.load() }
    }

    private func controls(_ r: WeatherPage.Radar) -> some View {
        VStack(spacing: 8) {
            HStack(spacing: 12) {
                Button { playing.toggle() } label: {
                    Image(systemName: playing ? "pause.fill" : "play.fill").font(.headline)
                        .frame(width: 40, height: 40).background(Theme.accent, in: Circle()).foregroundStyle(Theme.onAccent)
                }
                .accessibilityLabel(playing ? "Pause" : "Play")
                Slider(value: Binding(get: { Double(min(frame, r.frames.count - 1)) },
                                      set: { playing = false; frame = Int($0.rounded()) }),
                       in: 0...Double(max(1, r.frames.count - 1)), step: 1)
                    .tint(Theme.accent)
            }
            HStack {
                let f = r.frames[min(frame, r.frames.count - 1)]
                Text(label(f)).font(.subheadline.weight(.semibold)).monospacedDigit()
                    .foregroundStyle(f.future == true ? Theme.warn : Theme.fg)
                Spacer()
                Text(r.attribution ?? "").font(.caption2).foregroundStyle(Theme.dim)
            }
            HStack(spacing: 0) {
                ForEach([Color(red: 0.6, green: 0.9, blue: 1), .cyan, .blue, .green, .yellow, .orange, .red, .purple], id: \.self) { c in
                    Rectangle().fill(c)
                }
            }
            .frame(height: 6).clipShape(Capsule())
            HStack { Text("Light"); Spacer(); Text("Heavy"); Spacer(); Text("Hail") }.font(.caption2).foregroundStyle(Theme.dim)
        }
        .padding(16)
        .background(Theme.card)
    }

    private func label(_ f: WeatherPage.Radar.Frame) -> String {
        guard let d = ISO.parse(f.t) else { return f.t }
        let mins = Int((d.timeIntervalSinceNow / 60).rounded())
        let clock = d.formatted(date: .omitted, time: .shortened)
        if f.future == true { return "\(clock) · forecast, in \(mins) min" }
        return mins > -3 ? "\(clock) · latest" : "\(clock) · \(-mins) min ago"
    }
}

/// Map with the radar frames as tile overlays from the hub, cross-fading between frames.
struct RadarMap: UIViewRepresentable {
    let place: WxPlace
    let radar: WeatherPage.Radar
    let alerts: [WxAlert]
    var interactive: Bool
    var frame: Binding<Int>? = nil
    var playing: Binding<Bool>? = nil

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func makeUIView(context: Context) -> MKMapView {
        let map = MKMapView()
        map.delegate = context.coordinator
        map.pointOfInterestFilter = .excludingAll
        map.showsUserLocation = false
        map.isUserInteractionEnabled = interactive
        map.isRotateEnabled = false
        map.isPitchEnabled = false
        let span = interactive ? 3.5 : 2.2
        map.setRegion(MKCoordinateRegion(center: place.coordinate, span: MKCoordinateSpan(latitudeDelta: span, longitudeDelta: span * 1.3)), animated: false)
        let pin = MKPointAnnotation()
        pin.coordinate = place.coordinate
        pin.title = place.kind == "home" ? "Home" : place.name
        map.addAnnotation(pin)
        context.coordinator.install(on: map)
        return map
    }

    func updateUIView(_ map: MKMapView, context: Context) {
        context.coordinator.parent = self
        if context.coordinator.frameKey != radar.frames.map(\.id).joined() { context.coordinator.install(on: map) }
        context.coordinator.syncFromBinding()
    }

    static func dismantleUIView(_ map: MKMapView, coordinator: Coordinator) { coordinator.stop() }

    @MainActor
    final class Coordinator: NSObject, MKMapViewDelegate {
        var parent: RadarMap
        var overlays: [RadarTiles] = []
        var renderers: [ObjectIdentifier: MKTileOverlayRenderer] = [:]
        var frameKey = ""
        var current = 0
        private var timer: Timer?

        init(_ p: RadarMap) { parent = p }

        func install(on map: MKMapView) {
            map.removeOverlays(map.overlays)
            renderers = [:]
            let r = parent.radar
            frameKey = r.frames.map(\.id).joined()
            overlays = r.frames.map { RadarTiles(provider: r.provider, frame: $0.id, maxZ: r.maxZoom ?? 10) }
            for o in overlays { map.addOverlay(o, level: .aboveRoads) }
            for a in parent.alerts {
                guard let pts = a.polygon, pts.count > 2 else { continue }
                let coords = pts.map { CLLocationCoordinate2D(latitude: $0[1], longitude: $0[0]) }
                let poly = AlertPolygon(coordinates: coords, count: coords.count)
                poly.color = UIColor(a.color)
                map.addOverlay(poly, level: .aboveLabels)
            }
            // Start on the newest scan that isn't a forecast.
            current = r.frames.lastIndex { $0.future != true } ?? max(0, r.frames.count - 1)
            let start = current
            DispatchQueue.main.async { self.parent.frame?.wrappedValue = start }
            show(current)
            startTimer()
        }

        func syncFromBinding() {
            guard let f = parent.frame?.wrappedValue, f != current, f < overlays.count else { return }
            current = f
            show(f)
        }

        func startTimer() {
            timer?.invalidate()
            let reduce = UIAccessibility.isReduceMotionEnabled
            timer = Timer.scheduledTimer(withTimeInterval: reduce ? 1.6 : 0.5, repeats: true) { [weak self] _ in
                Task { @MainActor in self?.tick() }
            }
        }

        func stop() { timer?.invalidate(); timer = nil }

        private func tick() {
            guard !overlays.isEmpty, parent.playing?.wrappedValue ?? true else { return }
            // Hold a beat on the latest frame so "now" is easy to read.
            current = (current + 1) % overlays.count
            parent.frame?.wrappedValue = current
            show(current)
        }

        private func show(_ i: Int) {
            for (n, o) in overlays.enumerated() {
                renderers[ObjectIdentifier(o)]?.alpha = n == i ? 0.75 : 0
            }
        }

        func mapView(_ mapView: MKMapView, rendererFor overlay: MKOverlay) -> MKOverlayRenderer {
            if let t = overlay as? RadarTiles {
                let r = MKTileOverlayRenderer(tileOverlay: t)
                let i = overlays.firstIndex { $0 === t } ?? -1
                r.alpha = i == current ? 0.75 : 0
                renderers[ObjectIdentifier(t)] = r
                return r
            }
            if let p = overlay as? AlertPolygon {
                let r = MKPolygonRenderer(polygon: p)
                r.strokeColor = p.color
                r.fillColor = p.color.withAlphaComponent(0.08)
                r.lineWidth = 2
                r.lineDashPattern = [6, 4]
                return r
            }
            return MKOverlayRenderer(overlay: overlay)
        }
    }
}

final class AlertPolygon: MKPolygon { var color: UIColor = .systemRed }

/// Radar tiles for one frame, fetched through the hub with this phone's token.
final class RadarTiles: MKTileOverlay {
    let provider: String
    let frame: String

    init(provider: String, frame: String, maxZ: Int) {
        self.provider = provider
        self.frame = frame
        super.init(urlTemplate: nil)
        canReplaceMapContent = false
        minimumZ = 2
        maximumZ = maxZ
        tileSize = CGSize(width: 256, height: 256)
    }

    override func loadTile(at path: MKTileOverlayPath, result: @escaping (Data?, Error?) -> Void) {
        let p = "/v1/weather/radar/\(provider)/\(frame)/\(path.z)/\(path.x)/\(path.y).png"
        Task {
            do {
                var req = try await HubClient.shared.authorizedRequest(p)
                req.timeoutInterval = 15
                let (data, resp) = try await HubClient.session.data(for: req)
                let ok = (resp as? HTTPURLResponse)?.statusCode == 200
                result(ok ? data : nil, nil)
            } catch {
                result(nil, error)
            }
        }
    }
}

// MARK: - Kids

/// Pictures first: how it feels outside, what to wear, the next three days. Warnings by name only.
struct KidWeatherView: View {
    @Environment(HomeStore.self) private var store
    private let wx = WeatherStore.shared
    private var little: Bool { store.me?.style == .little }
    private var color: Color { little ? Theme.little : Theme.kid }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if let p = wx.page, let n = p.now {
                    hero(p, n)
                    if let w = p.wear, !w.isEmpty {
                        Text(little ? "Wear" : "What to wear").font(Theme.kidFont(20))
                        HStack(spacing: 10) {
                            ForEach(w, id: \.self) { item in
                                VStack(spacing: 6) {
                                    Text(item.emoji).font(.system(size: 44))
                                    Text(item.words).font(Theme.kidFont(15, .semibold)).multilineTextAlignment(.center)
                                }
                                .frame(maxWidth: .infinity).padding(.vertical, 14)
                                .background(Theme.card, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
                            }
                        }
                    }
                    if let d = p.daily, d.count > 1 {
                        Text("Next days").font(Theme.kidFont(20))
                        HStack(spacing: 10) {
                            ForEach(Array(d.prefix(3).enumerated()), id: \.offset) { i, day in
                                VStack(spacing: 4) {
                                    Text(Wx.emoji(day.code)).font(.system(size: 40))
                                    Text(i == 0 ? "Today" : i == 1 ? "Tomorrow" : Wx.weekday(day.date, tz: p.tz, first: false)).font(Theme.kidFont(15))
                                    Text(Wx.deg(day.high)).font(Theme.kidFont(15, .semibold)).foregroundStyle(Theme.dim)
                                }
                                .frame(maxWidth: .infinity).padding(.vertical, 12)
                                .background(Theme.card, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
                            }
                        }
                    }
                } else if let f = wx.failed[wx.place] {
                    EmptyCard(symbol: "cloud.slash", title: "No weather right now", detail: f)
                } else { LoadingCard() }
            }
            .screen()
        }
        .background(Backdrop())
        .navigationTitle("Weather")
        .navigationBarTitleDisplayMode(.inline)
        .task { wx.place = "home"; await wx.load() }
        .refreshable { await wx.load(force: true) }
    }

    private func hero(_ p: WeatherPage, _ n: WeatherPage.Now) -> some View {
        let warn = p.alerts?.first
        let line = warn.map { "\($0.event)! Stay with a grown-up." } ?? kidLine(n, today: p.daily?.first)
        return VStack(spacing: 6) {
            Text(Wx.emoji(n.code, day: n.isDay ?? true)).font(.system(size: 64))
            Text(Wx.deg(n.temp)).font(Theme.kidFont(72))
            Text(line).font(Theme.kidFont(little ? 22 : 20, .semibold)).multilineTextAlignment(.center)
            if little { SayButton(text: "It's \(Wx.deg(n.temp)) outside. \(line)") }
        }
        .foregroundStyle(.white)
        .frame(maxWidth: .infinity).padding(22)
        .background(LinearGradient(colors: warn != nil ? [Theme.crit, Theme.warn] : Wx.sky(n.code, day: n.isDay ?? true),
                                   startPoint: .top, endPoint: .bottom),
                    in: RoundedRectangle(cornerRadius: 28, style: .continuous))
    }

    private func kidLine(_ n: WeatherPage.Now, today: WeatherPage.Day?) -> String {
        let c = n.code ?? 0
        if c >= 95 { return "Storms! Stay inside when it thunders." }
        if [71, 73, 75, 77, 85, 86].contains(c) { return "Snow! ❄️" }
        if (51...67).contains(c) || (80...82).contains(c) { return "Rainy. Puddle time!" }
        if (today?.rainChance ?? 0) >= 60 { return "Rain later today." }
        if (n.temp ?? 60) <= 35 { return "Brrr, it's cold!" }
        if (n.temp ?? 60) >= 85 { return "Hot! Drink lots of water." }
        return "Nice day to play outside!"
    }
}
