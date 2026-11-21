import ReplayKit
import SwiftUI

struct BroadcastPickerView: UIViewRepresentable {
    func makeUIView(context: Context) -> RPSystemBroadcastPickerView {
        // 必须给非零尺寸：系统在 init 时按这个尺寸建内部按钮，零尺寸建出来的按钮不可见也点不到。
        let picker = RPSystemBroadcastPickerView(frame: CGRect(x: 0, y: 0, width: 60, height: 60))
        picker.preferredExtension = "com.yilinwang.realtimesubtitle.broadcast"
        picker.showsMicrophoneButton = false
        // 系统按钮默认画成黑色，深色界面上看不见（真机上只剩一块空白，用户找不到开播入口）。
        // 换成 label 色：深色模式白、浅色模式黑。
        for case let button as UIButton in picker.subviews {
            button.imageView?.tintColor = .label
        }
        return picker
    }

    func updateUIView(_ uiView: RPSystemBroadcastPickerView, context: Context) {}
}
