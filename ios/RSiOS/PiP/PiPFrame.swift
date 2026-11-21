import CoreMedia
import CoreVideo
import UIKit

/// 把画好的字幕图转成画中画能吃的帧。
enum PiPFrame {
    static func pixelBuffer(from image: UIImage) -> CVPixelBuffer? {
        let attrs: [String: Any] = [
            kCVPixelBufferCGImageCompatibilityKey as String: true,
            kCVPixelBufferCGBitmapContextCompatibilityKey as String: true,
            kCVPixelBufferWidthKey as String: 1280,
            kCVPixelBufferHeightKey as String: 360,
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
            // 画中画窗口由系统进程（Pegasus）绘制，帧要跨进程传过去，只有 IOSurface 支撑的
            // 像素缓冲才能共享。少了这一项，App 内的预览照常显示，画中画里却一帧字幕都没有，
            // 日志只有一行 PixelBufferSharing err=-15596（2026-11-21 真机）。
            kCVPixelBufferIOSurfacePropertiesKey as String: [String: Any](),
        ]

        var pixelBuffer: CVPixelBuffer?
        let status = CVPixelBufferCreate(kCFAllocatorDefault, 1280, 360,
                                          kCVPixelFormatType_32BGRA, attrs as CFDictionary,
                                          &pixelBuffer)
        guard status == kCVReturnSuccess, let pb = pixelBuffer else { return nil }

        CVPixelBufferLockBaseAddress(pb, [])
        defer { CVPixelBufferUnlockBaseAddress(pb, []) }

        guard let context = CGContext(data: CVPixelBufferGetBaseAddress(pb),
                                       width: 1280, height: 360,
                                       bitsPerComponent: 8, bytesPerRow: CVPixelBufferGetBytesPerRow(pb),
                                       space: CGColorSpaceCreateDeviceRGB(),
                                       bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue),
              let cgImage = image.cgImage else { return nil }

        context.draw(cgImage, in: CGRect(x: 0, y: 0, width: 1280, height: 360))
        return pb
    }

    static func displayImmediately(_ sampleBuffer: CMSampleBuffer) -> CMSampleBuffer {
        CMSetAttachment(sampleBuffer, key: kCMSampleAttachmentKey_DisplayImmediately, value: kCFBooleanTrue, attachmentMode: kCMAttachmentMode_ShouldNotPropagate)
        return sampleBuffer
    }
}
