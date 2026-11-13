import SwiftUI
import PhotosUI
import Vision

// MARK: - Models

struct Oven: Codable, Hashable, Identifiable {
    var id: String
    var name: String
    var on: Bool
    var available: Bool
    var remoteReady: Bool
    var mode: String?
    var modes: [String]
    var currentTemp: Double?
    var targetTemp: Double?
    var minTemp: Double?
    var maxTemp: Double?
    var session: Session?
    struct Session: Codable, Hashable {
        var title: String?
        var timerMin: Int?
        var timerFrom: String?
        var autoOff: Bool?
        var preheated: Bool?
        var timerEndsAt: String?
    }
    var statusLine: String {
        if !available { return "Not connected" }
        guard on else { return remoteReady ? "Off · ready for the app" : "Off" }
        let cur = currentTemp.map { "\(Int($0))°" } ?? ""
        if let t = targetTemp, let c = currentTemp, c < t - 5 { return "\(mode ?? "On") · \(cur) heating to \(Int(t))°" }
        return [mode ?? "On", targetTemp.map { "\(Int($0))°" }].compactMap { $0 }.joined(separator: " · ")
    }
}

struct OvenPreset: Codable, Hashable, Identifiable { var id: String; var title: String; var detail: String; var mode: String; var temp: Int? }

struct KitchenSummary: Codable {
    var ovens: [Oven]
    var presets: [OvenPreset]
    var dinner: KitchenDinner?
    struct KitchenDinner: Codable, Hashable { var title: String; var slug: String?; var imageUrl: String?; var oven: OvenPlan? }
}

struct RecipeSummary: Codable, Hashable, Identifiable {
    var id: String?
    var slug: String
    var title: String?
    var imageUrl: String?
    var totalTime: String?
    var tags: [String]?
    var favorite: Bool?
    var oven: Bool?
    var lastMade: String?
}

struct RecipeList: Codable { var items: [RecipeSummary]; var total: Int? }
struct TagList: Codable { var tags: [Tag]; struct Tag: Codable, Hashable, Identifiable { var id: String; var name: String; var slug: String } }

struct PlanEntry: Codable, Hashable {
    var id: Int?
    var date: String?
    var meal: String?
    var title: String?
    var slug: String?
    var recipeId: String?
    var imageUrl: String?
    var totalTime: String?
    var note: String?
}
struct PlanDay: Codable, Hashable, Identifiable { var date: String; var dinner: PlanEntry?; var id: String { date } }
struct PlanReply: Codable { var days: [PlanDay] }
struct SavedRecipe: Codable { var ok: Bool?; var slug: String?; var recipe: Recipe?; var dryRun: Bool? }

struct RecipeDraft: Codable, Hashable {
    var title: String = ""
    var ingredients: [String] = []
    var steps: [String] = []
    var oven: OvenPlan?
    var servings: String? = nil
    var prepTime: String? = nil
    var cookTime: String? = nil
    var tags: [String]? = nil
}

enum OvenModes {
    static let common = ["Bake", "Convection Bake", "Convection Roast", "Conv. Multi-Bake", "Broil High", "Broil Low",
                         "Air Fry", "Warm", "Proof", "Frozen Pizza"]
    static func usesTemp(_ m: String) -> Bool { !["Broil High", "Broil Low", "Proof"].contains(m) }
}

// MARK: - Kitchen (ovens)

struct KitchenView: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    var focus: String? = nil
    @State private var data: KitchenSummary?
    @State private var ovenID: String?
    @State private var mode = "Bake"
    @State private var temp = 350
    @State private var timer = 0
    @State private var autoOff = false
    @State private var busy = false
    @State private var error: String?

    private var oven: Oven? { data?.ovens.first { $0.id == ovenID } ?? data?.ovens.first }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if let data {
                    if data.ovens.isEmpty {
                        EmptyCard(symbol: "oven", title: "No ovens found", detail: "The hub didn't find an oven in Home Assistant.")
                    } else {
                        if data.ovens.count > 1 {
                            Picker("Oven", selection: Binding(get: { oven?.id ?? "" }, set: { ovenID = $0 })) {
                                ForEach(data.ovens) { Text($0.name).tag($0.id) }
                            }
                            .pickerStyle(.segmented)
                        }
                        if let o = oven { status(o) }
                        if let d = data.dinner, let p = d.oven { dinnerCard(d, p) }
                        SectionHeader("Quick start")
                        LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 10) {
                            ForEach(data.presets) { p in
                                Button { mode = p.mode; if let t = p.temp { temp = t } } label: {
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(p.title).font(.headline)
                                        Text(p.detail).font(.caption).foregroundStyle(Theme.dim)
                                    }
                                    .frame(maxWidth: .infinity, alignment: .leading).padding(12)
                                    .background(Theme.card, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                                    .overlay(RoundedRectangle(cornerRadius: 14).stroke(Theme.accent, lineWidth: selected(p) ? 2 : 0))
                                }
                                .buttonStyle(.plain)
                            }
                        }
                        SectionHeader("Or set it")
                        VStack(spacing: 0) {
                            HStack {
                                Text("Mode"); Spacer()
                                Picker("Mode", selection: $mode) { ForEach(oven?.modes ?? OvenModes.common, id: \.self) { Text($0) } }
                            }
                            .padding(.vertical, 6)
                            Divider()
                            if OvenModes.usesTemp(mode) {
                                Stepper(value: $temp, in: (mode == "Warm" ? 140 : 170)...550, step: 25) {
                                    HStack { Text("Temperature"); Spacer(); Text("\(temp)°").font(.headline) }
                                }
                                .padding(.vertical, 6)
                                Divider()
                            }
                            Stepper(value: $timer, in: 0...360, step: 5) {
                                HStack { Text("Timer"); Spacer(); Text(timer == 0 ? "Off" : "\(timer) min").font(.headline) }
                            }
                            .padding(.vertical, 6)
                            if timer > 0 {
                                Divider()
                                Toggle("Turn off when the timer ends", isOn: $autoOff).padding(.vertical, 6)
                            }
                        }
                        .card(padding: 14)
                        if let o = oven, o.available, !o.remoteReady, !o.on { remoteNote }
                        PrimaryButton(title: startTitle, symbol: "flame.fill") { start() }
                            .disabled(busy || oven?.available != true)
                        Text("Starting an oven asks for Face ID.").font(.footnote).foregroundStyle(Theme.dim)
                    }
                } else if let error {
                    EmptyCard(symbol: "wifi.exclamationmark", title: "Kitchen isn't answering", detail: error)
                } else { LoadingCard() }
            }
            .screen()
        }
        .background(Backdrop())
        .navigationTitle("Kitchen")
        .onAppear { CatDirector.shared.perform(.kikiFood, chance: 0.25) }
        .toolbar { ToolbarItem(placement: .topBarTrailing) { Button { router.go(.dinners) } label: { Label("Dinners", systemImage: "fork.knife") } } }
        .task { if let focus { ovenID = focus }; await load() }
        .refreshable { await load() }
        .task(id: oven?.on) {
            // Keep the temperature moving while an oven heats.
            while oven?.on == true { try? await Task.sleep(for: .seconds(15)); await load() }
        }
    }

    private func selected(_ p: OvenPreset) -> Bool { p.mode == mode && (p.temp == nil || p.temp == temp) }
    private var startTitle: String {
        var s = "Start \((oven?.name ?? "oven").lowercased()) · \(mode)"
        if OvenModes.usesTemp(mode) { s += " \(temp)°" }
        if timer > 0 { s += " · \(timer) min" }
        return s
    }

    private var remoteNote: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "hand.tap.fill").foregroundStyle(Theme.warn)
            VStack(alignment: .leading, spacing: 2) {
                Text("Press Remote Enable on the oven").font(.subheadline.weight(.semibold))
                Text("GE ovens need it once before an app can start them. It stays on until that cooking cycle ends.")
                    .font(.footnote).foregroundStyle(Theme.dim)
            }
        }
        .card(padding: 12, color: Theme.warn.opacity(0.14))
    }

    @ViewBuilder private func status(_ o: Oven) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Label(o.name, systemImage: "oven.fill").font(.headline)
                Spacer()
                Text(o.on ? (o.session?.preheated == true || (o.currentTemp ?? 0) >= (o.targetTemp ?? 1) - 5 ? "Ready" : "Heating") : "Off")
                    .font(.subheadline.weight(.semibold)).foregroundStyle(o.on ? Theme.warn : Theme.dim)
            }
            if o.on {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(o.currentTemp.map { "\(Int($0))°" } ?? "–").font(.system(size: 40, weight: .semibold, design: .rounded))
                    if let t = o.targetTemp { Text("heating to \(Int(t))° · \(o.mode ?? "")").foregroundStyle(Theme.dim) }
                }
                if let t = o.targetTemp, let c = o.currentTemp { ProgressBar(value: c / max(t, 1), color: Theme.warn) }
                if let ends = o.session?.timerEndsAt.flatMap(ISO.parse) {
                    Label { Text(ends, style: .timer) } icon: { Image(systemName: "timer") }.font(.headline)
                } else if let m = o.session?.timerMin {
                    Button { setTimer(o, m) } label: { Label("It's in, start the \(m) min timer", systemImage: "timer") }
                        .buttonStyle(.bordered)
                }
                Button(role: .destructive) { turnOff(o) } label: { Label("Turn off", systemImage: "power").frame(maxWidth: .infinity) }
                    .buttonStyle(.bordered).tint(Theme.crit)
            } else {
                Text(o.statusLine).foregroundStyle(Theme.dim)
            }
        }
        .card()
    }

    private func dinnerCard(_ d: KitchenSummary.KitchenDinner, _ p: OvenPlan) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("TONIGHT").font(.caption.weight(.bold)).foregroundStyle(Theme.accent)
            Text(d.title).font(.headline)
            Text("Oven: \(p.summary)").foregroundStyle(Theme.dim)
            HStack {
                Button { mode = p.mode; if let t = p.temp { temp = t }; timer = p.minutes ?? 0 } label: { Text("Use this") }
                    .buttonStyle(.borderedProminent)
                if let s = d.slug { Button("Recipe") { router.go(.recipe(s)) }.buttonStyle(.bordered) }
            }
        }
        .card()
    }

    private func load() async {
        do { data = try await HubClient.shared.get("/v1/kitchen"); error = nil }
        catch { self.error = error.localizedDescription }
    }

    private func start() {
        guard let o = oven else { return }
        var params: [String: Any] = ["mode": mode]
        if OvenModes.usesTemp(mode) { params["temp"] = temp }
        if timer > 0 { params["timer_min"] = timer; params["timer_from"] = "now"; params["auto_off"] = autoOff }
        run("oven.set", o, params, confirm: "Start the \(o.name.lowercased())")
    }

    private func turnOff(_ o: Oven) { run("oven.set", o, ["on": false], confirm: nil) }
    private func setTimer(_ o: Oven, _ m: Int) { run("oven.timer", o, ["minutes": m], confirm: nil) }

    private func run(_ action: String, _ o: Oven, _ params: [String: Any], confirm: String?) {
        busy = true
        Task {
            defer { busy = false }
            do {
                let r = try await HubClient.shared.action(action, target: o.id, params: params, confirm: confirm)
                store.show(r.dryRun == true ? "Practice mode" : (r.message ?? "Done"))
            } catch HubClient.HubError.cancelled {
            } catch { store.show(error.localizedDescription, isError: true) }
            await load()
        }
    }
}

/// Set the oven straight from a recipe.
struct OvenFromRecipeSheet: View {
    @Environment(HomeStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let plan: OvenPlan
    let recipeTitle: String
    @State private var ovens: [Oven] = []
    @State private var ovenID: String?
    @State private var timerFrom = "manual"
    @State private var autoOff = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    LabeledContent("Mode", value: plan.mode)
                    if let t = plan.temp { LabeledContent("Temperature", value: "\(t)°") }
                    if let m = plan.minutes { LabeledContent("Timer", value: "\(m) min") }
                } footer: { if let q = plan.quote { Text("“\(q)”") } }
                if ovens.count > 1 {
                    Picker("Oven", selection: Binding(get: { ovenID ?? ovens.first?.id ?? "" }, set: { ovenID = $0 })) {
                        ForEach(ovens) { Text($0.name).tag($0.id) }
                    }
                }
                if plan.minutes != nil {
                    Section {
                        Picker("Start the timer", selection: $timerFrom) {
                            Text("When I say it's in").tag("manual")
                            Text("When it's preheated").tag("preheat")
                            Text("Now").tag("now")
                        }
                        Toggle("Turn off when the timer ends", isOn: $autoOff)
                    } footer: { Text("You'll get a notice when the oven is hot, with a button to start the timer.") }
                }
                if let o = selected, o.available, !o.remoteReady {
                    Section { Label("Press Remote Enable on the oven first.", systemImage: "hand.tap.fill").foregroundStyle(Theme.warn) }
                }
            }
            .navigationTitle("Set oven")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) { Button("Start") { start() }.disabled(selected == nil) }
            }
            .task {
                let k: KitchenSummary? = try? await HubClient.shared.get("/v1/kitchen")
                ovens = k?.ovens ?? []
                ovenID = ovens.first { !$0.on }?.id ?? ovens.first?.id
            }
        }
    }

    private var selected: Oven? { ovens.first { $0.id == ovenID } ?? ovens.first }

    private func start() {
        guard let o = selected else { return }
        var params: [String: Any] = ["mode": plan.mode, "recipe": recipeTitle]
        if let t = plan.temp { params["temp"] = t }
        if let m = plan.minutes { params["timer_min"] = m; params["timer_from"] = timerFrom; params["auto_off"] = autoOff }
        Task {
            do {
                let r = try await HubClient.shared.action("oven.set", target: o.id, params: params, confirm: "Start the \(o.name.lowercased())")
                store.show(r.dryRun == true ? "Practice mode" : (r.message ?? "Oven on"))
                dismiss()
            } catch HubClient.HubError.cancelled {
            } catch { store.show(error.localizedDescription, isError: true) }
        }
    }
}

// MARK: - Dinners (the week's plan)

struct DinnersView: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    @State private var days: [PlanDay]?
    @State private var picking: PlanDay?
    @State private var adding = false
    @State private var busy = false
    @State private var error: String?

    private var canEdit: Bool { store.me?.capabilities.contains("family.edit") ?? false }

    var body: some View {
        List {
            if canEdit {
                Section {
                    HStack(spacing: 10) {
                        Button { fill() } label: { Label("Fill the week", systemImage: "wand.and.stars").frame(maxWidth: .infinity) }
                            .buttonStyle(.borderedProminent).disabled(busy)
                        Button { router.go(.recipes) } label: { Label("Recipes", systemImage: "book.fill").frame(maxWidth: .infinity) }
                            .buttonStyle(.bordered)
                    }
                    .listRowBackground(Color.clear).listRowInsets(EdgeInsets())
                }
            }
            if let days {
                Section("This week") {
                    ForEach(days) { d in row(d) }
                }
            } else if let error {
                EmptyCard(symbol: "fork.knife", title: "Meals aren't answering", detail: error).listRowBackground(Color.clear)
            } else { ProgressView() }
        }
        .scrollContentBackground(.hidden).background(Backdrop())
        .navigationTitle("Dinners")
        .onAppear { CatDirector.shared.perform(.kikiFood, chance: 0.25) }
        .toolbar {
            if canEdit { ToolbarItem(placement: .topBarTrailing) { Button { adding = true } label: { Image(systemName: "plus") } } }
        }
        .task { await load() }
        .refreshable { await load() }
        .sheet(item: $picking) { d in
            RecipePicker(title: dayName(d.date, long: true)) { choice in
                Task { await set(d.date, choice) }
            }
        }
        .sheet(isPresented: $adding) { AddRecipeSheet { slug in router.go(.recipe(slug)) } }
    }

    @ViewBuilder private func row(_ d: PlanDay) -> some View {
        let isToday = d.date == ISO.day(Date())
        Button {
            if let s = d.dinner?.slug { router.go(.recipe(s)) } else if canEdit { picking = d }
        } label: {
            HStack(spacing: 12) {
                VStack(spacing: 0) {
                    Text(dayName(d.date, long: false).uppercased()).font(.caption2.weight(.bold)).foregroundStyle(isToday ? Theme.accent : Theme.dim)
                    Text(ISO.parseDay(d.date).map { "\(Calendar.current.component(.day, from: $0))" } ?? "").font(.title3.weight(.semibold))
                }
                .frame(width: 40)
                if let e = d.dinner {
                    HubImage(path: e.imageUrl) { Theme.card2.overlay(Image(systemName: "fork.knife").foregroundStyle(Theme.dim)) }
                        .frame(width: 48, height: 48).clipShape(RoundedRectangle(cornerRadius: 10))
                    VStack(alignment: .leading, spacing: 2) {
                        Text(e.title ?? "Dinner").font(.body.weight(.medium)).foregroundStyle(Theme.fg)
                        if let t = e.totalTime { Text(t).font(.caption).foregroundStyle(Theme.dim) }
                    }
                } else {
                    Text(canEdit ? "+ Plan dinner" : "Nothing planned").foregroundStyle(canEdit ? Theme.accent : Theme.dim)
                }
                Spacer()
            }
        }
        .swipeActions(edge: .trailing) {
            if canEdit, d.dinner != nil {
                Button(role: .destructive) { Task { await set(d.date, .clear) } } label: { Label("Clear", systemImage: "xmark") }
                Button { picking = d } label: { Label("Swap", systemImage: "arrow.triangle.2.circlepath") }.tint(Theme.accent)
            }
        }
        .contextMenu {
            if canEdit {
                Button { picking = d } label: { Label(d.dinner == nil ? "Plan dinner" : "Swap", systemImage: "arrow.triangle.2.circlepath") }
                Button { Task { await set(d.date, .note("Eating out")) } } label: { Label("Eating out", systemImage: "takeoutbag.and.cup.and.straw") }
                Button { Task { await set(d.date, .note("Leftovers")) } } label: { Label("Leftovers", systemImage: "refrigerator") }
                if d.dinner != nil { Button(role: .destructive) { Task { await set(d.date, .clear) } } label: { Label("Clear", systemImage: "xmark") } }
            }
        }
    }

    private func dayName(_ s: String, long: Bool) -> String {
        guard let d = ISO.parseDay(s) else { return s }
        if Calendar.current.isDateInToday(d) { return long ? "Tonight" : "Today" }
        return d.formatted(.dateTime.weekday(long ? .wide : .abbreviated))
    }

    private func load() async {
        do { days = (try await HubClient.shared.get("/v1/meals/plan") as PlanReply).days; error = nil }
        catch { self.error = error.localizedDescription }
    }

    private func set(_ date: String, _ c: RecipePicker.Choice) async {
        var body: [String: Any] = [:]
        switch c {
        case .recipe(let slug): body["slug"] = slug
        case .note(let t): body["title"] = t
        case .clear: break
        }
        do {
            let _: Empty = try await HubClient.shared.call("PUT", "/v1/meals/plan/\(date)", body: body)
            await load(); await store.loadHome()
        } catch { store.show(error.localizedDescription, isError: true) }
    }

    private func fill() {
        busy = true
        Task {
            defer { busy = false }
            do {
                struct R: Decodable { var filled: Int?; var days: [PlanDay]? }
                let r: R = try await HubClient.shared.post("/v1/meals/plan/fill")
                if let d = r.days { days = d }
                store.show((r.filled ?? 0) == 0 ? "The week is already planned" : "Planned \(r.filled!) dinners")
                await store.loadHome()
            } catch { store.show(error.localizedDescription, isError: true) }
        }
    }
}

/// Pick a recipe for a night (or say "Eating out").
struct RecipePicker: View {
    enum Choice { case recipe(String), note(String), clear }
    @Environment(\.dismiss) private var dismiss
    let title: String
    let onPick: (Choice) -> Void
    @State private var search = ""
    @State private var items: [RecipeSummary] = []
    @State private var suggested: [RecipeSummary] = []

    var body: some View {
        NavigationStack {
            List {
                if search.isEmpty, !suggested.isEmpty {
                    Section("Not made lately") { ForEach(suggested) { pick($0) } }
                }
                Section(search.isEmpty ? "All recipes" : "Matches") { ForEach(items) { pick($0) } }
                Section {
                    Button { onPick(.note("Eating out")); dismiss() } label: { Label("Eating out", systemImage: "takeoutbag.and.cup.and.straw") }
                    Button { onPick(.note("Leftovers")); dismiss() } label: { Label("Leftovers", systemImage: "refrigerator") }
                }
            }
            .searchable(text: $search, prompt: "Search recipes")
            .navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } } }
            .task(id: search) {
                try? await Task.sleep(for: .milliseconds(250))
                items = ((try? await HubClient.shared.get("/v1/meals/recipes", search.isEmpty ? [:] : ["search": search])) as RecipeList?)?.items ?? []
            }
            .task { suggested = ((try? await HubClient.shared.get("/v1/meals/suggest", ["n": "5"])) as RecipeList?)?.items ?? [] }
        }
    }

    private func pick(_ r: RecipeSummary) -> some View {
        Button { onPick(.recipe(r.slug)); dismiss() } label: { RecipeRow(r: r) }
    }
}

struct RecipeRow: View {
    let r: RecipeSummary
    var body: some View {
        HStack(spacing: 12) {
            HubImage(path: r.imageUrl) { Theme.card2.overlay(Image(systemName: "fork.knife").foregroundStyle(Theme.dim)) }
                .frame(width: 44, height: 44).clipShape(RoundedRectangle(cornerRadius: 9))
            VStack(alignment: .leading, spacing: 2) {
                Text(r.title ?? r.slug).foregroundStyle(Theme.fg)
                Text([r.totalTime, r.oven == true ? "oven" : nil, r.favorite == true ? "♥" : nil, SettingsStore.shared.isStarred(.recipe, r.slug) ? "★" : nil].compactMap { $0 }.joined(separator: " · "))
                    .font(.caption).foregroundStyle(Theme.dim)
            }
        }
    }
}

// MARK: - Recipes library

struct RecipesView: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    @State private var search = ""
    @State private var tag: String?
    @State private var items: [RecipeSummary]?
    @State private var total = 0
    @State private var tags: [TagList.Tag] = []
    @State private var adding = false

    private var canEdit: Bool { store.me?.capabilities.contains("family.edit") ?? false }
    /// Not a Mealie tag: the person's own starred recipes, kept on the hub.
    static let mine = "__mine"

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 8) {
                        ChipButton(title: "All", active: tag == nil) { tag = nil }
                        if SettingsStore.shared.anyFavorites(.recipe) {
                            ChipButton(title: "My favorites", symbol: "star.fill", active: tag == Self.mine) { tag = Self.mine }
                        }
                        ChipButton(title: "Family favorites", symbol: "heart.fill", active: tag == "favorite") { tag = "favorite" }
                        ChipButton(title: "Uses the oven", symbol: "oven", active: tag == "oven") { tag = "oven" }
                        ForEach(tags.filter { !["favorite", "oven"].contains($0.slug) }) { t in
                            ChipButton(title: t.name, active: tag == t.slug) { tag = t.slug }
                        }
                    }
                }
                if let items {
                    if items.isEmpty {
                        EmptyCard(symbol: "book", title: search.isEmpty && tag == nil ? "No recipes yet" : "Nothing matches",
                                  detail: canEdit ? "Tap + to add one from a link, a cookbook page, or by typing." : nil)
                    }
                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), spacing: 10)], spacing: 10) {
                        ForEach(items) { r in
                            Button { router.go(.recipe(r.slug)) } label: {
                                VStack(alignment: .leading, spacing: 0) {
                                    HubImage(path: r.imageUrl) { Theme.card2.overlay(Image(systemName: "fork.knife").font(.title2).foregroundStyle(Theme.dim)) }
                                        .frame(height: 100).frame(maxWidth: .infinity).clipped()
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(r.title ?? r.slug).font(.subheadline.weight(.semibold)).lineLimit(2).foregroundStyle(Theme.fg)
                                        Text([r.totalTime, r.oven == true ? "oven" : nil].compactMap { $0 }.joined(separator: " · "))
                                            .font(.caption).foregroundStyle(Theme.dim)
                                    }
                                    .padding(10)
                                }
                                .background(Theme.card, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                                .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
                            }
                            .buttonStyle(.plain)
                        }
                    }
                } else { LoadingCard() }
            }
            .screen()
        }
        .background(Backdrop())
        .navigationTitle("Recipes")
        .searchable(text: $search, prompt: total > 0 ? "Search \(total) recipes" : "Search recipes")
        .toolbar { if canEdit { ToolbarItem(placement: .topBarTrailing) { Button { adding = true } label: { Image(systemName: "plus") } } } }
        .task(id: "\(search)|\(tag ?? "")") {
            if tag == Self.mine {
                let mine = SettingsStore.shared.favorites(.recipe).map { RecipeSummary(slug: $0.id, title: $0.title, imageUrl: $0.image) }
                items = search.isEmpty ? mine : mine.filter { ($0.title ?? $0.slug).localizedCaseInsensitiveContains(search) }
                return
            }
            try? await Task.sleep(for: .milliseconds(250))
            var q: [String: String] = [:]
            if !search.isEmpty { q["search"] = search }
            if let tag { q["tag"] = tag }
            if let r: RecipeList = try? await HubClient.shared.get("/v1/meals/recipes", q) { items = r.items; if q.isEmpty { total = r.total ?? r.items.count } }
            else if items == nil { items = [] }
        }
        .task { tags = ((try? await HubClient.shared.get("/v1/meals/tags")) as TagList?)?.tags ?? [] }
        .sheet(isPresented: $adding) { AddRecipeSheet { slug in router.go(.recipe(slug)) } }
    }
}

// MARK: - Recipe

struct RecipeView: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    let recipeID: String
    @State private var recipe: Recipe?
    @State private var cooking = false
    @State private var step = 0
    @State private var settingOven = false
    @State private var planning = false
    @State private var editing = false
    @State private var planDate = Date()

    private var canEdit: Bool { store.me?.capabilities.contains("family.edit") ?? false }
    private var canOven: Bool { store.me?.capabilities.contains("appliances") ?? false }

    var body: some View {
        ScrollView {
            if let r = recipe {
                VStack(alignment: .leading, spacing: 14) {
                    HubImage(path: r.imageUrl) { Theme.card2 }.frame(height: 200).clipShape(RoundedRectangle(cornerRadius: 20))
                    HStack {
                        if let t = r.totalTime { Pill(text: t) }
                        if let s = r.servings { Pill(text: "Serves \(s)") }
                        if r.favorite == true { Pill(text: "♥ Family favorite", color: Theme.star) }
                    }
                    if let p = r.oven {
                        VStack(alignment: .leading, spacing: 8) {
                            Text(p.saved == true ? "OVEN SETTING" : "OVEN FOUND IN THIS RECIPE").font(.caption.weight(.bold)).foregroundStyle(Theme.accent)
                            Text(p.summary).font(.title2.weight(.semibold))
                            if let q = p.quote { Text("“\(q)”").font(.footnote).italic().foregroundStyle(Theme.dim) }
                            if canOven { PrimaryButton(title: "Set oven", symbol: "oven.fill") { settingOven = true } }
                        }
                        .card()
                    }
                    if canEdit {
                        HStack(spacing: 10) {
                            Button { planning = true } label: { Label("Plan a night", systemImage: "calendar.badge.plus").frame(maxWidth: .infinity) }
                            Button { shop(r) } label: { Label("Add to Shopping", systemImage: "cart.badge.plus").frame(maxWidth: .infinity) }
                        }
                        .buttonStyle(.bordered)
                    }
                    PrimaryButton(title: "Start cooking", symbol: "flame.fill", color: Theme.warn) { step = 0; cooking = true }
                    if let d = r.description, !d.isEmpty { Text(d).foregroundStyle(Theme.dim) }
                    SectionHeader("Ingredients")
                    VStack(alignment: .leading, spacing: 8) { ForEach(r.ingredients, id: \.self) { Text("• \($0)") } }.card()
                    SectionHeader("Steps")
                    VStack(alignment: .leading, spacing: 12) {
                        ForEach(Array(r.steps.enumerated()), id: \.offset) { i, s in
                            HStack(alignment: .top) { Text("\(i + 1)").font(.headline).foregroundStyle(Theme.accent).frame(width: 24); Text(s) }
                        }
                    }
                    .card()
                    if let u = r.sourceUrl, let url = URL(string: u) { Link("Original recipe", destination: url).font(.footnote) }
                }
                .screen()
            } else { ProgressView().padding(40) }
        }
        .background(Backdrop())
        .navigationTitle(recipe?.title ?? "Recipe")
        .toolbar {
            if let r = recipe {
                ToolbarItemGroup(placement: .topBarTrailing) {
                    // Your own star follows you; the heart is the family's shared pick (a Mealie tag).
                    StarButton(kind: .recipe, id: r.slug ?? recipeID, name: r.title,
                               label: FavoriteRef(id: r.slug ?? recipeID, title: r.title, image: r.imageUrl))
                    if canEdit {
                        Button { favorite(r) } label: { Image(systemName: r.favorite == true ? "heart.fill" : "heart") }
                            .accessibilityLabel(r.favorite == true ? "Remove family favorite" : "Mark as a family favorite")
                        Button("Edit") { editing = true }
                    }
                }
            }
        }
        .task { await load() }
        .sheet(isPresented: $settingOven) { if let r = recipe, let p = r.oven { OvenFromRecipeSheet(plan: p, recipeTitle: r.title) } }
        .sheet(isPresented: $planning) {
            NavigationStack {
                DatePicker("Night", selection: $planDate, in: Calendar.current.startOfDay(for: Date())..., displayedComponents: .date)
                    .datePickerStyle(.graphical).padding()
                    .navigationTitle("Plan for…").navigationBarTitleDisplayMode(.inline)
                    .toolbar {
                        ToolbarItem(placement: .cancellationAction) { Button("Cancel") { planning = false } }
                        ToolbarItem(placement: .confirmationAction) { Button("Plan") { plan() } }
                    }
            }
            .presentationDetents([.medium, .large])
        }
        .sheet(isPresented: $editing) {
            if let r = recipe { RecipeEditor(slug: r.slug ?? recipeID, draft: RecipeDraft(from: r)) { _ in Task { await load() } } }
        }
        .fullScreenCover(isPresented: $cooking) {
            if let r = recipe {
                VStack(spacing: 24) {
                    HStack { Text("Step \(step + 1) of \(r.steps.count)").foregroundStyle(Theme.dim); Spacer(); Button("Done") { cooking = false } }
                    Spacer()
                    Text(r.steps.indices.contains(step) ? r.steps[step] : "").font(.system(size: 30, weight: .semibold, design: .rounded))
                        .multilineTextAlignment(.center)
                    Spacer()
                    HStack(spacing: 16) {
                        Button { step = max(0, step - 1) } label: { Image(systemName: "chevron.left").font(.title).frame(maxWidth: .infinity, minHeight: 64) }
                            .buttonStyle(.bordered).disabled(step == 0)
                        Button {
                            if step < r.steps.count - 1 { step += 1 } else {
                                cooking = false
                                Task { try? await HubClient.shared.send("/v1/meals/recipes/\(r.slug ?? recipeID)/made") }
                            }
                        } label: {
                            Image(systemName: step < r.steps.count - 1 ? "chevron.right" : "checkmark").font(.title).frame(maxWidth: .infinity, minHeight: 64)
                        }
                        .buttonStyle(.borderedProminent)
                    }
                }
                .padding(24)
                .onAppear { UIApplication.shared.isIdleTimerDisabled = true }
                .onDisappear { UIApplication.shared.isIdleTimerDisabled = false }
            }
        }
    }

    private func load() async { recipe = try? await HubClient.shared.get("/v1/meals/recipes/\(recipeID)") }

    private func favorite(_ r: Recipe) {
        let on = !(r.favorite ?? false)
        recipe?.favorite = on
        Task {
            do { try await HubClient.shared.send("/v1/meals/recipes/\(r.slug ?? recipeID)/favorite", ["on": on]) }
            catch { recipe?.favorite = !on; store.show(error.localizedDescription, isError: true) }
        }
    }

    private func shop(_ r: Recipe) {
        Task {
            do {
                let a: ActionReply = try await HubClient.shared.post("/v1/meals/recipes/\(r.slug ?? recipeID)/shopping")
                store.show(a.dryRun == true ? "Practice mode" : (a.message ?? "Added to Shopping"))
            } catch { store.show(error.localizedDescription, isError: true) }
        }
    }

    private func plan() {
        planning = false
        let day = ISO.day(planDate)
        Task {
            do {
                let _: Empty = try await HubClient.shared.call("PUT", "/v1/meals/plan/\(day)", body: ["slug": recipe?.slug ?? recipeID])
                store.show("Planned for \(planDate.formatted(.dateTime.weekday(.wide)))")
                await store.loadHome()
            } catch { store.show(error.localizedDescription, isError: true) }
        }
    }
}

// MARK: - Adding and editing

struct AddRecipeSheet: View {
    @Environment(HomeStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let onAdded: (String) -> Void
    @State private var link = ""
    @State private var clipboardLink: String?
    @State private var busy = false
    @State private var draft: RecipeDraft?
    @State private var photo: PhotosPickerItem?
    @State private var scanImage: UIImage?
    @State private var scanning = false

    var body: some View {
        NavigationStack {
            List {
                if let c = clipboardLink {
                    Section {
                        Button { add(c) } label: {
                            VStack(alignment: .leading, spacing: 2) {
                                Label("Add the link you copied", systemImage: "doc.on.clipboard").font(.headline)
                                Text(c).font(.caption).foregroundStyle(Theme.dim).lineLimit(1)
                            }
                        }
                    }
                }
                Section {
                    TextField("Paste a recipe link", text: $link).keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                        .onSubmit { add(link) }
                    Button { add(link) } label: { Label("Add from link", systemImage: "link") }.disabled(link.isEmpty || busy)
                } header: { Text("From a website") } footer: { Text("Works with most recipe sites. In Safari you can also Share → Copy, then come here.") }
                Section {
                    if UIImagePickerController.isSourceTypeAvailable(.camera) {
                        Button { scanning = true } label: { Label("Take a photo of the page", systemImage: "camera") }
                    }
                    PhotosPicker(selection: $photo, matching: .images) { Label("Choose a photo", systemImage: "photo") }
                } header: { Text("Scan a cookbook page") } footer: { Text("The phone reads the text; you check it before saving.") }
                Section {
                    Button { draft = RecipeDraft() } label: { Label("Type it in", systemImage: "square.and.pencil") }
                }
                if busy { Section { HStack { ProgressView(); Text("Reading the recipe…").foregroundStyle(Theme.dim) } } }
            }
            .navigationTitle("Add a recipe")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } } }
            .task {
                if UIPasteboard.general.hasURLs, let u = UIPasteboard.general.url, u.scheme?.hasPrefix("http") == true {
                    clipboardLink = u.absoluteString
                }
            }
            .onChange(of: photo) { _, item in
                guard let item else { return }
                Task {
                    if let data = try? await item.loadTransferable(type: Data.self), let img = UIImage(data: data) { await read(img) }
                }
            }
            .fullScreenCover(isPresented: $scanning) {
                CameraPicker { img in scanning = false; if let img { Task { await read(img) } } }.ignoresSafeArea()
            }
            .sheet(item: Binding(get: { draft.map { DraftBox(draft: $0) } }, set: { draft = $0?.draft })) { box in
                RecipeEditor(slug: nil, draft: box.draft, photo: scanImage) { slug in
                    dismiss()
                    if let slug { onAdded(slug) }
                }
            }
        }
    }

    struct DraftBox: Identifiable { var draft: RecipeDraft; var id: String { draft.title + "\(draft.steps.count)" } }

    private func add(_ s: String) {
        let url = s.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !url.isEmpty else { return }
        busy = true
        Task {
            defer { busy = false }
            do {
                let r: SavedRecipe = try await HubClient.shared.post("/v1/meals/recipes", ["url": url])
                if r.dryRun == true { store.show("Practice mode"); return }
                store.show("Added \(r.recipe?.title ?? "recipe")")
                dismiss()
                if let slug = r.slug { onAdded(slug) }
            } catch { store.show(error.localizedDescription, isError: true) }
        }
    }

    /// Read the text on the phone (Vision), then let the hub split it into title, ingredients and steps.
    private func read(_ img: UIImage) async {
        busy = true
        defer { busy = false }
        scanImage = img
        let text = await TextReader.read(img)
        guard !text.isEmpty else { store.show("Couldn't read any text in that photo", isError: true); return }
        do { draft = try await HubClient.shared.post("/v1/meals/recipes/parse", ["text": text]) }
        catch { store.show(error.localizedDescription, isError: true) }
    }
}

enum TextReader {
    static func read(_ image: UIImage) async -> String {
        guard let cg = image.cgImage else { return "" }
        return await withCheckedContinuation { cont in
            let req = VNRecognizeTextRequest { req, _ in
                let lines = (req.results as? [VNRecognizedTextObservation])?.compactMap { $0.topCandidates(1).first?.string } ?? []
                cont.resume(returning: lines.joined(separator: "\n"))
            }
            req.recognitionLevel = .accurate
            req.usesLanguageCorrection = true
            DispatchQueue.global(qos: .userInitiated).async {
                do { try VNImageRequestHandler(cgImage: cg, orientation: image.cgOrientation).perform([req]) }
                catch { cont.resume(returning: "") }
            }
        }
    }
}

extension UIImage {
    var cgOrientation: CGImagePropertyOrientation {
        switch imageOrientation {
        case .up: .up; case .down: .down; case .left: .left; case .right: .right
        case .upMirrored: .upMirrored; case .downMirrored: .downMirrored; case .leftMirrored: .leftMirrored; case .rightMirrored: .rightMirrored
        @unknown default: .up
        }
    }
}

struct CameraPicker: UIViewControllerRepresentable {
    let done: (UIImage?) -> Void
    func makeUIViewController(context: Context) -> UIImagePickerController {
        let p = UIImagePickerController(); p.sourceType = .camera; p.delegate = context.coordinator; return p
    }
    func updateUIViewController(_ vc: UIImagePickerController, context: Context) {}
    func makeCoordinator() -> Coordinator { Coordinator(done: done) }
    final class Coordinator: NSObject, UIImagePickerControllerDelegate, UINavigationControllerDelegate {
        let done: (UIImage?) -> Void
        init(done: @escaping (UIImage?) -> Void) { self.done = done }
        func imagePickerController(_ picker: UIImagePickerController, didFinishPickingMediaWithInfo info: [UIImagePickerController.InfoKey: Any]) {
            done(info[.originalImage] as? UIImage)
        }
        func imagePickerControllerDidCancel(_ picker: UIImagePickerController) { done(nil) }
    }
}

extension RecipeDraft {
    init(from r: Recipe) {
        self.init(title: r.title, ingredients: r.ingredients, steps: r.steps, oven: r.oven, servings: r.servings,
                  prepTime: r.prepTime, cookTime: r.cookTime, tags: r.tags)
    }
}

struct RecipeEditor: View {
    @Environment(HomeStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let slug: String?
    @State var draft: RecipeDraft
    var photo: UIImage? = nil
    let onSaved: (String?) -> Void
    @State private var hasOven = true
    @State private var ovenMode = "Bake"
    @State private var ovenTemp = 350
    @State private var ovenMinutes = 0
    @State private var pickedPhoto: PhotosPickerItem?
    @State private var newPhoto: Data?
    @State private var usePhoto = false
    @State private var saving = false
    @State private var confirmDelete = false
    @State private var tagText = ""

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Title", text: $draft.title).font(.headline)
                    TextField("Serves", text: Binding(get: { draft.servings ?? "" }, set: { draft.servings = $0.isEmpty ? nil : $0 }))
                    TextField("Prep time (e.g. 10 minutes)", text: Binding(get: { draft.prepTime ?? "" }, set: { draft.prepTime = $0.isEmpty ? nil : $0 }))
                    TextField("Cook time (e.g. 25 minutes)", text: Binding(get: { draft.cookTime ?? "" }, set: { draft.cookTime = $0.isEmpty ? nil : $0 }))
                }
                Section {
                    PhotosPicker(selection: $pickedPhoto, matching: .images) {
                        Label(newPhoto == nil ? "Choose a photo" : "Photo chosen", systemImage: newPhoto == nil ? "photo" : "checkmark.circle.fill")
                    }
                    if photo != nil, newPhoto == nil { Toggle("Use the scanned page as the photo", isOn: $usePhoto) }
                } header: { Text("Photo") }
                Section {
                    Toggle("Uses the oven", isOn: $hasOven)
                    if hasOven {
                        Picker("Mode", selection: $ovenMode) { ForEach(OvenModes.common, id: \.self) { Text($0) } }
                        if OvenModes.usesTemp(ovenMode) {
                            Stepper("Temperature \(ovenTemp)°", value: $ovenTemp, in: 140...550, step: 5)
                        }
                        Stepper(ovenMinutes == 0 ? "Timer: none" : "Timer \(ovenMinutes) min", value: $ovenMinutes, in: 0...360, step: 5)
                    }
                } header: { Text("Oven setting") } footer: {
                    if let q = draft.oven?.quote { Text("Found: “\(q)”") } else { Text("Set oven uses this.") }
                }
                Section("Ingredients") {
                    ForEach(draft.ingredients.indices, id: \.self) { i in
                        TextField("Ingredient", text: Binding(get: { draft.ingredients.indices.contains(i) ? draft.ingredients[i] : "" },
                                                            set: { if draft.ingredients.indices.contains(i) { draft.ingredients[i] = $0 } }))
                    }
                    .onDelete { draft.ingredients.remove(atOffsets: $0) }
                    .onMove { draft.ingredients.move(fromOffsets: $0, toOffset: $1) }
                    Button { draft.ingredients.append("") } label: { Label("Add ingredient", systemImage: "plus") }
                }
                Section("Steps") {
                    ForEach(draft.steps.indices, id: \.self) { i in
                        TextField("Step \(i + 1)", text: Binding(get: { draft.steps.indices.contains(i) ? draft.steps[i] : "" },
                                                                 set: { if draft.steps.indices.contains(i) { draft.steps[i] = $0 } }), axis: .vertical)
                    }
                    .onDelete { draft.steps.remove(atOffsets: $0) }
                    .onMove { draft.steps.move(fromOffsets: $0, toOffset: $1) }
                    Button { draft.steps.append("") } label: { Label("Add step", systemImage: "plus") }
                }
                Section("Tags") {
                    TextField("Dinner, Kids like it, Quick", text: $tagText)
                }
                if slug != nil {
                    Section { Button("Delete recipe", role: .destructive) { confirmDelete = true } }
                }
            }
            .navigationTitle(slug == nil ? "New recipe" : "Edit recipe")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") { save() }.disabled(saving || draft.title.trimmingCharacters(in: .whitespaces).isEmpty)
                }
                ToolbarItem(placement: .topBarLeading) { EditButton() }
            }
            .onAppear {
                if let o = draft.oven { ovenMode = o.mode; ovenTemp = o.temp ?? 350; ovenMinutes = o.minutes ?? 0; hasOven = true }
                else { hasOven = slug == nil ? false : false }
                tagText = (draft.tags ?? []).filter { !["Oven", "Favorite"].contains($0) }.joined(separator: ", ")
                usePhoto = photo != nil
            }
            .onChange(of: pickedPhoto) { _, item in
                Task { newPhoto = try? await item?.loadTransferable(type: Data.self) }
            }
            .confirmationDialog("Delete \(draft.title)?", isPresented: $confirmDelete, titleVisibility: .visible) {
                Button("Delete", role: .destructive) { delete() }
            }
        }
    }

    private func requestBody() -> [String: Any] {
        var b: [String: Any] = [
            "title": draft.title.trimmingCharacters(in: .whitespaces),
            "ingredients": draft.ingredients.map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty },
            "steps": draft.steps.map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty },
        ]
        if let s = draft.servings { b["servings"] = s }
        if let s = draft.prepTime { b["prep_time"] = s }
        if let s = draft.cookTime { b["cook_time"] = s }
        let keep = (draft.tags ?? []).filter { $0 == "Favorite" }
        b["tags"] = keep + tagText.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        if hasOven {
            var o: [String: Any] = ["mode": ovenMode]
            if OvenModes.usesTemp(ovenMode) { o["temp"] = ovenTemp }
            if ovenMinutes > 0 { o["minutes"] = ovenMinutes }
            b["oven"] = o
        } else { b["no_oven"] = true }
        return b
    }

    private func save() {
        saving = true
        Task {
            defer { saving = false }
            do {
                let r: SavedRecipe = slug == nil
                    ? try await HubClient.shared.post("/v1/meals/recipes", requestBody())
                    : try await HubClient.shared.call("PUT", "/v1/meals/recipes/\(slug!)", body: requestBody())
                if r.dryRun == true { store.show("Practice mode"); dismiss(); return }
                let saved = r.slug ?? slug
                if let saved, let img = newPhoto ?? (usePhoto ? photo?.jpegData(compressionQuality: 0.8) : nil) {
                    _ = try? await HubClient.shared.raw("PUT", "/v1/meals/recipes/\(saved)/image", body: img)
                }
                store.show(slug == nil ? "Recipe added" : "Saved")
                dismiss()
                onSaved(saved)
            } catch { store.show(error.localizedDescription, isError: true) }
        }
    }

    private func delete() {
        guard let slug else { return }
        Task {
            do {
                let _: Empty = try await HubClient.shared.call("DELETE", "/v1/meals/recipes/\(slug)")
                store.show("Deleted"); dismiss(); onSaved(nil)
            } catch { store.show(error.localizedDescription, isError: true) }
        }
    }
}
