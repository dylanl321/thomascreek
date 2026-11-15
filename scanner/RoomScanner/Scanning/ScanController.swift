import ARKit
import Foundation
import RoomPlan

/// What a finished scan session hands to `PackageWriter`.
struct ScanOutput {
    var rooms: [CapturedRoom]
    var descriptions: [String]
    var photos: [CapturedPhoto]
    var photoDirectory: URL
    var worldMap: ARWorldMap?
}

/// Drives one RoomPlan session: the standard coaching UI, automatic photos, optional
/// "Next room" (same AR session, so rooms line up for StructureBuilder), and RoomBuilder.
final class ScanController: NSObject, ObservableObject, RoomCaptureViewDelegate {
    enum Phase: Equatable {
        case scanning
        case processing
        case failed(String)
    }

    @Published var phase: Phase = .scanning
    @Published var roomCount = 0
    @Published var photoCount = 0

    let arSession = ARSession()
    private(set) lazy var captureView: RoomCaptureView = {
        let view = RoomCaptureView(frame: .zero, arSession: arSession)
        view.delegate = self
        return view
    }()

    var onComplete: ((ScanOutput) -> Void)?

    private let photos = PhotoCapturer()
    private var rooms: [CapturedRoom] = []
    private var descriptions: [String]
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
        captureView.captureSession.stop()
    }

    // MARK: RoomCaptureViewDelegate

    func captureView(shouldPresent roomDataForProcessing: CapturedRoomData, error: Error?) -> Bool {
        guard !cancelled else { return false }
        Task { @MainActor in
            do {
                let builder = RoomBuilder(options: [.beautifyObjects])
                let room = try await builder.capturedRoom(from: roomDataForProcessing)
                rooms.append(room)
                roomCount = rooms.count
            } catch {
                if continueAfterProcessing { descriptions.removeLast() }
                if rooms.isEmpty && !continueAfterProcessing {
                    phase = .failed("RoomPlan couldn't build the room: \(error.localizedDescription)")
                    return
                }
            }
            if continueAfterProcessing {
                continueAfterProcessing = false
                photos.nextRoom()
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
                                photos: photos.photos, photoDirectory: photos.directory, worldMap: worldMap)
        onComplete?(output)
    }
}
