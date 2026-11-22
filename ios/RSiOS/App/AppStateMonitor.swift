import Foundation
import Observation
import UIKit

extension DateFormatter {
    static let timeFormatter: DateFormatter = {
        let df = DateFormatter()
        df.dateFormat = "HH:mm:ss"
        return df
    }()
}

@MainActor
@Observable
final class AppStateMonitor {
    static let shared = AppStateMonitor()

    var isInBackground = false

    private init() {
        NotificationCenter.default.addObserver(self, selector: #selector(didEnterBackground), name: UIApplication.didEnterBackgroundNotification, object: nil)
        NotificationCenter.default.addObserver(self, selector: #selector(willEnterForeground), name: UIApplication.willEnterForegroundNotification, object: nil)
    }

    @objc private func didEnterBackground() {
        DispatchQueue.main.async {
            self.isInBackground = true
        }
    }

    @objc private func willEnterForeground() {
        DispatchQueue.main.async {
            self.isInBackground = false
        }
    }
}
