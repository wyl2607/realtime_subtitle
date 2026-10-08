// swift-tools-version: 6.2

import PackageDescription

let package = Package(
    name: "macos-native",
    platforms: [.macOS(.v26)],
    products: [
        .executable(name: "rsbench", targets: ["rsbench"]),
        .executable(name: "rstranslate", targets: ["rstranslate"]),
        .executable(name: "rslite", targets: ["rslite"]),
    ],
    targets: [
        .executableTarget(name: "rsbench"),
        .executableTarget(name: "rstranslate"),
        .executableTarget(name: "rslite"),
    ]
)
