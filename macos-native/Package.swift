// swift-tools-version: 6.2

import PackageDescription

let package = Package(
    name: "macos-native",
    platforms: [.macOS(.v26)],
    products: [
        .executable(name: "rsbench", targets: ["rsbench"]),
    ],
    targets: [
        .executableTarget(name: "rsbench"),
    ]
)
