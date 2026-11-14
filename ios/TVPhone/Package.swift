// swift-tools-version: 5.10
import PackageDescription

let package = Package(
    name: "TVPhone",
    platforms: [
        .iOS(.v17),
        .macOS(.v14),
    ],
    products: [
        .library(name: "TVPhone", targets: ["TVPhone"]),
    ],
    targets: [
        .target(name: "TVPhone"),
        .testTarget(name: "TVPhoneTests", dependencies: ["TVPhone"]),
    ]
)
