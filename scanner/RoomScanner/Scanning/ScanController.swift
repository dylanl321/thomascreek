import ARKit
import Foundation
import RoomPlan

/// A spot marked during the scan: "this part is the dining nook", or "this is the hallway, not
/// this room". The Mac converter (scanner/tools) uses them to split a big shared space into areas
/// and to cut off floor and furniture that RoomPlan picked up through a doorway.
struct AreaMarker: Codable, Hashable {
    var name: String
    /// Camera position when the spot was marked (world space, meters). Only x and z are used.
    var position: [Float]
    /// Which room of the session ("Next room" count) the marker belongs to.
    var roomIndex: Int
    /// true: not part of this room (the hallway beyond a doorway, the next room of an open plan).
    var exclude: Bool
    var timestamp: Double
}

/// What a finished scan session hands to `PackageWriter`.
struct ScanOutput {
    var rooms: [CapturedRoom]
    var descriptions: [String]
    var photos: [CapturedPhoto]
    var photoDirectory: URL
    var worldMap: ARWorldMap?
    var areas: [AreaMarker]
}

/// Drives one RoomPlan session: the standard coaching UI, automatic photos, optional
/// "Next room" (same AR session, so rooms line up for StructureBuilder), area markers, and RoomBuilder.
final class ScanController: NSObject, ObservableObject, RoomCaptureViewDelegate {
    enum Phase: Equatable {
        case scanning
        case processing
        case failed(String)
    }

    @Published var phase: Phase = .scanning
    @Published var roomCount = 0
    @Published var photoCount = 0
    @Published var areaCount = 0
    /// Shown briefly after a room RoomPlan couldn't build was skipped.
    @Published var notice: String?

    let arSession = ARSession()
    private(set) lazy var captureView: RoomCaptureView = {
        let view = RoomCaptureView(frame: .zero, arSession: arSession)
        view.delegate = self
        return view
    }()

    var onComplete: ((ScanOutput) -> Void)?

    private let photos = PhotoCapturer()
    private var rooms: [CapturedRoom] = []
    /// One per room in the session, in order: the room being scanned is `descriptions[rooms.count]`.
    private var descriptions: [String]
    private var areas: [AreaMarker] = []
    private var continueAfterProcessing = false
    private var cancelled = false
    private var started = false
    private var worldMap: ARWorldMap?

    init(description: String) {
        descriptions = [description]
        super.init()
    }

    required init?(coder: NSCoder) { nil }
    func encode(with coder: NSCoder) {}

    func start() {
        guard !started else { return }
        started = true
        captureView.captureSession.run(configuration: RoomCaptureSession.Configuration())
        photos.start(session: arSession) { [weak self] count in self?.photoCount = count }
    }

    /// Finish this room but keep the AR session running, then start scanning the next one.
    func nextRoom(description: String) {
        guard phase == .scanning else { return }
        descriptions.append(description)
        continueAfterProcessing = true
        phase = .processing
        // No photos while walking to the next room: they would count against the room just finished.
        photos.pause()
        captureView.captureSession.stop(pauseARSession: false)
    }

    func finish() {
        guard phase == .scanning else { return }
        continueAfterProcessing = false
        phase = .processing
        photos.stop()
        // Save the world map before the session pauses, for relocalizing later scans.
        arSession.getCurrentWorldMap { [weak self] map, _ in
            DispatchQueue.main.async {
                guard let self else { return }
                self.worldMap = map
                self.captureView.captureSession.stop()
            }
        }
    }

    func cancel() {
        cancelled = true
        photos.stop()
        photos.discardAll()
        captureView.captureSession.stop()
    }

    /// Mark the spot the phone is standing in as a named area of the room being scanned.
    func markArea(name: String, exclude: Bool) {
        guard phase == .scanning, let frame = arSession.currentFrame else { return }
        let p = frame.camera.transform.columns.3
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        areas.append(AreaMarker(name: trimmed.isEmpty ? (exclude ? "Not this room" : "Area \(areas.count + 1)") : trimmed,
                                position: [p.x, p.y, p.z], roomIndex: rooms.count, exclude: exclude,
                                timestamp: frame.timestamp))
        areaCount = areas.count
    }

    // MARK: RoomCaptureViewDelegate

    func captureView(shouldPresent roomDataForProcessing: CapturedRoomData, error: Error?) -> Bool {
        guard !cancelled else { return false }
        Task { @MainActor in
            let index = rooms.count
            do {
                let builder = RoomBuilder(options: [.beautifyObjects])
                let room = try await builder.capturedRoom(from: roomDataForProcessing)
                rooms.append(room)
                roomCount = rooms.count
            } catch {
                // Drop everything that belonged to the room that failed: its description (not the
                // next room's), its photos and its area markers, so later rooms keep their own labels.
                if index < descriptions.count { descriptions.remove(at: index) }
                photos.discardRoom(index)
                areas.removeAll { $0.roomIndex == index }
                areas = areas.map { var a = $0; if a.roomIndex > index { a.roomIndex -= 1 }; return a }
                areaCount = areas.count
                if rooms.isEmpty && !continueAfterProcessing {
                    phase = .failed("RoomPlan couldn't build the room: \(error.localizedDescription)")
                    return
                }
                notice = "Skipped a room RoomPlan couldn't build."
            }
            if continueAfterProcessing {
                continueAfterProcessing = false
                photos.beginRoom(rooms.count)
                captureView.captureSession.run(configuration: RoomCaptureSession.Configuration())
                phase = .scanning
            } else {
                complete()
            }
        }
        // We present our own dollhouse preview instead of RoomPlan's result view.
        return false
    }

    private func complete() {
        let output = ScanOutput(rooms: rooms, descriptions: Array(descriptions.prefix(rooms.count)),
                                photos: photos.photos.filter { $0.record.roomIndex < rooms.count },
                                photoDirectory: photos.directory, worldMap: worldMap,
                                areas: areas.filter { $0.roomIndex < rooms.count })
        onComplete?(output)
    }
}
