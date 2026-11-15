import ARKit
import CoreImage
import ImageIO
import UIKit

/// A small sRGB copy of one photo kept in memory for color sampling.
struct PixelSample {
    let width: Int
    let height: Int
    let bytes: [UInt8]          // RGBA8, row 0 = top of the image
    let transform: simd_float4x4
    let inverseTransform: simd_float4x4
    let fx: Float, fy: Float, cx: Float, cy: Float   // already scaled to width x height

    /// Projects a world point into this image (ARKit camera looks down -Z, +Y up).
    func project(_ p: simd_float3) -> (u: Float, v: Float, depth: Float)? {
        let c = inverseTransform * simd_float4(p.x, p.y, p.z, 1)
        guard c.z < -0.15 else { return nil }
        let depth = -c.z
        let u = fx * (c.x / depth) + cx
        let v = fy * (-c.y / depth) + cy
        guard u >= 3, v >= 3, u < Float(width - 3), v < Float(height - 3) else { return nil }
        return (u, v, depth)
    }

    func collect(u: Float, v: Float, into r: inout [UInt8], _ g: inout [UInt8], _ b: inout [UInt8]) {
        let x0 = Int(u), y0 = Int(v)
        for y in (y0 - 2)...(y0 + 2) {
            for x in (x0 - 2)...(x0 + 2) {
                let i = (y * width + x) * 4
                r.append(bytes[i]); g.append(bytes[i + 1]); b.append(bytes[i + 2])
            }
        }
    }
}

struct CapturedPhoto {
    let record: PhotoRecord
    let sample: PixelSample
}

/// Grabs a photo from the RoomPlan AR session every ~1.5 s when the camera has moved or turned
/// enough, up to `maxPerRoom` per room. Polls `currentFrame` so RoomPlan keeps its own delegate.
final class PhotoCapturer {
    let directory: URL
    let maxPerRoom = 40
    let interval: TimeInterval = 1.5

    private var timer: Timer?
    private weak var session: ARSession?
    private var lastTransform: simd_float4x4?
    private var lastTime: TimeInterval = 0
    private var countThisRoom = 0
    private var totalCount = 0
    private var roomIndex = 0
    private var inFlight = false
    private var paused = false
    private var onCount: ((Int) -> Void)?

    private let queue = DispatchQueue(label: "me.dlewis.roomscanner.photos", qos: .userInitiated)
    private var storage: [CapturedPhoto] = []     // only touched on `queue`
    private let context = CIContext()
    private let srgb = CGColorSpace(name: CGColorSpace.sRGB)!

    init() {
        directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("scan-\(UUID().uuidString)", isDirectory: true)
            .appendingPathComponent(PackageFormat.photosFolder, isDirectory: true)
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    var photos: [CapturedPhoto] { queue.sync { storage } }

    func start(session: ARSession, onCount: @escaping (Int) -> Void) {
        self.session = session
        self.onCount = onCount
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: 0.3, repeats: true) { [weak self] _ in self?.tick() }
    }

    func stop() {
        timer?.invalidate()
        timer = nil
    }

    /// Stop taking photos until `beginRoom` (between rooms, while RoomPlan processes).
    func pause() {
        paused = true
    }

    /// Photos from now on belong to room `index` of the session.
    func beginRoom(_ index: Int) {
        roomIndex = index
        countThisRoom = 0
        lastTransform = nil
        paused = false
    }

    /// Forget a room's photos (RoomPlan couldn't build it) and delete their files.
    func discardRoom(_ index: Int) {
        let dir = directory
        queue.async { [weak self] in
            guard let self else { return }
            for photo in self.storage where photo.record.roomIndex == index {
                try? FileManager.default.removeItem(at: dir.appendingPathComponent((photo.record.file as NSString).lastPathComponent))
            }
            self.storage.removeAll { $0.record.roomIndex == index }
            let count = self.storage.count
            DispatchQueue.main.async { self.onCount?(count) }
        }
    }

    /// Cancelled scan: delete the temporary photo folder.
    func discardAll() {
        let dir = directory.deletingLastPathComponent()
        queue.async { [weak self] in
            self?.storage.removeAll()
            try? FileManager.default.removeItem(at: dir)
        }
    }

    private func tick() {
        guard !paused, !inFlight, countThisRoom < maxPerRoom, let frame = session?.currentFrame else { return }
        guard case .normal = frame.camera.trackingState else { return }
        guard frame.timestamp - lastTime >= interval else { return }
        let transform = frame.camera.transform
        if let last = lastTransform {
            let moved = simd_distance(transform.columns.3, last.columns.3)
            let f1 = simd_normalize(simd_make_float3(transform.columns.2))
            let f0 = simd_normalize(simd_make_float3(last.columns.2))
            let angle = acos(max(-1, min(1, simd_dot(f1, f0))))
            if moved < 0.3 && angle < 0.26 { return }   // < 30 cm and < 15 degrees: too similar
        }
        lastTime = frame.timestamp
        lastTransform = transform
        countThisRoom += 1
        inFlight = true

        let index = totalCount
        totalCount += 1
        let pixelBuffer = frame.capturedImage
        let intrinsics = frame.camera.intrinsics
        let resolution = frame.camera.imageResolution
        let timestamp = frame.timestamp
        let room = roomIndex

        queue.async { [weak self] in
            guard let self else { return }
            let photo = self.encode(pixelBuffer: pixelBuffer, index: index, room: room, timestamp: timestamp,
                                    transform: transform, intrinsics: intrinsics, resolution: resolution)
            if let photo { self.storage.append(photo) }
            let count = self.storage.count
            DispatchQueue.main.async {
                self.inFlight = false
                self.onCount?(count)
            }
        }
    }

    private func encode(pixelBuffer: CVPixelBuffer, index: Int, room: Int, timestamp: TimeInterval,
                        transform: simd_float4x4, intrinsics: simd_float3x3, resolution: CGSize) -> CapturedPhoto? {
        let image = CIImage(cvPixelBuffer: pixelBuffer)
        let name = String(format: "photo_%03d.jpg", index)
        let quality = CIImageRepresentationOption(rawValue: kCGImageDestinationLossyCompressionQuality as String)
        guard let jpeg = context.jpegRepresentation(of: image, colorSpace: srgb, options: [quality: 0.8]) else { return nil }
        do { try jpeg.write(to: directory.appendingPathComponent(name)) } catch { return nil }

        // Quarter-size sRGB copy for color sampling.
        let scale: CGFloat = 0.25
        let small = image.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
        let w = Int(resolution.width * scale), h = Int(resolution.height * scale)
        guard w > 8, h > 8,
              let cg = context.createCGImage(small, from: CGRect(x: 0, y: 0, width: w, height: h),
                                             format: .RGBA8, colorSpace: srgb) else { return nil }
        var bytes = [UInt8](repeating: 0, count: w * h * 4)
        let drawn: Bool = bytes.withUnsafeMutableBytes { raw in
            guard let ctx = CGContext(data: raw.baseAddress, width: w, height: h, bitsPerComponent: 8,
                                      bytesPerRow: w * 4, space: srgb,
                                      bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) else { return false }
            ctx.draw(cg, in: CGRect(x: 0, y: 0, width: w, height: h))
            return true
        }
        guard drawn else { return nil }

        let k = intrinsics
        let s = Float(w) / Float(resolution.width)
        let t = transform
        let record = PhotoRecord(
            file: "\(PackageFormat.photosFolder)/\(name)", timestamp: timestamp, roomIndex: room,
            imageWidth: Int(resolution.width), imageHeight: Int(resolution.height),
            transform: [t.columns.0, t.columns.1, t.columns.2, t.columns.3].flatMap { [$0.x, $0.y, $0.z, $0.w] },
            intrinsics: [k.columns.0, k.columns.1, k.columns.2].flatMap { [$0.x, $0.y, $0.z] },
            fx: k[0][0], fy: k[1][1], cx: k[2][0], cy: k[2][1])
        let sample = PixelSample(width: w, height: h, bytes: bytes, transform: t, inverseTransform: t.inverse,
                                 fx: k[0][0] * s, fy: k[1][1] * s, cx: k[2][0] * s, cy: k[2][1] * s)
        return CapturedPhoto(record: record, sample: sample)
    }
}
