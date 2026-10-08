import Darwin
import Foundation

/// 单实例保护：两个 rslite 会互相删掉对方的 tap 聚合设备，所以第二个实例必须退出。
/// 用 flock 而不是 pid 文件：进程崩溃或被 kill 时内核自动释放锁，不会留下假锁。
enum SingleInstance {
    static var defaultLockURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/rslite/lock")
    }

    // 锁的生命周期 = 进程生命周期，fd 故意不关
    private nonisolated(unsafe) static var heldDescriptor: Int32 = -1

    /// 拿到锁返回 true；别的实例已持有返回 false。目录或文件建不出来时抛错，不当成「已有实例」。
    static func acquire(lockURL: URL = defaultLockURL) throws -> Bool {
        guard heldDescriptor < 0 else {
            return true
        }
        let directory = lockURL.deletingLastPathComponent()
        try FileManager.default.createDirectory(
            at: directory,
            withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        // 目录可能是更早以别的权限建出来的；无论新建还是已存在，都收紧到 0700
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: directory.path)
        let fd = open(lockURL.path, O_CREAT | O_RDWR | O_CLOEXEC, 0o600)
        guard fd >= 0 else {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
        if flock(fd, LOCK_EX | LOCK_NB) == 0 {
            heldDescriptor = fd
            return true
        }
        let code = errno
        close(fd)
        if code == EWOULDBLOCK {
            return false
        }
        throw POSIXError(POSIXErrorCode(rawValue: code) ?? .EIO)
    }
}
