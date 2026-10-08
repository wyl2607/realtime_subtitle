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
    var remote: URL?
    var tokenFile = "~/.config/rslite/remote-token"
}

@main
struct RSLite {
    static func main() async {
        do {
            let options = try parse(Array(CommandLine.arguments.dropFirst()))
            if #available(macOS 27.0, *) {
                if options.headless {
                    try await runHeadless(options)
                } else {
                    await MainActor.run {
                        runApp(options)
                    }
                }
            } else {
                throw RSLiteError.unsupported("rslite 需要 macOS 27 或更新版本")
            }
        } catch {
            fputs("error: \(error)\n", stderr)
            exit(1)
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
            case "--source", "--src", "--dst", "--remote", "--token-file":
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
                case "--token-file":
                    options.tokenFile = value
                default:
                    guard let url = URL(string: value), let scheme = url.scheme,
                          ["ws", "wss"].contains(scheme), url.host != nil else {
                        throw RSLiteError.usage("--remote 需要 ws:// 或 wss:// 地址，例如 ws://100.x.y.z:8790/v1")
                    }
                    options.remote = url
                }
                i += 2
            default:
                throw RSLiteError.usage(Self.usage)
            }
        }
        return options
    }

    private static let usage = "usage: rslite [--source tap|mic|file:PATH] [--src de-DE] [--dst zh-Hans] [--headless] [--remote ws://HOST:PORT/v1] [--token-file PATH]"

    /// 给了 --remote 才走远程；否则和原来一样直接用本机 Pipeline。
    @available(macOS 27.0, *)
    private static func makeEngine(
        _ options: Options,
        callbacks: PipelineCallbacks,
        onMode: @escaping @Sendable (SubtitleMode) -> Void
    ) -> SubtitleEngine {
        let base = PipelineConfig(
            sourceSpec: options.source,
            sourceLocaleID: options.src,
            targetLanguageID: options.dst
        )
        guard let remote = options.remote else {
            return Pipeline(config: base, callbacks: callbacks)
        }
        let config = RemoteConfig(
            url: remote,
            token: RemoteTokenFile.read(path: options.tokenFile),
            tokenPath: options.tokenFile,
            base: base
        )
        return RemotePipeline(config: config, callbacks: callbacks, onMode: onMode)
    }

    @available(macOS 27.0, *)
    private static func runHeadless(_ options: Options) async throws {
        let startedAt = ContinuousClock().now
        let clock = ContinuousClock()

        let pipeline = makeEngine(
            options,
            callbacks: PipelineCallbacks(
                onVolatile: { text in
                    writeEvent(clock: clock, startedAt: startedAt, ev: "volatile", id: nil, text: text)
                },
                onFinal: { id, text in
                    writeEvent(clock: clock, startedAt: startedAt, ev: "final", id: id, text: text)
                },
                onTranslation: { id, text in
                    writeEvent(clock: clock, startedAt: startedAt, ev: "translation", id: id, text: text)
                },
                onStatus: { text in
                    writeEvent(clock: clock, startedAt: startedAt, ev: "status", id: nil, text: text)
                }
            ),
            onMode: { mode in
                writeEvent(clock: clock, startedAt: startedAt, ev: "status", id: nil, text: "模式：\(mode.label)")
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
                onFinal: { id, text in
                    Task { @MainActor in overlay.addFinal(id: id, text: text) }
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
        text: String
    ) {
        var object: [String: Any] = [
            "t": round3(startedAt.duration(to: clock.now).seconds),
            "ev": ev,
            "text": text,
        ]
        if let id {
            object["id"] = id
        }
        guard let data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]) else {
            return
        }
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data([0x0A]))
    }

    private static func round3(_ value: Double) -> Double {
        (value * 1000).rounded() / 1000
    }
}

extension Duration {
    var seconds: Double {
        Double(components.seconds) + Double(components.attoseconds) / 1e18
    }
}
