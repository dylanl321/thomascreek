import SwiftUI
import MapKit

// MARK: - Models (GET /v1/trips, GET /v1/trips/{id}; see server/docs/API.md "Trips")

struct TripList: Codable, Hashable {
    var trips: [Trip]
    var next: Trip?
    var planUrl: String?
}

struct Trip: Codable, Hashable, Identifiable {
    var id: Int
    var title: String
    var description: String?
    var startDate: String?
    var endDate: String?
    var archived: Bool?
    /// "now", "upcoming", "someday" or "past"
    var status: String
    var daysUntil: Int?
    var dayNumber: Int?
    var dayCount: Int?
    var travellers: [Traveller]?
    var planUrl: String?
    // Detail only.
    var today: String?
    var days: [TripDay]?
    var accommodations: [Stay]?
    var unplannedPlaces: [Place]?
    var unscheduledReservations: [Booking]?

    struct Traveller: Codable, Hashable { var name: String; var owner: Bool? }

    struct Place: Codable, Hashable {
        var name: String
        var address: String?
        var lat: Double?
        var lng: Double?
        var time: String?
        var endTime: String?
        var durationMinutes: Double?
        var category: String?
        var notes: String?
        var transportMode: String?
        var coordinate: CLLocationCoordinate2D? { lat.flatMap { la in lng.map { CLLocationCoordinate2D(latitude: la, longitude: $0) } } }
    }

    struct Booking: Codable, Hashable {
        var type: String?
        var title: String?
        var location: String?
        var time: String?
        var endTime: String?
        var status: String?
        var notes: String?
    }

    struct Stay: Codable, Hashable {
        var name: String?
        var address: String?
        var lat: Double?
        var lng: Double?
        var startDate: String?
        var endDate: String?
        var checkIn: String?
        var checkOut: String?
        var notes: String?
    }

    struct DayNote: Codable, Hashable { var text: String; var time: String? }

    var isOn: Bool { status == "now" }
    var isPast: Bool { status == "past" }
    var start: Date? { TripDate.parse(startDate) }
    var end: Date? { TripDate.parse(endDate) ?? start }

    var range: String {
        guard let s = start else { return "No dates yet" }
        guard let e = end, e != s else { return s.formatted(.dateTime.month(.abbreviated).day().year()) }
        let sameMonth = Calendar.current.isDate(s, equalTo: e, toGranularity: .month)
        return "\(s.formatted(.dateTime.month(.abbreviated).day())) – \(sameMonth ? e.formatted(.dateTime.day()) : e.formatted(.dateTime.month(.abbreviated).day())), \(e.formatted(.dateTime.year()))"
    }

    var countdown: String {
        if isOn, let n = dayNumber { return "Day \(n)\(dayCount.map { " of \($0)" } ?? "")" }
        if let d = daysUntil { return d == 1 ? "Tomorrow" : "\(d) days" }
        return status == "someday" ? "Someday" : ""
    }

    var allBookings: [(day: TripDay?, booking: Booking)] {
        let dated: [(day: TripDay?, booking: Booking)] = (days ?? []).flatMap { d in d.reservations.map { (day: Optional(d), booking: $0) } }
        let loose: [(day: TripDay?, booking: Booking)] = (unscheduledReservations ?? []).map { (day: nil, booking: $0) }
        return dated + loose
    }
}

struct TripDay: Codable, Hashable, Identifiable {
    var date: String
    var dayNumber: Int
    var title: String?
    var notes: String?
    var places: [Trip.Place]
    var dayNotes: [Trip.DayNote]
    var reservations: [Trip.Booking]
    var id: String { date }
    var day: Date? { TripDate.parse(date) }
    var name: String { title?.isEmpty == false ? title! : "Day \(dayNumber)" }
}

enum TripDate {
    static func parse(_ s: String?) -> Date? {
        guard let s, s.count >= 10 else { return nil }
        let p = s.prefix(10).split(separator: "-").compactMap { Int($0) }
        guard p.count == 3 else { return nil }
        return Calendar.current.date(from: DateComponents(year: p[0], month: p[1], day: p[2], hour: 12))
    }

    /// "17:30" or an ISO time, as the phone shows times ("5:30 PM").
    static func time(_ s: String?) -> String? {
        guard let s, !s.isEmpty else { return nil }
        if s.count <= 5, s.contains(":") {
            let p = s.split(separator: ":").compactMap { Int($0) }
            guard p.count == 2, let d = Calendar.current.date(from: DateComponents(hour: p[0], minute: p[1])) else { return s }
            return d.formatted(date: .omitted, time: .shortened)
        }
        // "2026-11-27T17:30" (TREK keeps what the user typed, often with no zone).
        if s.count >= 16, let t = time(String(s.dropFirst(11).prefix(5))) {
            let day = parse(s).map { $0.formatted(.dateTime.weekday(.abbreviated)) }
            return [day, t].compactMap { $0 }.joined(separator: " ")
        }
        return s
    }
}

/// One shared copy of the trip list so Home, Family and the kids' screens don't each ask the hub.
@MainActor
@Observable
final class TripStore {
    static let shared = TripStore()
    var list: TripList?
    var failed: String?
    private var loadedAt: Date?

    /// When the list was last fetched from home (nil: showing a saved copy, see `savedAt`).
    var savedAt: Date?

    func load(force: Bool = false) async {
        if list == nil { list = HubClient.saved("/v1/trips"); savedAt = OfflineCache.savedAt(path: "/v1/trips") }
        if !force, let at = loadedAt, Date.now.timeIntervalSince(at) < 120 { return }
        do {
            let r = try await HubClient.shared.fetch("/v1/trips", as: TripList.self)
            list = r.value; savedAt = r.savedAt ?? .now
            failed = nil
            if r.savedAt == nil { loadedAt = .now; Task { await saveForOffline() } }
        } catch {
            failed = error.localizedDescription
        }
    }

    func detail(_ id: Int) async throws -> Trip {
        try await HubClient.shared.get("/v1/trips/\(id)", keep: isKept(id), as: Trip.self)
    }

    // MARK: offline

    /// Trips kept on the phone: on now, starting within two weeks, or back less than a week ago,
    /// plus any an adult switched on by hand (and minus any switched off).
    func isKept(_ id: Int) -> Bool {
        if AppSettings.offlineTripsOff.contains(id) { return false }
        if AppSettings.offlineTripsOn.contains(id) { return true }
        guard let t = list?.trips.first(where: { $0.id == id }) else { return false }
        return Self.autoKeep(t)
    }

    static func autoKeep(_ t: Trip) -> Bool {
        if t.isOn { return true }
        let today = Calendar.current.startOfDay(for: .now)
        if let s = t.start, s >= today, s.timeIntervalSince(today) <= 14 * 86400 { return true }
        if let e = t.end, e < today, today.timeIntervalSince(e) <= 7 * 86400 { return true }
        return false
    }

    func setKept(_ id: Int, _ on: Bool) {
        var a = AppSettings.offlineTripsOn, b = AppSettings.offlineTripsOff
        a.remove(id); b.remove(id)
        if on != list.flatMap({ $0.trips.first { $0.id == id } }).map(Self.autoKeep) ?? false { if on { a.insert(id) } else { b.insert(id) } }
        AppSettings.offlineTripsOn = a; AppSettings.offlineTripsOff = b
        Task { await saveForOffline() }
    }

    var keptTrips: [Trip] { (list?.trips ?? []).filter { isKept($0.id) } }

    /// Fetch every kept trip in full and draw each day's map as a picture (maps need signal).
    func saveForOffline() async {
        guard let list else { return }
        for t in list.trips {
            if isKept(t.id) {
                guard let full = try? await HubClient.shared.get("/v1/trips/\(t.id)", keep: true, as: Trip.self) else { continue }
                for day in full.days ?? [] where TripMapPicture.load(trip: t.id, date: day.date) == nil {
                    await TripMapPicture.make(trip: t.id, day: day)
                }
            } else {
                OfflineCache.unkeep(path: "/v1/trips/\(t.id)")
                TripMapPicture.remove(trip: t.id)
            }
        }
    }
}

// MARK: - Look

/// TREK trips have no photo through the API, so each trip gets a scene from its title.
enum TripArt {
    /// TREK has no cover images, so the cover shows the destination's flag, found by a country name
    /// in the trip's title or description ("Germany: History and Christmas Markets" -> 🇩🇪), else ✈️.
    static func flag(_ trip: Trip) -> String {
        let text = " " + [trip.title, trip.description ?? ""].joined(separator: " ").lowercased() + " "
        let en = Locale(identifier: "en_US")
        for region in Locale.Region.isoRegions where region.identifier.count == 2 && region.identifier.allSatisfy { $0.isLetter } {
            guard let name = en.localizedString(forRegionCode: region.identifier)?.lowercased(), name.count > 3 else { continue }
            if text.range(of: "\\b\(NSRegularExpression.escapedPattern(for: name))\\b", options: .regularExpression) != nil {
                return region.identifier.uppercased().unicodeScalars
                    .compactMap { UnicodeScalar(127397 + $0.value) }.map { String($0) }.joined()
            }
        }
        return "✈️"
    }

    /// Warm sunset gradient for every trip cover.
    static let warm: [Color] = [Color(red: 0.93, green: 0.45, blue: 0.33), Color(red: 0.98, green: 0.76, blue: 0.45)]

    static func symbol(_ title: String) -> String {
        let t = title.lowercased()
        if ["beach", "bank", "island", "coast", "shore", "ocean", "bay"].contains(where: t.contains) { return "beach.umbrella.fill" }
        if ["mountain", "hike", "asheville", "ridge", "ski", "park"].contains(where: t.contains) { return "mountain.2.fill" }
        if ["lake", "river", "cabin", "camp"].contains(where: t.contains) { return "tent.fill" }
        if ["disney", "orlando", "theme"].contains(where: t.contains) { return "sparkles" }
        if ["thanksgiving", "christmas", "grandma", "grandpa", "family"].contains(where: t.contains) { return "house.fill" }
        return "airplane"
    }

    static func colors(_ id: Int) -> [Color] {
        let sets: [[Color]] = [
            [Color(red: 0.18, green: 0.35, blue: 0.42), Color(red: 0.91, green: 0.64, blue: 0.42)],
            [Color(red: 0.23, green: 0.53, blue: 0.66), Color(red: 0.95, green: 0.82, blue: 0.61)],
            [Color(red: 0.31, green: 0.54, blue: 0.43), Color(red: 0.94, green: 0.78, blue: 0.45)],
            [Color(red: 0.36, green: 0.29, blue: 0.55), Color(red: 0.93, green: 0.55, blue: 0.52)],
        ]
        return sets[abs(id) % sets.count]
    }

    static func emoji(_ text: String?) -> String {
        let t = (text ?? "").lowercased()
        let table: [(String, [String])] = [
            ("🚗", ["drive", "road", "head home", "leave"]), ("✈️", ["fly", "flight", "airport"]),
            ("🦃", ["thanksgiving", "turkey"]), ("🎄", ["christmas"]), ("🏰", ["castle", "biltmore", "palace"]),
            ("🥾", ["hike", "trail", "parkway", "mountain"]), ("🏖️", ["beach", "sand", "dune"]),
            ("🐴", ["horse"]), ("🎢", ["park", "ride", "disney"]), ("🎨", ["art", "museum", "gallery"]),
            ("🍽️", ["dinner", "lunch", "restaurant", "eat"]), ("🥞", ["breakfast", "pancake", "slow morning"]),
            ("🏊", ["pool", "swim"]), ("🏡", ["home", "cabin", "check in", "check out"]),
        ]
        return table.first { $0.1.contains(where: t.contains) }?.0 ?? "📍"
    }
}

struct TripCover: View {
    let trip: Trip
    var height: CGFloat = 120
    var body: some View {
        ZStack(alignment: .bottomLeading) {
            LinearGradient(colors: TripArt.warm, startPoint: .topLeading, endPoint: .bottomTrailing)
            Text(TripArt.flag(trip))
                .font(.system(size: height * 0.42))
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topTrailing)
                .padding(14)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(trip.title).font(.title3.weight(.bold)).lineLimit(2)
                Text(trip.range).font(.footnote).opacity(0.9)
            }
            .foregroundStyle(.white).shadow(color: .black.opacity(0.3), radius: 6)
            .padding(14)
        }
        .frame(height: height)
        .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
        .accessibilityElement(children: .combine)
    }
}

struct TravellerFaces: View {
    let travellers: [Trip.Traveller]
    var body: some View {
        HStack(spacing: -6) {
            ForEach(Array(travellers.prefix(6).enumerated()), id: \.offset) { _, t in
                Avatar(name: t.name, size: 26).overlay(Circle().stroke(Theme.card, lineWidth: 2))
            }
        }
        .accessibilityLabel(travellers.map(\.name).joined(separator: ", "))
    }
}

// MARK: - Trips list (Family → Trips)

struct TripsView: View {
    @Environment(Router.self) private var router
    @Environment(\.openURL) private var openURL
    private let trips = TripStore.shared

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                if let list = trips.list {
                    let live = list.trips.filter { !$0.isPast }
                    let past = list.trips.filter(\.isPast)
                    SectionHeader(title: "Upcoming") {
                        if let s = list.planUrl, let u = URL(string: s) { MoreButton("Plan in TREK") { openURL(u) } }
                    }
                    if live.isEmpty { EmptyCard(symbol: "suitcase", title: "No trips planned", detail: "Trips you plan in TREK show up here.") }
                    ForEach(live) { t in
                        Button { router.go(.trip(t.id)) } label: { TripCard(trip: t) }.buttonStyle(.plain)
                    }
                    if !past.isEmpty {
                        SectionHeader("Past trips")
                        RowsCard {
                            ForEach(past) { t in
                                Button { router.go(.trip(t.id)) } label: {
                                    InfoRow(TripArt.symbol(t.title), t.title, t.range, chevron: true)
                                }
                                .buttonStyle(.plain)
                            }
                        }
                    }
                } else if let f = trips.failed {
                    EmptyCard(symbol: "suitcase", title: "Trips aren't available", detail: f)
                } else { LoadingCard() }
            }
            .screen()
        }
        .background(Backdrop())
        .navigationTitle("Trips")
        .refreshable { await trips.load(force: true) }
        .task { await trips.load() }
    }
}

struct TripCard: View {
    let trip: Trip
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            TripCover(trip: trip)
            HStack(spacing: 10) {
                if let t = trip.travellers, !t.isEmpty { TravellerFaces(travellers: t) }
                Spacer()
                if !trip.countdown.isEmpty { Pill(text: trip.isOn ? "Now · \(trip.countdown)" : trip.countdown) }
            }
            .padding(.horizontal, 4)
        }
        .card(padding: 10)
    }
}

// MARK: - One trip

struct TripView: View {
    let tripID: Int
    @Environment(Router.self) private var router
    @Environment(\.openURL) private var openURL
    @State private var trip: Trip?
    @State private var failed: String?
    @State private var section: Part = .plan
    @State private var savedAt: Date?

    enum Part: String, CaseIterable { case plan = "Plan", bookings = "Bookings", stay = "Stay", ideas = "Ideas" }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                if let trip {
                    TripCover(trip: trip, height: 150)
                    HStack(spacing: 10) {
                        if let t = trip.travellers, !t.isEmpty {
                            TravellerFaces(travellers: t)
                            Text(t.map(\.name).joined(separator: ", ")).font(.caption).foregroundStyle(Theme.dim).lineLimit(2)
                        }
                        Spacer()
                        if !trip.countdown.isEmpty, !trip.isPast { Pill(text: trip.isOn ? trip.countdown : "\(trip.countdown) to go") }
                    }
                    TripOfflineRow(trip: trip, savedAt: savedAt)
                    if !trip.isPast {
                        // The forecast where we're going (the Weather screen's trip place).
                        HStack(spacing: 10) {
                            Image(systemName: "cloud.sun.fill").symbolRenderingMode(.multicolor)
                            Text("Weather there").font(.subheadline.weight(.semibold)).foregroundStyle(Theme.fg)
                            Spacer()
                            Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(Theme.dim)
                        }
                        .card(padding: 12)
                        .linksToWeather(place: "trip:\(tripID)")
                    }
                    Picker("Section", selection: $section) {
                        ForEach(Part.allCases, id: \.self) { Text($0.rawValue) }
                    }
                    .pickerStyle(.segmented)
                    switch section {
                    case .plan: plan(trip)
                    case .bookings: bookings(trip)
                    case .stay: stay(trip)
                    case .ideas: ideas(trip)
                    }
                    if let s = trip.planUrl, let u = URL(string: s), !trip.isPast {
                        PrimaryButton(title: "Plan in TREK", symbol: "arrow.up.forward.app") { openURL(u) }.padding(.top, 6)
                    }
                } else if let failed {
                    EmptyCard(symbol: "suitcase", title: "Couldn't open this trip", detail: failed)
                } else { LoadingCard() }
            }
            .screen()
        }
        .background(Backdrop())
        .navigationTitle(trip?.title ?? "Trip")
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { await load() }
        .task { await load() }
    }

    private func load() async {
        if trip == nil, let saved: Trip = HubClient.saved("/v1/trips/\(tripID)") {
            trip = saved; savedAt = OfflineCache.savedAt(path: "/v1/trips/\(tripID)")
        }
        do {
            let r = try await HubClient.shared.fetch("/v1/trips/\(tripID)", keep: TripStore.shared.isKept(tripID), as: Trip.self)
            trip = r.value; savedAt = r.savedAt; failed = nil
        } catch { if trip == nil { failed = error.localizedDescription } }
    }

    @ViewBuilder private func plan(_ t: Trip) -> some View {
        let days = t.days ?? []
        if days.isEmpty {
            EmptyCard(symbol: "calendar", title: "No days planned yet", detail: "Days show up here once they're added in TREK.")
        } else {
            RowsCard {
                ForEach(days) { d in
                    Button { router.go(.tripDay(t.id, d.date)) } label: {
                        HStack(spacing: 12) {
                            VStack(alignment: .leading, spacing: 2) {
                                HStack(spacing: 8) {
                                    Text("Day \(d.dayNumber)").font(.subheadline.weight(.semibold))
                                    if let day = d.day { Text(day.formatted(.dateTime.weekday(.abbreviated).month(.abbreviated).day())).font(.caption).foregroundStyle(Theme.dim) }
                                    if t.today == d.date { Pill(text: "Today") }
                                }
                                if d.title?.isEmpty == false { Text(d.name).font(.subheadline) }
                                Text("\(d.places.count) stop\(d.places.count == 1 ? "" : "s")\(d.reservations.isEmpty ? "" : " · \(d.reservations.count) booked")")
                                    .font(.caption).foregroundStyle(Theme.dim)
                            }
                            Spacer()
                            Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(Theme.dim)
                        }
                        .padding(.vertical, 8)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    @ViewBuilder private func bookings(_ t: Trip) -> some View {
        let all = t.allBookings
        if all.isEmpty {
            EmptyCard(symbol: "ticket", title: "Nothing booked yet", detail: "Flights, tickets and reservations added in TREK appear here.")
        } else {
            RowsCard {
                ForEach(Array(all.enumerated()), id: \.offset) { _, row in
                    BookingRow(booking: row.booking, day: row.day)
                }
            }
        }
    }

    @ViewBuilder private func stay(_ t: Trip) -> some View {
        let stays = t.accommodations ?? []
        if stays.isEmpty {
            EmptyCard(symbol: "bed.double", title: "No place to stay yet", detail: "Add where you're staying in TREK.")
        } else {
            ForEach(Array(stays.enumerated()), id: \.offset) { _, s in
                VStack(alignment: .leading, spacing: 10) {
                    HStack(spacing: 12) {
                        IconBadge(symbol: "bed.double.fill")
                        VStack(alignment: .leading, spacing: 2) {
                            Text(s.name ?? "Place to stay").font(.headline)
                            if let a = s.address { Text(a).font(.caption).foregroundStyle(Theme.dim) }
                        }
                    }
                    HStack(alignment: .top) {
                        stayFact("Check in", s.startDate, s.checkIn)
                        stayFact("Check out", s.endDate.flatMap { TripDate.parse($0) }.map { Calendar.current.date(byAdding: .day, value: 1, to: $0)! }.map { ISODate.string($0) }, s.checkOut)
                    }
                    if let n = s.notes, !n.isEmpty { Text(n).font(.footnote).foregroundStyle(Theme.dim).textSelection(.enabled) }
                    if let dest = Directions.destination(name: s.name, lat: s.lat, lng: s.lng, address: s.address) {
                        Button { Directions.open(dest) } label: { Label("Directions", systemImage: "arrow.triangle.turn.up.right.diamond.fill") }
                            .buttonStyle(.bordered).tint(Theme.accent)
                    }
                }
                .card()
            }
        }
    }

    private func stayFact(_ label: String, _ date: String?, _ time: String?) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label).font(.caption).foregroundStyle(Theme.dim)
            Text([TripDate.parse(date)?.formatted(.dateTime.weekday(.abbreviated).month(.abbreviated).day()), TripDate.time(time)]
                .compactMap { $0 }.joined(separator: ", ")).font(.subheadline.weight(.semibold))
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder private func ideas(_ t: Trip) -> some View {
        let ideas = t.unplannedPlaces ?? []
        if ideas.isEmpty {
            EmptyCard(symbol: "lightbulb", title: "No ideas saved", detail: "Places saved in TREK that aren't on a day yet show up here.")
        } else {
            RowsCard {
                ForEach(Array(ideas.enumerated()), id: \.offset) { _, p in
                    InfoRow("lightbulb", p.name, p.address ?? p.category)
                }
            }
        }
    }
}

enum ISODate {
    static func string(_ d: Date) -> String {
        let c = Calendar.current.dateComponents([.year, .month, .day], from: d)
        return String(format: "%04d-%02d-%02d", c.year ?? 0, c.month ?? 0, c.day ?? 0)
    }
}

struct BookingRow: View {
    let booking: Trip.Booking
    var day: TripDay?
    private var symbol: String {
        switch (booking.type ?? "").lowercased() {
        case "flight": "airplane"
        case "train": "tram.fill"
        case "car", "rental_car", "car_rental": "car.fill"
        case "hotel", "accommodation": "bed.double.fill"
        case "restaurant": "fork.knife"
        case "event", "tour", "activity", "tickets": "ticket.fill"
        default: "checkmark.seal"
        }
    }
    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            IconBadge(symbol: symbol, size: 32)
            VStack(alignment: .leading, spacing: 3) {
                Text(booking.title ?? booking.type?.capitalized ?? "Booking").font(.subheadline.weight(.semibold))
                Text([day?.day?.formatted(.dateTime.weekday(.abbreviated).month(.abbreviated).day()), TripDate.time(booking.time), booking.location]
                    .compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · "))
                    .font(.caption).foregroundStyle(Theme.dim)
                if let n = booking.notes, !n.isEmpty { Text(n).font(.caption).textSelection(.enabled) }
            }
            Spacer(minLength: 0)
            if let s = booking.status, !s.isEmpty { Pill(text: s.capitalized, color: s.lowercased() == "pending" ? Theme.warn : Theme.accent) }
        }
        .padding(.vertical, 8)
    }
}

// MARK: - One day: map and stops

struct TripDayView: View {
    let tripID: Int
    let date: String
    @Environment(HomeStore.self) private var store
    @Environment(\.openURL) private var openURL
    @State private var trip: Trip?

    private var day: TripDay? { trip?.days?.first { $0.date == date } }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                if let day {
                    VStack(alignment: .leading, spacing: 2) {
                        if let d = day.day { Text(d.formatted(.dateTime.weekday(.wide).month(.wide).day())).font(.subheadline).foregroundStyle(Theme.dim) }
                        Text(day.name).font(.title2.weight(.bold))
                    }
                    let pins = day.places.enumerated().compactMap { i, p in p.coordinate.map { (i + 1, p.name, $0) } }
                    if !pins.isEmpty, !store.reachable, let pic = TripMapPicture.load(trip: tripID, date: date) {
                        Image(uiImage: pic).resizable().scaledToFill()
                            .frame(height: 220).frame(maxWidth: .infinity)
                            .clipShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
                            .overlay(alignment: .bottomLeading) {
                                Label("Saved map", systemImage: "arrow.down.circle.fill").font(.caption.weight(.semibold))
                                    .padding(.horizontal, 8).padding(.vertical, 4).background(.ultraThinMaterial, in: Capsule()).padding(8)
                            }
                    } else if !pins.isEmpty {
                        Map(initialPosition: .automatic) {
                            ForEach(pins, id: \.0) { n, name, c in Marker(name, monogram: Text("\(n)"), coordinate: c).tint(Theme.accent) }
                            if pins.count > 1 { MapPolyline(coordinates: pins.map(\.2)).stroke(Theme.accent, style: StrokeStyle(lineWidth: 3, dash: [6, 5])) }
                        }
                        .frame(height: 220)
                        .clipShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
                    }
                    VStack(alignment: .leading, spacing: 0) {
                        ForEach(Array(day.places.enumerated()), id: \.offset) { i, p in StopRow(number: i + 1, place: p) }
                        if day.places.isEmpty { Text("No stops on this day yet.").foregroundStyle(Theme.dim).padding(14) }
                    }
                    .card(padding: 4)
                    if !day.dayNotes.isEmpty || day.notes?.isEmpty == false {
                        SectionHeader("Notes")
                        VStack(alignment: .leading, spacing: 8) {
                            if let n = day.notes, !n.isEmpty { Text(n) }
                            ForEach(Array(day.dayNotes.enumerated()), id: \.offset) { _, n in
                                HStack(alignment: .top) {
                                    if let t = TripDate.time(n.time) { Text(t).font(.caption.monospacedDigit()).foregroundStyle(Theme.dim).frame(width: 64, alignment: .leading) }
                                    Text(n.text)
                                }
                            }
                        }
                        .font(.subheadline).card()
                    }
                    if !day.reservations.isEmpty {
                        SectionHeader("Booked")
                        RowsCard { ForEach(Array(day.reservations.enumerated()), id: \.offset) { _, b in BookingRow(booking: b) } }
                    }
                    if let s = trip?.planUrl, let u = URL(string: s) {
                        Button { openURL(u) } label: { Label("Edit in TREK", systemImage: "arrow.up.forward.app").frame(maxWidth: .infinity, minHeight: 44) }
                            .buttonStyle(.bordered).tint(Theme.accent)
                    }
                } else if trip == nil { LoadingCard() }
                else { EmptyCard(symbol: "calendar", title: "This day isn't in the trip any more") }
            }
            .screen()
        }
        .background(Backdrop())
        .navigationTitle(day.map { "Day \($0.dayNumber)" } ?? "Day")
        .navigationBarTitleDisplayMode(.inline)
        .task { trip = try? await TripStore.shared.detail(tripID) }
    }
}

struct StopRow: View {
    let number: Int
    let place: Trip.Place
    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Text(TripDate.time(place.time) ?? "").font(.caption.monospacedDigit()).foregroundStyle(Theme.dim)
                .frame(width: 62, alignment: .leading).padding(.top, 3)
            Text("\(number)").font(.caption.weight(.bold)).foregroundStyle(Theme.onAccent)
                .frame(width: 22, height: 22).background(Theme.accent, in: Circle())
            VStack(alignment: .leading, spacing: 2) {
                Text(place.name).font(.subheadline.weight(.semibold))
                if let n = place.notes, !n.isEmpty { Text(n).font(.caption).foregroundStyle(Theme.dim) }
                if let a = place.address, !a.isEmpty { Text(a).font(.caption).foregroundStyle(Theme.dim).lineLimit(1) }
            }
            Spacer(minLength: 0)
            if let dest = Directions.destination(name: place.name, lat: place.lat, lng: place.lng, address: place.address) {
                Button { Directions.open(dest) } label: {
                    Image(systemName: "arrow.triangle.turn.up.right.circle.fill").font(.title2).foregroundStyle(Theme.accent)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Directions to \(place.name)")
            }
        }
        .padding(.vertical, 10).padding(.horizontal, 10)
    }
}

/// Hands a place to Apple Maps for directions.
enum Directions {
    struct Destination { var name: String?; var coordinate: CLLocationCoordinate2D?; var address: String? }

    static func destination(name: String?, lat: Double?, lng: Double?, address: String?) -> Destination? {
        if let lat, let lng { return Destination(name: name, coordinate: .init(latitude: lat, longitude: lng), address: address) }
        if let address, !address.isEmpty { return Destination(name: name, coordinate: nil, address: address) }
        return nil
    }

    @MainActor static func open(_ d: Destination) {
        if let c = d.coordinate {
            let item = MKMapItem(placemark: MKPlacemark(coordinate: c))
            item.name = d.name
            item.openInMaps(launchOptions: [MKLaunchOptionsDirectionsModeKey: MKLaunchOptionsDirectionsModeDefault])
        } else if let a = d.address?.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed),
                  let url = URL(string: "maps://?daddr=\(a)") {
            UIApplication.shared.open(url)
        }
    }
}

// MARK: - Home cards

/// Grown-ups' Home: the next trip once it's three weeks out, and today's plan while it's on.
struct NextTripCard: View {
    @Environment(Router.self) private var router
    @Environment(HomeStore.self) private var store
    private let trips = TripStore.shared
    @State private var today: TripDay?

    var body: some View {
        Group {
            if let t = trips.list?.next, t.isOn || (t.daysUntil ?? 99) <= 21 {
                if store.role == .tablet {
                    // The kitchen tablet only gets the countdown.
                    SectionHeader(t.isOn ? "On the trip" : "Next trip")
                    content(t)
                } else {
                    SectionHeader(title: t.isOn ? "On the trip" : "Next trip") { MoreButton("All trips") { router.show(.family, .trips) } }
                    Button {
                        if t.isOn, let d = today { router.go(.tripDay(t.id, d.date)) } else { router.go(.trip(t.id)) }
                    } label: { content(t) }
                    .buttonStyle(.plain)
                    .task(id: t.id) { if t.isOn { await loadToday(t) } }
                }
            }
        }
        // The list itself is loaded by HomeView (an empty Group never runs a task).
    }

    private func content(_ t: Trip) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text(t.isOn ? (today.map { "Today: \($0.name)" } ?? t.title) : t.title).font(.headline).foregroundStyle(Theme.fg)
                    Text(t.isOn ? "\(t.title) · \(t.countdown)" : t.range).font(.caption).foregroundStyle(Theme.dim)
                }
                Spacer()
                if let tr = t.travellers, !tr.isEmpty { TravellerFaces(travellers: tr) }
            }
            if t.isOn, let d = today {
                ForEach(Array(d.places.prefix(3).enumerated()), id: \.offset) { _, p in
                    HStack(spacing: 10) {
                        Text(TripDate.time(p.time) ?? "").font(.caption.monospacedDigit()).foregroundStyle(Theme.dim).frame(width: 64, alignment: .leading)
                        Text(p.name).font(.subheadline).foregroundStyle(Theme.fg)
                    }
                }
                if d.places.count > 3 { Text("+\(d.places.count - 3) more").font(.caption).foregroundStyle(Theme.dim) }
            } else if let n = t.daysUntil {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text("\(n)").font(.system(size: 34, weight: .bold, design: .rounded)).monospacedDigit().foregroundStyle(Theme.fg)
                    Text(n == 1 ? "day to go" : "days to go").foregroundStyle(Theme.dim)
                }
            }
        }
        .card()
    }

    private func loadToday(_ t: Trip) async {
        guard let d = try? await TripStore.shared.detail(t.id) else { return }
        today = d.days?.first { $0.date == d.today }
    }
}

/// The kids' Home: a countdown in their colour, then what each day holds. Juni gets sleeps to count.
struct KidTripCard: View {
    let little: Bool
    let color: Color
    @Environment(Router.self) private var router
    private let trips = TripStore.shared
    @State private var detail: Trip?

    init(little: Bool, color: Color) { self.little = little; self.color = color }

    var body: some View {
        Group {
            if let t = trips.list?.next, t.isOn || (t.daysUntil ?? 99) <= 21 {
                VStack(alignment: .leading, spacing: 12) {
                    hero(t)
                    if let days = detail?.days, !days.isEmpty { dayList(t, days) }
                }
                .task(id: t.id) { detail = try? await TripStore.shared.detail(t.id) }
            }
        }
        // The list itself is loaded by KidHomeView (an empty Group never runs a task).
    }

    private var spoken: String {
        guard let t = trips.list?.next else { return "" }
        if t.isOn { return "Today is day \(t.dayNumber ?? 1) of our trip." }
        let n = t.daysUntil ?? 0
        return "\(n) \(n == 1 ? "sleep" : "sleeps") until \(t.title)!"
    }

    private func hero(_ t: Trip) -> some View {
        let todayDay = detail?.days?.first { $0.date == detail?.today }
        return VStack(alignment: little ? .center : .leading, spacing: 8) {
            if t.isOn {
                Text("Today on our trip").font(Theme.kidFont(18, .semibold)).opacity(0.9)
                Text("\(TripArt.emoji(todayDay?.name)) \(todayDay?.name ?? t.title)").font(Theme.kidFont(little ? 32 : 30)).multilineTextAlignment(little ? .center : .leading)
                Text("Day \(t.dayNumber ?? 1) of \(t.dayCount ?? 1)").font(Theme.kidFont(17, .semibold)).opacity(0.85)
            } else {
                let n = t.daysUntil ?? 0
                if little { Text(TripArt.emoji(t.title)).font(.system(size: 56)) }
                Text(little ? "\(n) \(n == 1 ? "sleep" : "sleeps")" : "\(n) \(n == 1 ? "day" : "days")").font(Theme.kidFont(46, .heavy))
                Text("until \(t.title)").font(Theme.kidFont(18, .semibold)).multilineTextAlignment(little ? .center : .leading)
                if little, n > 0, n <= 21 {
                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 28), spacing: 6)], spacing: 6) {
                        ForEach(0..<n, id: \.self) { _ in Text("🌙").font(.system(size: 15)).frame(width: 28, height: 28).background(.white.opacity(0.22), in: Circle()) }
                    }
                }
            }
        }
        .foregroundStyle(.white)
        .frame(maxWidth: .infinity, alignment: little ? .center : .leading)
        .padding(18)
        .background(color, in: RoundedRectangle(cornerRadius: 26, style: .continuous))
        .overlay(alignment: .topTrailing) { SayButton(text: spoken).foregroundStyle(.white).padding(8) }
        .onTapGesture { router.go(.trip(t.id)) }
        .accessibilityAddTraits(.isButton)
    }

    @ViewBuilder private func dayList(_ t: Trip, _ days: [TripDay]) -> some View {
        if little {
            LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible(), spacing: 10)], spacing: 10) {
                let names: [String] = t.isOn ? (days.first { $0.date == detail?.today }?.places ?? []).prefix(4).map(\.name) : days.map(\.name)
                ForEach(Array(names.enumerated()), id: \.offset) { _, name in
                    VStack(spacing: 4) {
                        Text(TripArt.emoji(name)).font(.system(size: 44))
                        Text(name).font(Theme.kidFont(15)).lineLimit(1).minimumScaleFactor(0.7)
                    }
                    .frame(maxWidth: .infinity).padding(.vertical, 14)
                    .background(Theme.card, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
                    .onTapGesture { Speaker.say(name) }
                }
            }
        } else {
            SectionHeader(title: t.isOn ? "Today" : "The plan") { MoreButton("Whole trip") { router.go(.trip(t.id)) } }
            let rows: [(String, String, String?)] = t.isOn
                ? (days.first { $0.date == detail?.today }?.places ?? []).map { (TripArt.emoji($0.name), $0.name, TripDate.time($0.time)) }
                : days.map { (TripArt.emoji($0.name), $0.name, $0.day?.formatted(.dateTime.weekday(.wide))) }
            ForEach(Array(rows.enumerated()), id: \.offset) { _, r in
                HStack(spacing: 12) {
                    Text(r.0).font(.system(size: 28)).frame(width: 52, height: 52).background(Theme.card2, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                    VStack(alignment: .leading, spacing: 1) {
                        Text(r.1).font(Theme.kidFont(17, .semibold))
                        if let s = r.2 { Text(s).font(.caption).foregroundStyle(Theme.dim) }
                    }
                    Spacer()
                }
                .card(padding: 10)
            }
        }
    }
}

// MARK: - Offline

/// "Kept for offline" on a trip: when it was saved, and (for adults) a switch.
struct TripOfflineRow: View {
    @Environment(HomeStore.self) private var store
    let trip: Trip
    let savedAt: Date?
    private let trips = TripStore.shared

    var body: some View {
        let kept = trips.isKept(trip.id)
        HStack(spacing: 12) {
            IconBadge(symbol: kept ? "arrow.down.circle.fill" : "icloud", color: kept ? Theme.accent : Theme.dim, size: 34)
            VStack(alignment: .leading, spacing: 2) {
                Text(kept ? "Kept for offline" : "Not kept offline").font(.subheadline.weight(.semibold))
                Text(detail(kept)).font(.caption).foregroundStyle(Theme.dim)
            }
            Spacer()
            if store.role == .admin || store.role == .adult {
                Toggle("Keep offline", isOn: Binding(get: { kept }, set: { trips.setKept(trip.id, $0) })).labelsHidden()
            }
        }
        .card(padding: 12)
    }

    private func detail(_ kept: Bool) -> String {
        let when = savedAt.map { "Saved \(Freshness.text($0))" } ?? "Up to date"
        if !kept { return store.reachable ? "Turn on to take it on the road" : when }
        return TripStore.autoKeep(trip) && !AppSettings.offlineTripsOn.contains(trip.id)
            ? "\(when) · kept by itself while it's close" : when
    }
}

/// Each day's map drawn once as a picture, for when there's no signal.
enum TripMapPicture {
    static func name(_ trip: Int, _ date: String) -> String { "trip-\(trip)-\(date).jpg" }

    static func load(trip: Int, date: String) -> UIImage? {
        OfflineCache.loadFile(name(trip, date), kind: .kept, category: .trips).flatMap(UIImage.init(data:))
    }

    static func remove(trip: Int) {
        let dir = OfflineCache.dir(.kept, OfflineCache.Category.trips.rawValue)
        for f in (try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil)) ?? []
        where f.lastPathComponent.hasPrefix("trip-\(trip)-") { try? FileManager.default.removeItem(at: f) }
    }

    @MainActor
    static func make(trip: Int, day: TripDay) async {
        let coords = day.places.compactMap(\.coordinate)
        guard !coords.isEmpty else { return }
        let o = MKMapSnapshotter.Options()
        var rect = MKMapRect.null
        for c in coords { let p = MKMapPoint(c); rect = rect.union(MKMapRect(x: p.x, y: p.y, width: 1, height: 1)) }
        let pad = max(rect.size.width, rect.size.height) * 0.25 + 2000
        o.mapRect = rect.insetBy(dx: -pad, dy: -pad)
        o.size = CGSize(width: 390, height: 220)
        o.scale = 3
        guard let shot = try? await MKMapSnapshotter(options: o).start() else { return }
        let r = UIGraphicsImageRenderer(size: o.size)
        let img = r.image { ctx in
            shot.image.draw(at: .zero)
            let pts = coords.map(shot.point(for:))
            let tint = UIColor(Theme.accent)
            if pts.count > 1 {
                let path = UIBezierPath(); path.move(to: pts[0]); pts.dropFirst().forEach { path.addLine(to: $0) }
                path.lineWidth = 3; path.setLineDash([6, 5], count: 2, phase: 0); tint.setStroke(); path.stroke()
            }
            for (i, p) in pts.enumerated() {
                let dot = CGRect(x: p.x - 11, y: p.y - 11, width: 22, height: 22)
                tint.setFill(); UIBezierPath(ovalIn: dot).fill()
                let s = NSAttributedString(string: "\(i + 1)", attributes: [.font: UIFont.boldSystemFont(ofSize: 12), .foregroundColor: UIColor.white])
                let sz = s.size(); s.draw(at: CGPoint(x: p.x - sz.width / 2, y: p.y - sz.height / 2))
            }
            _ = ctx
        }
        if let d = img.jpegData(compressionQuality: 0.8) {
            OfflineCache.storeFile(d, name: name(trip, day.date), kind: .kept, category: .trips)
        }
    }
}
