import SwiftUI

// Recipe sections (Mealie categories), holiday and feast plans (kept on the hub), and one
// shopping list built from a feast, some weeks of dinners, or both.

// MARK: - Models

struct RecipeSections: Codable {
    var sections: [Group]
    var unsorted: [RecipeSummary]
    var names: [String]
    var total: Int?
    struct Group: Codable, Hashable, Identifiable {
        var name: String
        var symbol: String
        var count: Int
        var items: [RecipeSummary]
        var id: String { name }
    }
}

struct FeastItem: Codable, Hashable, Identifiable {
    var id: String
    var slug: String?
    var recipeId: String?
    var title: String
    var imageUrl: String?
    var servings: Int?
    var baseServings: Double?
    var when: String?
    var day: String?
    var note: String?
}

struct FeastCourse: Codable, Hashable, Identifiable {
    var key: String
    var title: String
    var items: [FeastItem]
    var id: String { key }
}

struct FeastPlan: Codable, Hashable, Identifiable {
    var id: String
    var name: String
    var kind: String
    var startDate: String
    var endDate: String
    var guests: Int
    var notes: String?
    var courses: [FeastCourse]
    var recipeCount: Int?
    var timeline: [Step]?

    struct Step: Codable, Hashable, Identifiable {
        var day: String
        var when: String
        var label: String
        var items: [Entry]
        var id: String { day + when }
        struct Entry: Codable, Hashable { var title: String?; var slug: String?; var course: String? }
    }

    var symbol: String { Feast.symbol(kind) }
    var multiDay: Bool { endDate != startDate }
    var dateLine: String {
        guard let s = ISO.parseDay(startDate) else { return startDate }
        let first = s.formatted(.dateTime.weekday(.abbreviated).month(.abbreviated).day())
        guard multiDay, let e = ISO.parseDay(endDate) else { return first }
        return "\(first) – \(e.formatted(.dateTime.month(.abbreviated).day()))"
    }
}

struct FeastTemplate: Codable, Hashable, Identifiable {
    var id: String
    var title: String
    var symbol: String
    var guests: Int
    var date: String
    var courses: [String]
}

struct KeyTitle: Codable, Hashable, Identifiable { var key: String; var title: String; var id: String { key } }

struct FeastsReply: Codable {
    var plans: [FeastPlan]
    var templates: [FeastTemplate]
    var courses: [KeyTitle]
    var when: [KeyTitle]
}

struct FeastReply: Codable { var plan: FeastPlan?; var dryRun: Bool? }

struct ShopBuild: Codable {
    var items: [Item]
    var aisles: [Aisle]
    var count: Int
    var staplesSkipped: Int?
    var sources: [Source]
    var label: String
    var lists: [ListRef]

    struct Item: Codable, Hashable, Identifiable {
        var key: String
        var name: String
        var text: String
        var aisle: String
        var recipes: [String]
        var staple: Bool?
        var id: String { key }
    }
    struct Aisle: Codable, Hashable, Identifiable { var name: String; var items: [Item]; var id: String { name } }
    struct Source: Codable, Hashable {
        var title: String
        var factor: Double?
        var servings: Double?
        var baseServings: Double?
        var why: String?
        var scaled: Bool?
        var missing: Bool?
    }
    struct ListRef: Codable, Hashable, Identifiable { var id: String; var name: String? }
}

enum Feast {
    static let sections = ["Appetizers", "Mains", "Sides", "Salads", "Soups", "Breads", "Desserts", "Drinks",
                           "Breakfast", "Snacks", "Sauces"]
    static let whenOrder: [(String, String)] = [("days_before", "A few days before"), ("day_before", "The day before"),
                                                ("morning", "Morning of"), ("day_of", "Day of"),
                                                ("last_minute", "Right before serving")]
    static func whenLabel(_ k: String?) -> String { whenOrder.first { $0.0 == k }?.1 ?? "Day of" }
    static func symbol(_ kind: String) -> String {
        switch kind {
        case "thanksgiving": "leaf.fill"
        case "christmas": "gift.fill"
        case "easter": "sun.max.fill"
        default: "fork.knife"
        }
    }
    static func sectionSymbol(_ name: String) -> String {
        switch name {
        case "Appetizers": "takeoutbag.and.cup.and.straw"
        case "Mains": "fork.knife"
        case "Sides": "carrot"
        case "Salads": "leaf"
        case "Soups": "mug"
        case "Breads": "birthday.cake"
        case "Desserts": "birthday.cake.fill"
        case "Drinks": "wineglass"
        case "Breakfast": "sunrise"
        case "Snacks": "popcorn"
        case "Sauces": "drop"
        default: "square.grid.2x2"
        }
    }
    /// A Codable value as the plain JSON the hub client sends (snake_case keys).
    static func json<T: Encodable>(_ v: T) -> Any {
        let e = JSONEncoder()
        e.keyEncodingStrategy = .convertToSnakeCase
        guard let d = try? e.encode(v), let o = try? JSONSerialization.jsonObject(with: d) else { return [String: Any]() }
        return o
    }
}

// MARK: - Recipe sections

struct RecipeSectionsView: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    @State private var data: RecipeSections?
    @State private var error: String?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 6) {
                if let data {
                    let groups = data.sections.filter { $0.count > 0 }
                    if groups.isEmpty && data.unsorted.isEmpty {
                        EmptyCard(symbol: "square.grid.2x2", title: "No recipes yet", detail: "Add a recipe, then file it under Mains, Sides, Desserts…")
                    }
                    ForEach(groups) { g in
                        header(g.name, symbol: g.symbol, count: g.count)
                        shelf(g.items)
                    }
                    if !data.unsorted.isEmpty {
                        header("No section yet", symbol: "tray", count: data.unsorted.count)
                        Text("Open a recipe and tap Sections to file it.").font(.caption).foregroundStyle(Theme.dim)
                        shelf(data.unsorted)
                    }
                } else if let error {
                    EmptyCard(symbol: "fork.knife", title: "Meals aren't answering", detail: error)
                } else { LoadingCard() }
            }
            .screen()
        }
        .background(Backdrop())
        .navigationTitle("Sections")
        .task { await load() }
        .refreshable { await load() }
    }

    private func header(_ title: String, symbol: String, count: Int) -> some View {
        HStack(spacing: 8) {
            Image(systemName: symbol).foregroundStyle(Theme.accent)
            Text(title).font(.title3.weight(.semibold)).foregroundStyle(Theme.fg)
            Text("\(count)").font(.caption.weight(.semibold)).foregroundStyle(Theme.dim)
            Spacer()
        }
        .padding(.top, 14)
    }

    private func shelf(_ items: [RecipeSummary]) -> some View {
        ScrollView(.horizontal, showsIndicators: false) {
            LazyHStack(spacing: 10) {
                ForEach(items, id: \.slug) { r in
                    Button { router.go(.recipe(r.slug)) } label: {
                        VStack(alignment: .leading, spacing: 0) {
                            HubImage(path: r.imageUrl) { Theme.card2.overlay(Image(systemName: "fork.knife").foregroundStyle(Theme.dim)) }
                                .frame(width: 150, height: 96).clipped()
                            Text(r.title ?? r.slug).font(.subheadline.weight(.semibold)).lineLimit(2).foregroundStyle(Theme.fg)
                                .frame(width: 130, alignment: .leading).padding(10)
                        }
                        .background(Theme.card, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                        .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    private func load() async {
        do { data = try await HubClient.shared.get("/v1/meals/sections"); error = nil }
        catch { self.error = error.localizedDescription }
    }
}

/// File a recipe under sections (Mealie categories).
struct SectionsSheet: View {
    @Environment(\.dismiss) private var dismiss
    @Environment(HomeStore.self) private var store
    let slug: String
    var onSaved: ([String]) -> Void
    @State private var picked: [String]
    @State private var names: [String]
    @State private var custom = ""
    @State private var saving = false

    init(slug: String, current: [String], onSaved: @escaping ([String]) -> Void) {
        self.slug = slug
        self.onSaved = onSaved
        _picked = State(initialValue: current)
        _names = State(initialValue: Feast.sections + current.filter { !Feast.sections.contains($0) })
    }

    var body: some View {
        NavigationStack {
            List {
                Section {
                    ForEach(names, id: \.self) { n in
                        Button {
                            if let i = picked.firstIndex(of: n) { picked.remove(at: i) } else { picked.append(n) }
                        } label: {
                            HStack {
                                Image(systemName: Feast.sectionSymbol(n)).foregroundStyle(Theme.accent).frame(width: 26)
                                Text(n).foregroundStyle(Theme.fg)
                                Spacer()
                                if picked.contains(n) { Image(systemName: "checkmark").foregroundStyle(Theme.accent) }
                            }
                        }
                    }
                } footer: { Text("Sections are Mealie categories, so they show there too.") }
                Section("New section") {
                    HStack {
                        TextField("e.g. Holiday", text: $custom).onSubmit(addCustom)
                        Button("Add", action: addCustom).disabled(custom.trimmingCharacters(in: .whitespaces).isEmpty)
                    }
                }
            }
            .navigationTitle("Sections").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) { Button("Save") { save() }.disabled(saving) }
            }
            .task {
                if let d: RecipeSections = await hubQuiet("meals/sections", { try await HubClient.shared.get("/v1/meals/sections") }) {
                    for n in d.names where !names.contains(n) { names.append(n) }
                }
            }
        }
    }

    private func addCustom() {
        let n = custom.trimmingCharacters(in: .whitespaces)
        guard !n.isEmpty else { return }
        if !names.contains(n) { names.append(n) }
        if !picked.contains(n) { picked.append(n) }
        custom = ""
    }

    private func save() {
        saving = true
        let ordered = names.filter { picked.contains($0) }
        Task {
            defer { saving = false }
            do {
                let _: Empty = try await HubClient.shared.call("PUT", "/v1/meals/recipes/\(slug)/sections", body: ["sections": ordered])
                onSaved(ordered)
                dismiss()
            } catch { store.show(error.localizedDescription, isError: true) }
        }
    }
}

/// From a recipe: drop it into a course of a holiday or feast plan.
struct AddToFeastSheet: View {
    @Environment(\.dismiss) private var dismiss
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    let slug: String
    let sections: [String]
    @State private var plans: [FeastPlan]?

    var body: some View {
        NavigationStack {
            List {
                if let plans {
                    if plans.isEmpty {
                        EmptyCard(symbol: "leaf.fill", title: "No holiday plans yet", detail: "Start one in Family › Holidays.")
                            .listRowBackground(Color.clear)
                        Button { dismiss(); router.go(.feasts) } label: { Label("Open Holidays", systemImage: "leaf.fill") }
                    }
                    ForEach(plans) { p in
                        Section {
                            ForEach(p.courses) { c in
                                Button { add(p, c) } label: {
                                    HStack {
                                        Text(c.title).foregroundStyle(Theme.fg)
                                        if sections.contains(c.title) { Pill(text: "Fits") }
                                        Spacer()
                                        Text("\(c.items.count)").foregroundStyle(Theme.dim)
                                    }
                                }
                            }
                        } header: { Label("\(p.name) · \(p.dateLine)", systemImage: p.symbol) }
                    }
                } else { ProgressView() }
            }
            .navigationTitle("Add to a plan").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } } }
            .task {
                let today = ISO.day(Date())
                plans = ((await hubQuiet("meals/feasts", { try await HubClient.shared.get("/v1/meals/feasts") })) as FeastsReply?)?.plans.filter { $0.endDate >= today } ?? []
            }
        }
    }

    private func add(_ p: FeastPlan, _ c: FeastCourse) {
        Task {
            do {
                let _: FeastReply = try await HubClient.shared.post("/v1/meals/feasts/\(p.id)/items", ["course": c.key, "slug": slug])
                store.show("Added to \(p.name)", c.title)
                dismiss()
            } catch { store.show(error.localizedDescription, isError: true) }
        }
    }
}

// MARK: - Holidays and feasts

struct FeastsView: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    @State private var data: FeastsReply?
    @State private var error: String?
    @State private var busy = false

    private var canEdit: Bool { store.me?.capabilities.contains("family.edit") ?? false }

    var body: some View {
        List {
            if canEdit, let t = data?.templates, !t.isEmpty {
                Section {
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 10) {
                            ForEach(t) { tpl in
                                Button { create(tpl) } label: {
                                    VStack(alignment: .leading, spacing: 6) {
                                        Image(systemName: tpl.symbol).font(.title2).foregroundStyle(Theme.accent)
                                        Text(tpl.title).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.fg).lineLimit(1)
                                        Text(day(tpl.date)).font(.caption).foregroundStyle(Theme.dim)
                                    }
                                    .frame(width: 128, height: 96, alignment: .leading).padding(.horizontal, 12)
                                    .background(Theme.card, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                                }
                                .buttonStyle(.plain).disabled(busy)
                            }
                        }
                        .padding(.horizontal, 16)
                    }
                    .listRowBackground(Color.clear).listRowInsets(EdgeInsets())
                } header: { Text("Start a plan") }
            }
            if let data {
                let today = ISO.day(Date())
                let upcoming = data.plans.filter { $0.endDate >= today }.sorted { $0.startDate < $1.startDate }
                let past = data.plans.filter { $0.endDate < today }
                if data.plans.isEmpty {
                    EmptyCard(symbol: "leaf.fill", title: "No holiday plans yet",
                              detail: canEdit ? "Pick Thanksgiving, Christmas, Easter or a feast above. Each comes with empty courses to fill." : nil)
                        .listRowBackground(Color.clear)
                }
                if !upcoming.isEmpty { Section("Coming up") { ForEach(upcoming) { row($0) } } }
                if !past.isEmpty { Section("Past") { ForEach(past) { row($0) } } }
                Section {
                    Button { router.go(.shopBuilder([])) } label: { Label("Build a shopping list", systemImage: "cart.badge.plus") }
                }
            } else if let error {
                EmptyCard(symbol: "fork.knife", title: "Meals aren't answering", detail: error).listRowBackground(Color.clear)
            } else { ProgressView() }
        }
        .scrollContentBackground(.hidden).background(Backdrop())
        .navigationTitle("Holidays")
        .task { await load() }
        .refreshable { await load() }
    }

    private func row(_ p: FeastPlan) -> some View {
        Button { router.go(.feast(p.id)) } label: {
            HStack(spacing: 12) {
                Image(systemName: p.symbol).font(.title3).foregroundStyle(Theme.accent).frame(width: 36)
                VStack(alignment: .leading, spacing: 2) {
                    Text(p.name).font(.body.weight(.medium)).foregroundStyle(Theme.fg)
                    Text("\(p.dateLine) · \(p.guests) guests · \(p.recipeCount ?? 0) dishes").font(.caption).foregroundStyle(Theme.dim)
                }
                Spacer()
            }
        }
        .swipeActions(edge: .trailing) {
            if canEdit { Button(role: .destructive) { delete(p) } label: { Label("Delete", systemImage: "trash") } }
        }
    }

    private func day(_ s: String) -> String { ISO.parseDay(s)?.formatted(.dateTime.month(.abbreviated).day()) ?? s }

    private func load() async {
        do { data = try await HubClient.shared.get("/v1/meals/feasts"); error = nil }
        catch { self.error = error.localizedDescription }
    }

    private func create(_ t: FeastTemplate) {
        busy = true
        Task {
            defer { busy = false }
            do {
                let r: FeastReply = try await HubClient.shared.post("/v1/meals/feasts", ["template": t.id])
                if let p = r.plan { await load(); router.go(.feast(p.id)) } else { store.show("Practice mode") }
            } catch { store.show(error.localizedDescription, isError: true) }
        }
    }

    private func delete(_ p: FeastPlan) {
        Task {
            do { let _: Empty = try await HubClient.shared.call("DELETE", "/v1/meals/feasts/\(p.id)"); await load() }
            catch { store.show(error.localizedDescription, isError: true) }
        }
    }
}

struct FeastView: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    let planID: String
    @State private var plan: FeastPlan?
    @State private var courseTitles: [KeyTitle] = []
    @State private var error: String?
    @State private var adding: FeastCourse?
    @State private var editingItem: ItemRef?
    @State private var editingInfo = false

    struct ItemRef: Identifiable { var course: String; var item: FeastItem; var id: String { item.id } }

    private var canEdit: Bool { store.me?.capabilities.contains("family.edit") ?? false }

    var body: some View {
        List {
            if let p = plan {
                Section {
                    HStack(spacing: 12) {
                        Image(systemName: p.symbol).font(.largeTitle).foregroundStyle(Theme.accent)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(p.dateLine).font(.headline)
                            Text("\(p.guests) guests · \(p.recipeCount ?? 0) dishes").font(.subheadline).foregroundStyle(Theme.dim)
                        }
                    }
                    if canEdit {
                        Stepper("Guests: \(p.guests)", value: Binding(get: { p.guests }, set: { g in
                            plan?.guests = g
                            save(["guests": g])
                        }), in: 1...200)
                    }
                    HStack(spacing: 10) {
                        Button { router.go(.shopBuilder([p.id])) } label: { Label("Shopping list", systemImage: "cart.badge.plus").frame(maxWidth: .infinity) }
                            .buttonStyle(.borderedProminent)
                        if canEdit {
                            Button { toMealPlan() } label: { Label("Meal plan", systemImage: "calendar.badge.plus").frame(maxWidth: .infinity) }
                                .buttonStyle(.bordered)
                        }
                    }
                    .listRowBackground(Color.clear).listRowInsets(EdgeInsets())
                }
                ForEach(p.courses) { c in
                    Section {
                        ForEach(c.items) { it in itemRow(c, it, guests: p.guests) }
                            .onDelete { idx in if canEdit { remove(c, idx) } }
                        if canEdit {
                            Button { adding = c } label: { Label("Add to \(c.title.lowercased())", systemImage: "plus.circle") }
                        }
                    } header: {
                        Label(c.title, systemImage: Feast.sectionSymbol(c.title))
                    }
                }
                if canEdit {
                    let missing = courseTitles.filter { k in !p.courses.contains { $0.key == k.key } }
                    if !missing.isEmpty {
                        Section {
                            Menu {
                                ForEach(missing) { k in Button(k.title) { addCourse(k) } }
                            } label: { Label("Add a course", systemImage: "plus.rectangle.on.rectangle") }
                        }
                    }
                }
                if let tl = p.timeline, !tl.isEmpty {
                    Section("Prep timeline") {
                        ForEach(tl) { step in
                            VStack(alignment: .leading, spacing: 4) {
                                Text(stepTitle(step, p)).font(.caption.weight(.bold)).foregroundStyle(Theme.accent)
                                ForEach(Array(step.items.enumerated()), id: \.offset) { _, e in
                                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                                        Text("• \(e.title ?? "")").foregroundStyle(Theme.fg)
                                        if let c = e.course { Text(c).font(.caption).foregroundStyle(Theme.dim) }
                                    }
                                }
                            }
                            .padding(.vertical, 2)
                        }
                    }
                }
            } else if let error {
                EmptyCard(symbol: "leaf", title: "Couldn't open this plan", detail: error).listRowBackground(Color.clear)
            } else { ProgressView() }
        }
        .scrollContentBackground(.hidden).background(Backdrop())
        .navigationTitle(plan?.name ?? "Holiday")
        .toolbar { if canEdit, plan != nil { ToolbarItem(placement: .topBarTrailing) { Button("Edit") { editingInfo = true } } } }
        .task { await load() }
        .refreshable { await load() }
        .sheet(item: $adding) { c in
            DishPicker(course: c) { choice in Task { await add(c, choice) } }
        }
        .sheet(item: $editingItem) { ref in
            if let p = plan {
                FeastItemSheet(plan: p, item: ref.item) { updated in replace(ref.course, updated) }
            }
        }
        .sheet(isPresented: $editingInfo) {
            if let p = plan { FeastInfoSheet(plan: p) { body in save(body) } }
        }
    }

    private func itemRow(_ c: FeastCourse, _ it: FeastItem, guests: Int) -> some View {
        Button {
            if canEdit { editingItem = ItemRef(course: c.key, item: it) } else if let s = it.slug { router.go(.recipe(s)) }
        } label: {
            HStack(spacing: 12) {
                HubImage(path: it.imageUrl) { Theme.card2.overlay(Image(systemName: "fork.knife").foregroundStyle(Theme.dim)) }
                    .frame(width: 44, height: 44).clipShape(RoundedRectangle(cornerRadius: 9))
                VStack(alignment: .leading, spacing: 2) {
                    Text(it.title).foregroundStyle(Theme.fg)
                    Text(detail(it, guests: guests)).font(.caption).foregroundStyle(Theme.dim)
                }
                Spacer()
            }
        }
        .contextMenu {
            if let s = it.slug { Button { router.go(.recipe(s)) } label: { Label("Open recipe", systemImage: "book") } }
        }
    }

    private func detail(_ it: FeastItem, guests: Int) -> String {
        let serves = it.servings ?? guests
        var parts = ["Serves \(serves)"]
        if let b = it.baseServings, b > 0, Double(serves) != b {
            let f = Double(serves) / b
            parts.append(f == f.rounded() ? "×\(Int(f))" : String(format: "×%.1f", f))
        }
        if it.slug == nil { parts = ["No recipe"] }
        parts.append(Feast.whenLabel(it.when))
        if let d = it.day, let date = ISO.parseDay(d), plan?.multiDay == true { parts.append(date.formatted(.dateTime.weekday(.abbreviated))) }
        return parts.joined(separator: " · ")
    }

    private func stepTitle(_ s: FeastPlan.Step, _ p: FeastPlan) -> String {
        var t = s.label.uppercased()
        if p.multiDay || s.day != p.startDate, let d = ISO.parseDay(s.day) {
            t = "\(d.formatted(.dateTime.weekday(.abbreviated).month(.abbreviated).day()).uppercased()) · \(t)"
        }
        return t
    }

    private func load() async {
        do {
            let r: FeastReply = try await HubClient.shared.get("/v1/meals/feasts/\(planID)")
            plan = r.plan
            error = nil
        } catch { self.error = error.localizedDescription }
        if courseTitles.isEmpty, let all: FeastsReply = await hubQuiet("meals/feasts", { try await HubClient.shared.get("/v1/meals/feasts") }) { courseTitles = all.courses }
    }

    private func apply(_ r: FeastReply) { if let p = r.plan { plan = p } else if r.dryRun == true { store.show("Practice mode") } }

    private func save(_ body: [String: Any]) {
        Task {
            do { apply(try await HubClient.shared.call("PUT", "/v1/meals/feasts/\(planID)", body: body)) }
            catch { store.show(error.localizedDescription, isError: true); await load() }
        }
    }

    private func saveCourses(_ courses: [FeastCourse]) {
        plan?.courses = courses
        save(["courses": Feast.json(courses)])
    }

    private func add(_ c: FeastCourse, _ choice: DishPicker.Choice) async {
        var body: [String: Any] = ["course": c.key]
        switch choice {
        case .recipe(let slug): body["slug"] = slug
        case .dish(let t): body["title"] = t
        }
        do { apply(try await HubClient.shared.post("/v1/meals/feasts/\(planID)/items", body)) }
        catch { store.show(error.localizedDescription, isError: true) }
    }

    private func remove(_ c: FeastCourse, _ idx: IndexSet) {
        guard var courses = plan?.courses, let ci = courses.firstIndex(where: { $0.key == c.key }) else { return }
        courses[ci].items.remove(atOffsets: idx)
        saveCourses(courses)
    }

    private func replace(_ course: String, _ item: FeastItem) {
        guard var courses = plan?.courses, let ci = courses.firstIndex(where: { $0.key == course }),
              let ii = courses[ci].items.firstIndex(where: { $0.id == item.id }) else { return }
        courses[ci].items[ii] = item
        saveCourses(courses)
    }

    private func addCourse(_ k: KeyTitle) {
        guard var courses = plan?.courses else { return }
        courses.append(FeastCourse(key: k.key, title: k.title, items: []))
        saveCourses(courses)
    }

    private func toMealPlan() {
        Task {
            do {
                let a: ActionReply = try await HubClient.shared.post("/v1/meals/feasts/\(planID)/mealplan")
                store.show(a.dryRun == true ? "Practice mode" : (a.message ?? "On the meal plan"))
                await store.loadHome()
            } catch { store.show(error.localizedDescription, isError: true) }
        }
    }
}

/// Servings, when to make it, and which day (for a plan over several days).
struct FeastItemSheet: View {
    @Environment(\.dismiss) private var dismiss
    @Environment(Router.self) private var router
    let plan: FeastPlan
    @State var item: FeastItem
    var onSave: (FeastItem) -> Void

    init(plan: FeastPlan, item: FeastItem, onSave: @escaping (FeastItem) -> Void) {
        self.plan = plan
        self.onSave = onSave
        _item = State(initialValue: item)
    }

    private var days: [String] {
        guard let s = ISO.parseDay(plan.startDate), let e = ISO.parseDay(plan.endDate) else { return [plan.startDate] }
        var out: [String] = []
        var d = s
        while d <= e && out.count < 64 { out.append(ISO.day(d)); d = Calendar.current.date(byAdding: .day, value: 1, to: d) ?? e.addingTimeInterval(1) }
        return out
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Dish", text: $item.title)
                    if item.slug != nil {
                        Stepper("Serves \(item.servings ?? plan.guests)", value: Binding(get: { item.servings ?? plan.guests },
                                                                                       set: { item.servings = $0 }), in: 1...200)
                        if let b = item.baseServings { Text("The recipe makes \(Int(b)); the shopping list scales to match.").font(.caption).foregroundStyle(Theme.dim) }
                    }
                }
                Section("When to make it") {
                    Picker("When", selection: Binding(get: { item.when ?? "day_of" }, set: { item.when = $0 })) {
                        ForEach(Feast.whenOrder, id: \.0) { Text($0.1).tag($0.0) }
                    }
                    .pickerStyle(.inline).labelsHidden()
                    if plan.multiDay {
                        Picker("Day", selection: Binding(get: { item.day ?? plan.startDate }, set: { item.day = $0 })) {
                            ForEach(days, id: \.self) { d in Text(ISO.parseDay(d)?.formatted(.dateTime.weekday(.wide).month().day()) ?? d).tag(d) }
                        }
                    }
                }
                if let s = item.slug {
                    Section { Button { dismiss(); router.go(.recipe(s)) } label: { Label("Open recipe", systemImage: "book") } }
                }
            }
            .navigationTitle(item.title).navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) { Button("Save") { onSave(item); dismiss() } }
            }
        }
    }
}

/// Name, dates and guests.
struct FeastInfoSheet: View {
    @Environment(\.dismiss) private var dismiss
    let plan: FeastPlan
    var onSave: ([String: Any]) -> Void
    @State private var name: String
    @State private var start: Date
    @State private var end: Date
    @State private var severalDays: Bool
    @State private var guests: Int
    @State private var notes: String

    init(plan: FeastPlan, onSave: @escaping ([String: Any]) -> Void) {
        self.plan = plan
        self.onSave = onSave
        let s = ISO.parseDay(plan.startDate) ?? Date()
        _name = State(initialValue: plan.name)
        _start = State(initialValue: s)
        _end = State(initialValue: ISO.parseDay(plan.endDate) ?? s)
        _severalDays = State(initialValue: plan.multiDay)
        _guests = State(initialValue: plan.guests)
        _notes = State(initialValue: plan.notes ?? "")
    }

    var body: some View {
        NavigationStack {
            Form {
                Section { TextField("Name", text: $name) }
                Section {
                    DatePicker(severalDays ? "From" : "Date", selection: $start, displayedComponents: .date)
                    Toggle("Several days (a holiday week)", isOn: $severalDays)
                    if severalDays { DatePicker("To", selection: $end, in: start..., displayedComponents: .date) }
                }
                Section { Stepper("Guests: \(guests)", value: $guests, in: 1...200) }
                Section("Notes") { TextField("Who's bringing what, seating…", text: $notes, axis: .vertical).lineLimit(3...8) }
            }
            .navigationTitle("Plan").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") {
                        let e = severalDays ? max(end, start) : start
                        onSave(["name": name, "start": ISO.day(start), "end": ISO.day(e), "guests": guests, "notes": notes])
                        dismiss()
                    }
                    .disabled(name.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
        }
    }
}

/// Pick a recipe for a course (its section first), or type a dish with no recipe ("Store-bought rolls").
struct DishPicker: View {
    enum Choice { case recipe(String), dish(String) }
    @Environment(\.dismiss) private var dismiss
    let course: FeastCourse
    let onPick: (Choice) -> Void
    @State private var search = ""
    @State private var data: RecipeSections?
    @State private var typed = ""

    private var all: [RecipeSummary] {
        guard let data else { return [] }
        var seen = Set<String>()
        var out: [RecipeSummary] = []
        for r in data.sections.flatMap(\.items) + data.unsorted where !seen.contains(r.slug) {
            seen.insert(r.slug); out.append(r)
        }
        return out.sorted { ($0.title ?? $0.slug) < ($1.title ?? $1.slug) }
    }

    private func matches(_ r: RecipeSummary) -> Bool { search.isEmpty || (r.title ?? r.slug).localizedCaseInsensitiveContains(search) }

    var body: some View {
        NavigationStack {
            List {
                Section {
                    HStack {
                        TextField("Or type a dish (no recipe)", text: $typed).onSubmit(pickTyped)
                        Button("Add", action: pickTyped).disabled(typed.trimmingCharacters(in: .whitespaces).isEmpty)
                    }
                }
                if data != nil {
                    let fits = data?.sections.first { $0.name == course.title }?.items.filter(matches) ?? []
                    if !fits.isEmpty { Section(course.title) { ForEach(fits, id: \.slug) { pick($0) } } }
                    let rest = all.filter { r in matches(r) && !fits.contains { $0.slug == r.slug } }
                    Section(fits.isEmpty ? "All recipes" : "Everything else") { ForEach(rest, id: \.slug) { pick($0) } }
                } else { ProgressView() }
            }
            .searchable(text: $search, prompt: "Search recipes")
            .navigationTitle("Add to \(course.title.lowercased())").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } } }
            .task { data = await hubQuiet("meals/sections", { try await HubClient.shared.get("/v1/meals/sections") }) }
        }
    }

    private func pick(_ r: RecipeSummary) -> some View {
        Button { onPick(.recipe(r.slug)); dismiss() } label: { RecipeRow(r: r) }
    }

    private func pickTyped() {
        let t = typed.trimmingCharacters(in: .whitespaces)
        guard !t.isEmpty else { return }
        onPick(.dish(t)); dismiss()
    }
}

// MARK: - Shopping list builder

struct ShopBuilderView: View {
    @Environment(HomeStore.self) private var store
    @Environment(Router.self) private var router
    var preset: [String] = []
    @State private var plans: [FeastPlan] = []
    @State private var picked: Set<String> = []
    @State private var weeks = 0          // 0 none, 1-4 weeks from today, -1 pick dates
    @State private var start = Date()
    @State private var end = Calendar.current.date(byAdding: .day, value: 6, to: Date()) ?? Date()
    @State private var skipStaples = false
    @State private var result: ShopBuild?
    @State private var have: Set<String> = []
    @State private var busy = false
    @State private var started = false

    private var canShop: Bool { store.me?.can(.shoppingAdd) ?? false }
    private var scopeEmpty: Bool { picked.isEmpty && weeks == 0 }

    var body: some View {
        List {
            Section {
                ForEach(plans) { p in
                    Toggle(isOn: Binding(get: { picked.contains(p.id) }, set: { on in
                        if on { picked.insert(p.id) } else { picked.remove(p.id) }
                    })) {
                        Label {
                            VStack(alignment: .leading, spacing: 1) {
                                Text(p.name)
                                Text("\(p.dateLine) · \(p.recipeCount ?? 0) dishes").font(.caption).foregroundStyle(Theme.dim)
                            }
                        } icon: { Image(systemName: p.symbol).foregroundStyle(Theme.accent) }
                    }
                }
                Picker("Dinners on the meal plan", selection: $weeks) {
                    Text("None").tag(0)
                    Text("This week").tag(1)
                    Text("2 weeks").tag(2)
                    Text("3 weeks").tag(3)
                    Text("4 weeks").tag(4)
                    Text("Pick dates").tag(-1)
                }
                if weeks == -1 {
                    DatePicker("From", selection: $start, displayedComponents: .date)
                    DatePicker("To", selection: $end, in: start..., displayedComponents: .date)
                }
                Toggle("Leave out salt, pepper, oil and water", isOn: $skipStaples)
                Button { Task { await build() } } label: {
                    HStack { Label(result == nil ? "Make the list" : "Make it again", systemImage: "wand.and.stars"); if busy { Spacer(); ProgressView() } }
                }
                .disabled(scopeEmpty || busy)
            } header: { Text("What to shop for") } footer: {
                Text("Amounts scale to each dish's servings, the same food adds up, and the list is sorted by aisle.")
            }
            if let r = result {
                Section {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("\(r.count - have.count) to buy").font(.title3.weight(.semibold))
                        Text("From \(r.sources.count) recipes\(have.isEmpty ? "" : " · \(have.count) already have")\((r.staplesSkipped ?? 0) > 0 ? " · \(r.staplesSkipped!) staples left out" : "")")
                            .font(.caption).foregroundStyle(Theme.dim)
                    }
                } header: { Text(r.label) }
                ForEach(r.aisles) { a in
                    Section(a.name) {
                        ForEach(a.items) { i in
                            Button { toggle(i) } label: {
                                HStack(alignment: .top, spacing: 10) {
                                    Image(systemName: have.contains(i.key) ? "checkmark.circle.fill" : "circle")
                                        .foregroundStyle(have.contains(i.key) ? Theme.accent : Theme.dim)
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(i.text).strikethrough(have.contains(i.key)).foregroundStyle(have.contains(i.key) ? Theme.dim : Theme.fg)
                                        if !i.recipes.isEmpty { Text(i.recipes.joined(separator: ", ")).font(.caption).foregroundStyle(Theme.dim) }
                                    }
                                }
                            }
                        }
                    }
                }
                Section("Recipes") {
                    ForEach(Array(r.sources.enumerated()), id: \.offset) { _, s in
                        HStack {
                            Text(s.title).foregroundStyle(s.missing == true ? Theme.dim : Theme.fg)
                            Spacer()
                            Text(sourceNote(s)).font(.caption).foregroundStyle(Theme.dim)
                        }
                    }
                }
            }
        }
        .scrollContentBackground(.hidden).background(Backdrop())
        .navigationTitle("Shopping list")
        .safeAreaInset(edge: .bottom) {
            if let r = result, canShop, r.count > have.count {
                Menu {
                    Button { push(r, listID: nil) } label: { Label("New list: \(r.label)", systemImage: "plus") }
                    ForEach(r.lists) { l in Button { push(r, listID: l.id) } label: { Label("Add to \(l.name ?? "list")", systemImage: "cart") } }
                } label: {
                    Label("Send to Shopping", systemImage: "cart.fill.badge.plus").font(.headline)
                        .frame(maxWidth: .infinity, minHeight: 50)
                        .background(Theme.accent, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                        .foregroundStyle(.white)
                }
                .padding(.horizontal).padding(.bottom, 6)
                .disabled(busy)
            }
        }
        .task {
            guard !started else { return }
            started = true
            picked = Set(preset)
            let today = ISO.day(Date())
            if let r: FeastsReply = await hubQuiet("meals/feasts", { try await HubClient.shared.get("/v1/meals/feasts") }) {
                plans = r.plans.filter { $0.endDate >= today || preset.contains($0.id) }.sorted { $0.startDate < $1.startDate }
            }
            if preset.isEmpty && plans.isEmpty { weeks = 1 }
            if !scopeEmpty { await build() }
        }
    }

    private func sourceNote(_ s: ShopBuild.Source) -> String {
        if s.missing == true { return "couldn't read" }
        if s.scaled == true, let f = s.factor, f != 1 { return f == f.rounded() ? "×\(Int(f))" : String(format: "×%.2g", f) }
        return s.why.flatMap { ISO.parseDay($0)?.formatted(.dateTime.weekday(.abbreviated).month(.abbreviated).day()) } ?? ""
    }

    private func toggle(_ i: ShopBuild.Item) {
        if have.contains(i.key) { have.remove(i.key) } else { have.insert(i.key) }
        UIImpactFeedbackGenerator(style: .light).impactOccurred()
    }

    private func scopeBody() -> [String: Any] {
        var body: [String: Any] = ["feasts": Array(picked), "skip_staples": skipStaples]
        if weeks != 0 {
            let s = weeks == -1 ? start : Date()
            let e = weeks == -1 ? max(end, start) : (Calendar.current.date(byAdding: .day, value: weeks * 7 - 1, to: s) ?? s)
            body["start"] = ISO.day(s)
            body["end"] = ISO.day(e)
        }
        return body
    }

    private func build() async {
        busy = true
        defer { busy = false }
        do {
            let r: ShopBuild = try await HubClient.shared.post("/v1/meals/shopping/build", scopeBody())
            let keys = Set(r.items.map(\.key))
            have = have.intersection(keys)
            result = r
        } catch { store.show(error.localizedDescription, isError: true) }
    }

    private func push(_ r: ShopBuild, listID: String?) {
        var body = scopeBody()
        body["skip"] = Array(have)
        if let listID { body["list_id"] = listID } else { body["list_name"] = r.label }
        busy = true
        Task {
            defer { busy = false }
            do {
                let a: ActionReply = try await HubClient.shared.post("/v1/meals/shopping/push", body)
                store.show(a.dryRun == true ? "Practice mode" : (a.message ?? "Sent to Shopping"))
                if a.dryRun != true { router.go(.shopping) }
            } catch { store.show(error.localizedDescription, isError: true) }
        }
    }
}
