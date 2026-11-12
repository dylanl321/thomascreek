import SwiftUI
import SceneKit
import simd

/// The real house, in 3D, drawn natively. The model isn't in the repository: the hub serves it
/// at /v1/assets/house-model in the mockups' web format, and the app keeps a copy for offline use.
/// Format: {mats:{name:"#hex"}, groups:[{k,m,p:[x,y,z...],i:[...]}], lines:[[x1,y1,z1,x2,y2,z2]...]}
/// Metres, Y up, front faces +Z. Lines are the light runs along the eaves.
struct House3DScreen: View {
    @StateObject private var lights = LightsModel.shared
    @State private var scene: SCNScene?
    @State private var error: String?

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            if let scene {
                SceneView(scene: scene, pointOfView: scene.rootNode.childNode(withName: "camera", recursively: false),
                          options: [.allowsCameraControl, .autoenablesDefaultLighting])
                    .ignoresSafeArea(edges: .bottom)
            } else if let error {
                ContentUnavailableView("The house model isn't available", systemImage: "cube.transparent", description: Text(error))
                    .foregroundStyle(.white).environment(\.colorScheme, .dark)
            } else {
                ProgressView("Loading the house…").tint(.white).foregroundStyle(.white)
            }
        }
        .navigationTitle("Thomas Creek in 3D").navigationBarTitleDisplayMode(.inline)
        .task { await load() }
        .onChange(of: lights.status?.look) { _, _ in if let scene { HouseModel.tint(scene, look: lights.status?.currentLook) } }
    }

    private func load() async {
        do {
            let data = try await HouseModel.data()
            let s = try HouseModel.scene(from: data)
            HouseModel.tint(s, look: lights.status?.currentLook ?? .elegant)
            scene = s
        } catch is DecodingError {
            self.error = "The hub's 3D model is in a format this version of the app doesn't understand."
        } catch let e as HouseModelError {
            self.error = e.localizedDescription
        } catch {
            self.error = "Couldn't reach the hub to get the house model. Check you're home or on the VPN."
        }
    }
}

enum HouseModelError: LocalizedError {
    case notOnHub, hubFailed
    var errorDescription: String? {
        switch self {
        case .notOnHub: "The hub doesn't have the 3D model yet. Once it's added there, this view fills in by itself."
        case .hubFailed: "The hub couldn't send the 3D model just now. Pull to try again in a minute."
        }
    }
}

enum HouseModel {
    private struct File: Decodable {
        var mats: [String: String]
        var groups: [Part]
        var lines: [[Float]]?
        struct Part: Decodable { var k: String?; var m: String; var p: [Float]; var i: [Int32] }
    }

    private static var cacheURL: URL {
        FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0].appendingPathComponent("house-model.json")
    }

    /// The hub's copy, falling back to the last one downloaded.
    static func data() async throws -> Data {
        do {
            let req = try await HubClient.shared.authorizedRequest("/v1/assets/house-model")
            var r = req; r.timeoutInterval = 30
            let (d, resp) = try await HubClient.streamSession.data(for: r)
            switch (resp as? HTTPURLResponse)?.statusCode {
            case 200: break
            case 404: throw HouseModelError.notOnHub
            default: throw HouseModelError.hubFailed
            }
            try? d.write(to: cacheURL)
            return d
        } catch {
            if let d = try? Data(contentsOf: cacheURL) { return d }
            throw error
        }
    }

    static func scene(from data: Data) throws -> SCNScene {
        let file = try JSONDecoder().decode(File.self, from: data)
        let scene = SCNScene()
        scene.background.contents = UIColor(red: 0.03, green: 0.05, blue: 0.1, alpha: 1)
        let house = SCNNode(); house.name = "house"
        scene.rootNode.addChildNode(house)

        var minV = SIMD3<Float>(repeating: .greatestFiniteMagnitude), maxV = SIMD3<Float>(repeating: -.greatestFiniteMagnitude)
        for g in file.groups {
            var verts: [SCNVector3] = []
            verts.reserveCapacity(g.p.count / 3)
            for j in stride(from: 0, to: g.p.count - 2, by: 3) {
                let v = SIMD3(g.p[j], g.p[j + 1], g.p[j + 2])
                minV = simd_min(minV, v); maxV = simd_max(maxV, v)
                verts.append(SCNVector3(v.x, v.y, v.z))
            }
            let normals = vertexNormals(verts, g.i)
            let geo = SCNGeometry(sources: [SCNGeometrySource(vertices: verts), SCNGeometrySource(normals: normals)],
                                  elements: [SCNGeometryElement(indices: g.i, primitiveType: .triangles)])
            let mat = SCNMaterial()
            mat.diffuse.contents = UIColor(Color(hexString: file.mats[g.m]) ?? .gray)
            mat.lightingModel = .lambert
            mat.isDoubleSided = true
            if g.m == "warmglass" || g.m == "window" { mat.emission.contents = UIColor(red: 1, green: 0.8, blue: 0.5, alpha: 1).withAlphaComponent(0.35) }
            geo.materials = [mat]
            let node = SCNNode(geometry: geo); node.name = g.k
            house.addChildNode(node)
        }

        // Light runs: a bead every 30 cm.
        let bulbs = SCNNode(); bulbs.name = "bulbs"
        let bead = SCNSphere(radius: 0.06)
        let beadMat = SCNMaterial(); beadMat.lightingModel = .constant
        bead.materials = [beadMat]
        for l in file.lines ?? [] where l.count == 6 {
            let a = SIMD3(l[0], l[1], l[2]), b = SIMD3(l[3], l[4], l[5])
            let n = max(1, Int(simd_distance(a, b) / 0.3))
            for s in 0...n {
                let p = simd_mix(a, b, SIMD3(repeating: Float(s) / Float(n)))
                let node = SCNNode(geometry: bead); node.simdPosition = p
                bulbs.addChildNode(node)
            }
        }
        scene.rootNode.addChildNode(bulbs)

        // Ground and a camera looking at the front (+Z) from above and to the right.
        let center = (minV + maxV) / 2
        let size = simd_length(maxV - minV)
        let ground = SCNNode(geometry: SCNFloor())
        ground.geometry?.firstMaterial?.diffuse.contents = UIColor(red: 0.05, green: 0.08, blue: 0.07, alpha: 1)
        ground.simdPosition = SIMD3(center.x, minV.y - 0.02, center.z)
        scene.rootNode.addChildNode(ground)

        let cam = SCNNode(); cam.name = "camera"
        cam.camera = SCNCamera(); cam.camera?.zFar = Double(size * 6); cam.camera?.fieldOfView = 50
        cam.simdPosition = center + SIMD3(size * 0.45, size * 0.35, size * 0.8)
        cam.look(at: SCNVector3(center.x, center.y, center.z))
        scene.rootNode.addChildNode(cam)

        let moon = SCNNode(); moon.light = SCNLight(); moon.light?.type = .directional
        moon.light?.color = UIColor(red: 0.6, green: 0.7, blue: 1, alpha: 1); moon.light?.intensity = 500
        moon.simdEulerAngles = SIMD3(-.pi / 3, .pi / 5, 0)
        scene.rootNode.addChildNode(moon)
        let ambient = SCNNode(); ambient.light = SCNLight(); ambient.light?.type = .ambient; ambient.light?.intensity = 350
        scene.rootNode.addChildNode(ambient)
        return scene
    }

    /// Colour the bulbs for the current look.
    static func tint(_ scene: SCNScene, look: Look?) {
        guard let bulbs = scene.rootNode.childNode(withName: "bulbs", recursively: false) else { return }
        let festive: [UIColor] = [.systemRed, .systemGreen, UIColor(red: 1, green: 0.8, blue: 0.3, alpha: 1)]
        for (i, n) in bulbs.childNodes.enumerated() {
            let c: UIColor
            switch look {
            case .off: c = UIColor(white: 0.15, alpha: 1)
            case .festive: c = festive[i % festive.count]
            default: c = UIColor(look?.tint ?? Look.elegant.tint)
            }
            // Each bead shares geometry, so give it its own material copy once.
            if n.geometry?.firstMaterial?.name != "own" {
                n.geometry = n.geometry?.copy() as? SCNGeometry
                let m = SCNMaterial(); m.lightingModel = .constant; m.name = "own"
                n.geometry?.materials = [m]
            }
            n.geometry?.firstMaterial?.diffuse.contents = c
            n.geometry?.firstMaterial?.emission.contents = c
        }
    }

    private static func vertexNormals(_ v: [SCNVector3], _ idx: [Int32]) -> [SCNVector3] {
        var acc = [SIMD3<Float>](repeating: .zero, count: v.count)
        func s(_ x: SCNVector3) -> SIMD3<Float> { SIMD3(Float(x.x), Float(x.y), Float(x.z)) }
        for t in stride(from: 0, to: idx.count - 2, by: 3) {
            let a = Int(idx[t]), b = Int(idx[t + 1]), c = Int(idx[t + 2])
            guard a < v.count, b < v.count, c < v.count else { continue }
            let n = simd_cross(s(v[b]) - s(v[a]), s(v[c]) - s(v[a]))
            acc[a] += n; acc[b] += n; acc[c] += n
        }
        return acc.map { n in
            let u = simd_length(n) > 0 ? simd_normalize(n) : SIMD3<Float>(0, 1, 0)
            return SCNVector3(u.x, u.y, u.z)
        }
    }
}
