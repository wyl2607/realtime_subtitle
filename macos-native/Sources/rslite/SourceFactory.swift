import Foundation

enum SourceFactoryError: Error, CustomStringConvertible {
    case unsupported(String)

    var description: String {
        switch self {
        case .unsupported(let message):
            return message
        }
    }
}

func makeAudioSource(_ spec: String) throws -> AudioSource {
    if spec.hasPrefix("file:") {
        let path = String(spec.dropFirst("file:".count))
        return try FileSource(path: path)
    }

    if spec == "tap" {
        return ProcessTapSource()
    }

    if spec == "mic" {
        return MicSource()
    }

    throw SourceFactoryError.unsupported("未知音频来源：\(spec)")
}
