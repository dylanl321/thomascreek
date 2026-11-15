import Foundation

// Phase 2 (not in v1): an LLM vision model reviews the photos together with the RoomPlan
// geometry and works out what each thing really is ("black shelves with backlit candy jars",
// "recessed light", "smart plug"), picks materials and flags smart devices. It only asks
// Dylan a question when it is truly unsure.
//
// The plan: a reviewer reads a finished package folder, returns a `RoomReview`, and the app
// writes it next to the other files as review.json and sets `Manifest.reviewFile`.
// Nothing calls this yet; `NoReviewer` is the placeholder.

protocol RoomReviewer {
    func review(packageFolder: URL, manifest: Manifest) async throws -> RoomReview
}

struct RoomReview: Codable {
    var schemaVersion = 1
    var model: String?
    var items: [ReviewedItem]
    /// Questions for Dylan, only when the model is unsure (e.g. "Is this a light or a speaker?").
    var questions: [String]
}

struct ReviewedItem: Codable {
    /// RoomPlan identifier of the wall, door, window or object; nil for things RoomPlan missed.
    var id: String?
    var label: String
    var material: String?
    var colorHex: String?
    var isSmartDevice: Bool?
    var confidence: Double?
}

struct NoReviewer: RoomReviewer {
    func review(packageFolder: URL, manifest: Manifest) async throws -> RoomReview {
        RoomReview(model: nil, items: [], questions: [])
    }
}
