import AVFoundation
import Foundation
import Speech
import Translation

enum BenchError: Error, CustomStringConvertible {
    case usage(String)
    case missing(String)
    case unsupported(String)

    var description: String {
        switch self {
        case .usage(let s), .missing(let s), .unsupported(let s):
            return s
        }
    }
}

struct RefLine: Decodable {
    let id: String
    let ref: String
    let audio_s: Double?

    enum CodingKeys: String, CodingKey {
        case id, ref, audio_s
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        if let s = try? c.decode(String.self, forKey: .id) {
            id = s
        } else {
            id = String(try c.decode(Int.self, forKey: .id))
        }
        ref = try c.decode(String.self, forKey: .ref)
        audio_s = try? c.decode(Double.self, forKey: .audio_s)
    }
}

struct Pair: Decodable {
    let id: String
    let de: String
    let zh: String

    enum CodingKeys: String, CodingKey {
        case id, de, zh
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        if let s = try? c.decode(String.self, forKey: .id) {
            id = s
        } else {
            id = String(try c.decode(Int.self, forKey: .id))
        }
        de = try c.decode(String.self, forKey: .de)
        zh = try c.decode(String.self, forKey: .zh)
    }
}

actor ASRCollector {
    private var finalTexts: [String] = []
    private var firstVolatileS: Double?

    func accept(_ result: SpeechTranscriber.Result, startedAt: ContinuousClock.Instant, clock: ContinuousClock) {
        let text = String(result.text.characters).trimmingCharacters(in: .whitespacesAndNewlines)
        if result.isFinal {
            if !text.isEmpty {
                finalTexts.append(text)
            }
        } else if firstVolatileS == nil {
            firstVolatileS = startedAt.duration(to: clock.now).seconds
        }
    }

    func snapshot() -> (String, Double?) {
        (finalTexts.joined(separator: " "), firstVolatileS)
    }
}

extension Duration {
    var seconds: Double {
        Double(components.seconds) + Double(components.attoseconds) / 1e18
    }
}

@main
struct RsBench {
    static func main() async {
        do {
            let args = Array(CommandLine.arguments.dropFirst())
            guard let cmd = args.first else {
                throw BenchError.usage(Self.usage)
            }
            let rest = Array(args.dropFirst())
            if cmd == "asr" {
                if #available(macOS 27.0, *) {
                    try await runASR(rest)
                } else {
                    throw BenchError.unsupported("SpeechAnalyzer/SpeechTranscriber benchmark requires macOS 27.")
                }
            } else if cmd == "translate" {
                if #available(macOS 26.0, *) {
                    try await runTranslate(rest)
                } else {
                    throw BenchError.unsupported("Translation benchmark requires macOS 26.")
                }
            } else {
                throw BenchError.usage(Self.usage)
            }
        } catch {
            fputs("error: \(error)\n", stderr)
            exit(1)
        }
    }

    static let usage = """
    usage:
      rsbench asr --wav-dir D --refs R --locale de-DE --out O.jsonl [--volatile]
      rsbench translate --pairs P --n 40 --src de --dst zh-Hans --out O.jsonl
    """

    static func parse(_ args: [String]) throws -> ([String: String], Set<String>) {
        var values: [String: String] = [:]
        var flags: Set<String> = []
        var i = 0
        while i < args.count {
            let key = args[i]
            guard key.hasPrefix("--") else {
                throw BenchError.usage("unexpected argument: \(key)")
            }
            if key == "--volatile" {
                flags.insert(key)
                i += 1
                continue
            }
            guard i + 1 < args.count else {
                throw BenchError.usage("missing value for \(key)")
            }
            values[key] = args[i + 1]
            i += 2
        }
        return (values, flags)
    }

    static func require(_ values: [String: String], _ key: String) throws -> String {
        guard let value = values[key], !value.isEmpty else {
            throw BenchError.usage("missing \(key)")
        }
        return value
    }

    @available(macOS 27.0, *)
    static func runASR(_ args: [String]) async throws {
        let (values, flags) = try parse(args)
        let wavDir = URL(fileURLWithPath: try require(values, "--wav-dir"), isDirectory: true)
        let refs = try readRefs(URL(fileURLWithPath: try require(values, "--refs")))
        let localeID = try require(values, "--locale")
        let outURL = URL(fileURLWithPath: try require(values, "--out"))
        let wantsVolatile = flags.contains("--volatile")

        let locale = Locale(identifier: localeID)
        guard let supported = await SpeechTranscriber.supportedLocale(equivalentTo: locale) else {
            throw BenchError.unsupported("unsupported speech locale: \(localeID)")
        }
        try await ensureSpeechAssets(locale: supported)

        try Data().write(to: outURL)
        let handle = try FileHandle(forWritingTo: outURL)
        defer { try? handle.close() }

        var audioS = 0.0
        var procS = 0.0
        var volatileValues: [Double] = []
        var volatileError: String?

        for ref in refs {
            let wav = wavDir.appendingPathComponent(ref.id).appendingPathExtension("wav")
            let audioFile = try AVAudioFile(forReading: wav)
            let thisAudioS = audioFile.durationSeconds
            audioS += ref.audio_s ?? thisAudioS

            let measured = try await transcribe(wav: wav, locale: supported, reporting: [])
            procS += measured.procS

            var rec: [String: Any] = [
                "id": ref.id,
                "hyp": measured.hyp,
                "proc_s": round3(measured.procS),
            ]

            if wantsVolatile && volatileError == nil {
                do {
                    let v = try await transcribe(wav: wav, locale: supported, reporting: [.volatileResults])
                    if let first = v.firstVolatileS {
                        volatileValues.append(first)
                        rec["first_volatile_s"] = round3(first)
                    } else {
                        rec["first_volatile_s"] = NSNull()
                    }
                } catch {
                    volatileError = String(describing: error)
                }
            }

            try writeJSONLine(rec, to: handle)
        }

        var summary: [String: Any] = [
            "summary": true,
            "n": refs.count,
            "audio_s": round1(audioS),
            "proc_s": round3(procS),
            "rtf": audioS > 0 ? round3(procS / audioS) : NSNull(),
            "peak_rss_mb": round1(peakRSSMB()),
        ]
        if wantsVolatile {
            if let volatileError {
                summary["first_volatile_error"] = volatileError
            } else if !volatileValues.isEmpty {
                summary["first_volatile_p50_s"] = round3(percentile(volatileValues, 0.5))
                summary["first_volatile_n"] = volatileValues.count
            } else {
                summary["first_volatile_error"] = "no volatile result emitted"
            }
        } else {
            summary["first_volatile"] = "disabled; pass --volatile to measure"
        }
        try writeJSONLine(summary, to: handle)
    }

    @available(macOS 27.0, *)
    static func ensureSpeechAssets(locale: Locale) async throws {
        let probe = SpeechTranscriber(locale: locale, preset: .transcription)
        let status = await AssetInventory.status(forModules: [probe])
        fputs("speech asset status: \(status)\n", stderr)
        guard status != .installed else {
            return
        }
        guard let request = try await AssetInventory.assetInstallationRequest(supporting: [probe]) else {
            return
        }
        let progress = request.progress
        let reporter = Task {
            while !progress.isFinished {
                fputs(String(format: "speech asset download: %.1f%%\n", progress.fractionCompleted * 100), stderr)
                try? await Task.sleep(for: .seconds(1))
            }
        }
        do {
            try await request.downloadAndInstall()
            reporter.cancel()
            fputs("speech asset installed\n", stderr)
        } catch {
            reporter.cancel()
            throw error
        }
    }

    @available(macOS 27.0, *)
    static func transcribe(
        wav: URL,
        locale: Locale,
        reporting: Set<SpeechTranscriber.ReportingOption>
    ) async throws -> (hyp: String, procS: Double, firstVolatileS: Double?) {
        let transcriber = SpeechTranscriber(
            locale: locale,
            transcriptionOptions: [],
            reportingOptions: reporting,
            attributeOptions: []
        )
        let analyzer = SpeechAnalyzer(modules: [transcriber])
        let audioFile = try AVAudioFile(forReading: wav)
        let collector = ASRCollector()
        let clock = ContinuousClock()
        let start = clock.now

        let resultTask = Task {
            for try await result in transcriber.results {
                await collector.accept(result, startedAt: start, clock: clock)
            }
        }

        _ = try await analyzer.analyzeSequence(from: audioFile)
        try await analyzer.finalizeAndFinishThroughEndOfInput()
        try await resultTask.value

        let elapsed = start.duration(to: clock.now).seconds
        let snap = await collector.snapshot()
        return (snap.0, elapsed, snap.1)
    }

    @available(macOS 26.0, *)
    static func runTranslate(_ args: [String]) async throws {
        let (values, _) = try parse(args)
        let pairURL = URL(fileURLWithPath: try require(values, "--pairs"))
        let n = Int(values["--n"] ?? "40") ?? 40
        let src = Locale.Language(identifier: try require(values, "--src"))
        let dst = Locale.Language(identifier: try require(values, "--dst"))
        let outURL = URL(fileURLWithPath: try require(values, "--out"))
        let pairs = Array(try readPairs(pairURL).prefix(n))

        let availability = LanguageAvailability()
        let status = await availability.status(from: src, to: dst)
        fputs("translation language status: \(status)\n", stderr)
        guard status == .installed else {
            throw BenchError.unsupported("translation language pair is \(status). Install it in System Settings -> General -> Language & Region -> Translation Languages.")
        }

        let session = TranslationSession(installedSource: src, target: dst)
        let clock = ContinuousClock()
        let coldStart = clock.now
        try await session.prepareTranslation()
        let coldS = coldStart.duration(to: clock.now).seconds

        try Data().write(to: outURL)
        let handle = try FileHandle(forWritingTo: outURL)
        defer { try? handle.close() }

        var times: [Double] = []
        for pair in pairs {
            let start = clock.now
            let response = try await session.translate(pair.de)
            let s = start.duration(to: clock.now).seconds
            times.append(s)
            try writeJSONLine([
                "id": pair.id,
                "de": pair.de,
                "ref": pair.zh,
                "hyp": response.targetText.trimmingCharacters(in: .whitespacesAndNewlines),
                "s": round3(s),
            ], to: handle)
        }

        try writeJSONLine([
            "summary": true,
            "n": pairs.count,
            "cold_s": round3(coldS),
            "p50": round3(percentile(times, 0.5)),
            "p90": round3(percentile(times, 0.9)),
            "peak_rss_mb": round1(peakRSSMB()),
        ], to: handle)
    }

    static func readRefs(_ url: URL) throws -> [RefLine] {
        let text = try String(contentsOf: url, encoding: .utf8)
        let decoder = JSONDecoder()
        return try text.split(whereSeparator: \.isNewline).map { line in
            try decoder.decode(RefLine.self, from: Data(String(line).utf8))
        }
    }

    static func readPairs(_ url: URL) throws -> [Pair] {
        try JSONDecoder().decode([Pair].self, from: Data(contentsOf: url))
    }

    static func writeJSONLine(_ object: [String: Any], to handle: FileHandle) throws {
        let data = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
        handle.write(data)
        handle.write(Data([0x0A]))
    }

    static func percentile(_ values: [Double], _ q: Double) -> Double {
        guard !values.isEmpty else {
            return 0
        }
        let sorted = values.sorted()
        let idx = min(sorted.count - 1, max(0, Int(Double(sorted.count - 1) * q + 0.5)))
        return sorted[idx]
    }

    static func round1(_ x: Double) -> Double {
        (x * 10).rounded() / 10
    }

    static func round3(_ x: Double) -> Double {
        (x * 1000).rounded() / 1000
    }

    static func peakRSSMB() -> Double {
        var usage = rusage()
        getrusage(RUSAGE_SELF, &usage)
        return Double(usage.ru_maxrss) / 1_000_000.0
    }
}

extension AVAudioFile {
    var durationSeconds: Double {
        guard processingFormat.sampleRate > 0 else {
            return 0
        }
        return Double(length) / processingFormat.sampleRate
    }
}
