import AppKit
import Foundation

enum RSLiteError: Error, CustomStringConvertible {
    case usage(String)
    case unsupported(String)

    var description: String {
        switch self {
        case .usage(let message), .unsupported(let message):
            return message
        }
    }
}

struct Options {
    var source = "tap"
    var src = "de-DE"
    var dst = "zh-Hans"
    var headless = false
    var mode = RSLiteMode.auto
    var selftest = false
}

@main
struct RSLite {
    // ☠️ main 必须是同步的。async main 里再 `app.run()`，NSApplication 的事件循环
    // 是跑在一个主队列任务**里面**的：主队列是串行的，这个任务永不返回，于是之后
    // 所有派到 MainActor 的任务（窗口显示、启动识别）都排在后面永远轮不到——
    // 2026-10-08 真机：进程活着、CPU 0%、窗口不出现、也不弹权限。
    // 同步 main 下 app.run()/dispatchMain() 自己就在排空主队列。
    static func main() {
        let options: Options
        do {
            options = try parse(Array(CommandLine.arguments.dropFirst()))
        } catch {
            fputs("error: \(error)\n", stderr)
            exit(1)
        }
        if options.selftest {
            runSelfTest()
            return
        }
        do {
            if !(options.headless && options.source.hasPrefix("file:")) {
                guard try SingleInstance.acquire() else {
                    fputs("error: rslite 已在运行，本实例退出\n", stderr)
                    exit(2)
                }
            }
        } catch {
            fputs("error: 单实例锁失败：\(error)\n", stderr)
            exit(1)
        }
        guard #available(macOS 27.0, *) else {
            fputs("error: rslite 需要 macOS 27 或更新版本\n", stderr)
            exit(1)
        }
        if options.headless {
            Task {
                do {
                    try await runHeadless(options)
                    exit(0)
                } catch {
                    fputs("error: \(error)\n", stderr)
                    exit(1)
                }
            }
            dispatchMain()
        } else {
            MainActor.assumeIsolated {
                runApp(options)
            }
        }
    }

    private static func parse(_ args: [String]) throws -> Options {
        var options = Options()
        var i = 0
        while i < args.count {
            let key = args[i]
            switch key {
            case "--headless":
                options.headless = true
                i += 1
            case "--selftest":
                options.selftest = true
                i += 1
            case "--source", "--src", "--dst", "--mode":
                guard i + 1 < args.count else {
                    throw RSLiteError.usage("missing value for \(key)")
                }
                let value = args[i + 1]
                switch key {
                case "--source":
                    options.source = value
                case "--src":
                    options.src = value
                case "--dst":
                    options.dst = value
                default:
                    guard let mode = RSLiteMode(rawValue: value) else {
                        throw RSLiteError.usage("--mode 只能是 auto、local 或 hybrid")
                    }
                    options.mode = mode
                }
                i += 2
            default:
                throw RSLiteError.usage(Self.usage)
            }
        }
        return options
    }

    private static let usage = "usage: rslite [--source tap|mic|file:PATH] [--src de-DE] [--dst zh-Hans] [--headless] [--mode auto|local|hybrid] [--selftest]"

    @available(macOS 27.0, *)
    private static func makeEngine(
        _ options: Options,
        callbacks: PipelineCallbacks,
        onMode: @escaping @Sendable (SubtitleMode) -> Void,
        onNodeFinal: @escaping @Sendable (String, Double?, Double?, String, String?) -> Void = { _, _, _, _, _ in }
    ) -> SubtitleEngine {
        let config = PipelineConfig(
            sourceSpec: options.source,
            sourceLocaleID: options.src,
            targetLanguageID: options.dst
        )
        return HybridEngine(
            config: config,
            mode: options.mode,
            callbacks: callbacks,
            onMode: onMode,
            onNodeFinal: onNodeFinal
        )
    }

    @available(macOS 27.0, *)
    private static func runHeadless(_ options: Options) async throws {
        let startedAt = ContinuousClock().now
        let clock = ContinuousClock()
        // 与 Overlay 同一套 P5 逻辑（LineStore），让 headless 也能观察到「节点句替换了几条本机句」
        let store = HeadlessLineStore()

        let pipeline = makeEngine(
            options,
            callbacks: PipelineCallbacks(
                onVolatile: { text in
                    writeEvent(clock: clock, startedAt: startedAt, ev: "volatile", id: nil, text: text)
                },
                onFinal: { id, text, t0, t1 in
                    store.addLocal(key: id, t0: t0, t1: t1, text: text)
                    writeEvent(clock: clock, startedAt: startedAt, ev: "final", id: id, text: text, t0: t0, t1: t1)
                },
                onTranslation: { id, text in
                    store.setTranslation(key: id, text: text)
                    writeEvent(clock: clock, startedAt: startedAt, ev: "translation", id: id, text: text)
                },
                onStatus: { text in
                    writeEvent(clock: clock, startedAt: startedAt, ev: "status", id: nil, text: text)
                }
            ),
            onMode: { mode in
                writeEvent(clock: clock, startedAt: startedAt, ev: "status", id: nil, text: "模式：\(mode.label)")
            },
            onNodeFinal: { nodeID, t0, t1, src, dst in
                let replaced = store.applyNode(nodeID: nodeID, t0: t0, t1: t1, src: src, dst: dst)
                // 日志不带正文：只报替换了哪些本机句（id）
                writeEvent(clock: clock, startedAt: startedAt, ev: "replace", id: nil,
                           text: "P5 node=\(nodeID) replaced=\(replaced.count) ids=\(replaced.sorted())", t0: t0, t1: t1)
                writeEvent(clock: clock, startedAt: startedAt, ev: "final", id: nil, text: "[\(nodeID)] \(src)", t0: t0, t1: t1)
                if let dst {
                    writeEvent(clock: clock, startedAt: startedAt, ev: "translation", id: nil, text: "[\(nodeID)] \(dst)")
                }
            }
        )

        try await pipeline.start()
        await pipeline.waitUntilFinished()
    }

    @available(macOS 27.0, *)
    @MainActor
    private static func runApp(_ options: Options) {
        NSApplication.shared.setActivationPolicy(.accessory)

        var pipeline: SubtitleEngine?
        let app = NSApplication.shared

        let overlay = OverlayController(
            onPauseChanged: { paused in
                guard let pipeline else {
                    return
                }
                let currentPipeline = pipeline
                Task {
                    if paused {
                        await currentPipeline.pause()
                    } else {
                        await currentPipeline.resume()
                    }
                }
            },
            onQuit: {
                let currentPipeline = pipeline
                Task {
                    await currentPipeline?.stop()
                    await MainActor.run {
                        app.terminate(nil)
                    }
                }
            }
        )

        pipeline = makeEngine(
            options,
            callbacks: PipelineCallbacks(
                onVolatile: { text in
                    Task { @MainActor in overlay.setVolatile(text) }
                },
                onFinal: { id, text, t0, t1 in
                    Task { @MainActor in overlay.addFinal(id: id, text: text, t0: t0, t1: t1) }
                },
                onTranslation: { id, text in
                    Task { @MainActor in overlay.addTranslation(id: id, text: text) }
                },
                onStatus: { text in
                    Task { @MainActor in overlay.setStatus(text) }
                },
                onFinished: {
                    Task { @MainActor in overlay.setStatus("音频来源已结束") }
                }
            ),
            onMode: { mode in
                Task { @MainActor in overlay.setMode(mode) }
            },
            onNodeFinal: { nodeID, t0, t1, src, dst in
                Task { @MainActor in overlay.addNodeFinal(nodeID: nodeID, t0: t0, t1: t1, src: src, dst: dst) }
            }
        )

        overlay.show()
        let currentPipeline = pipeline
        Task {
            do {
                try await currentPipeline?.start()
            } catch {
                await MainActor.run {
                    overlay.setStatus("启动失败：\(error.localizedDescription)")
                }
            }
        }
        app.run()
    }

    private static func writeEvent(
        clock: ContinuousClock,
        startedAt: ContinuousClock.Instant,
        ev: String,
        id: Int?,
        text: String,
        t0: Double? = nil,
        t1: Double? = nil
    ) {
        var object: [String: Any] = [
            "t": round3(startedAt.duration(to: clock.now).seconds),
            "ev": ev,
            "text": text,
        ]
        if let id {
            object["id"] = id
        }
        if let t0 { object["t0"] = round3(t0) }
        if let t1 { object["t1"] = round3(t1) }
        guard let data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]) else {
            return
        }
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data([0x0A]))
    }

    private static func round3(_ value: Double) -> Double {
        (value * 1000).rounded() / 1000
    }

    private static func runSelfTest() {
        var failures: [String] = []
        failures.append(contentsOf: LineStore.selfTest().map { "LineStore: \($0)" })
        failures.append(contentsOf: Capability.selfTest().map { "Capability: \($0)" })
        if #available(macOS 27.0, *) {
            failures.append(contentsOf: NodeRouter.selfTest().map { "NodeRouter: \($0)" })
        } else {
            failures.append("NodeRouter: 需要 macOS 27")
        }
        if failures.isEmpty {
            print("SELFTEST OK")
            exit(0)
        }
        for failure in failures {
            print("SELFTEST FAIL \(failure)")
        }
        exit(1)
    }
}

extension Duration {
    var seconds: Double {
        Double(components.seconds) + Double(components.attoseconds) / 1e18
    }
}

/// headless 用的 LineStore 线程安全包装（回调来自不同线程）。
final class HeadlessLineStore: @unchecked Sendable {
    private let lock = NSLock()
    private var store = LineStore()

    func addLocal(key: Int, t0: Double?, t1: Double?, text: String) {
        lock.withLockVoid { store.addLocal(key: key, t0: t0, t1: t1, text: text) }
    }

    func setTranslation(key: Int, text: String) {
        lock.withLockVoid { store.setTranslation(key: key, text: text) }
    }

    func applyNode(nodeID: String, t0: Double?, t1: Double?, src: String, dst: String?) -> [Int] {
        lock.withLockValue { store.applyNode(nodeID: nodeID, t0: t0, t1: t1, srcText: src, dstText: dst) }
    }
}
