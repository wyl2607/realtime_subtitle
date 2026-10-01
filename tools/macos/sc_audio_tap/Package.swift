// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "sc-audio-tap",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "sc-audio-tap", targets: ["ScAudioTap"])],
    targets: [.executableTarget(name: "ScAudioTap")]
)
