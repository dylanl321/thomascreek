import RoomPlan
import SceneKit
import SwiftUI

/// Orbitable dollhouse preview.
struct DollhouseView: UIViewRepresentable {
    let rooms: [CapturedRoom]
    let colors: [String: String]

    func makeUIView(context: Context) -> SCNView {
        let view = SCNView()
        let built = DollhouseScene.build(rooms: rooms, colors: colors)
        view.scene = built.scene
        view.pointOfView = built.camera
        view.allowsCameraControl = true
        view.defaultCameraController.interactionMode = .orbitTurntable
        view.defaultCameraController.target = built.center
        view.defaultCameraController.inertiaEnabled = true
        view.antialiasingMode = .multisampling4X
        view.backgroundColor = .clear
        return view
    }

    func updateUIView(_ uiView: SCNView, context: Context) {}
}
