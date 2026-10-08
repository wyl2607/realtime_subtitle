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
        throw SourceFactoryError.unsupported("采集模块未接入: tap") // 合并 Capture/ 后改成 ProcessTapSource()
    }

    if spec == "mic" {
        throw SourceFactoryError.unsupported("采集模块未接入: mic") // 合并 Capture/ 后改成 MicSource()
    }

    throw SourceFactoryError.unsupported("未知音频来源：\(spec)")
}
