import Foundation
import Observation
import UIKit

enum AppStateMonitorTransition: Equatable {
    case background
    case foreground

    var isInBackground: Bool {
        self == .background
    }
}

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

    nonisolated static func transition(for notificationName: Notification.Name) -> AppStateMonitorTransition? {
        switch notificationName {
        case UIApplication.didEnterBackgroundNotification:
            return .background
        case UIApplication.willEnterForegroundNotification:
            return .foreground
        default:
            return nil
        }
    }

    @objc private func didEnterBackground() {
        isInBackground = Self.transition(for: UIApplication.didEnterBackgroundNotification)?.isInBackground ?? isInBackground
    }

    @objc private func willEnterForeground() {
        isInBackground = Self.transition(for: UIApplication.willEnterForegroundNotification)?.isInBackground ?? isInBackground
    }
}
