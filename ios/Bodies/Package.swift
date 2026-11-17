// swift-tools-version: 5.10
// The hub bodies the iPhone decodes, tested on the Mac without a simulator: `cd ios/Bodies && swift test` (#206).
// The app compiles Sources/HubBodies directly (project.yml), so the tests decode with the app's own models.
// The fixtures are written by the hub's own routes: server/tests/test_ios_bodies.py.
import PackageDescription

let package = Package(
    name: "HubBodies",
    platforms: [.iOS(.v17), .macOS(.v14)],
    products: [.library(name: "HubBodies", targets: ["HubBodies"])],
    targets: [
        .target(name: "HubBodies"),
        .testTarget(name: "HubBodiesTests", dependencies: ["HubBodies"], resources: [.copy("Fixtures")]),
    ]
)
