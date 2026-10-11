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
        // Swift 6 给 @objc 覆写（hitTest 等）插的运行期主线程检查，在辅助功能（AX）命中测试
        // 回调里崩过：swift_task_isMainExecutor 拿到空执行器 → SIGSEGV（2026-10-11 19:39 崩溃报告，
        // CLAUDE.md §7 第 25 条）。编译期并发检查照旧，只关运行期这层。
        .executableTarget(
            name: "rslite",
            swiftSettings: [.unsafeFlags(["-Xfrontend", "-disable-dynamic-actor-isolation"])]
        ),
    ]
)
