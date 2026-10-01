"""Carbon 系统级热键：无需辅助功能权限，Cocoa/Qt 主事件循环负责分发。"""
import ctypes
from PyQt6.QtCore import QObject, Qt, pyqtSignal, pyqtSlot


class EventTypeSpec(ctypes.Structure):
    _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]


class EventHotKeyID(ctypes.Structure):
    _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]


_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
_SIGNATURE = int.from_bytes(b"RSUB", "big")
_KEYCODES = {"P": 35, "L": 37, "M": 46, "G": 5, "C": 8}


def load_carbon():
    carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
    signatures = {
        "GetEventDispatcherTarget": (ctypes.c_void_p, []),
        "InstallEventHandler": (ctypes.c_int32, [ctypes.c_void_p, _HANDLER, ctypes.c_uint32,
                                               ctypes.POINTER(EventTypeSpec), ctypes.c_void_p,
                                               ctypes.POINTER(ctypes.c_void_p)]),
        "RegisterEventHotKey": (ctypes.c_int32, [ctypes.c_uint32, ctypes.c_uint32, EventHotKeyID,
                                               ctypes.c_void_p, ctypes.c_uint32,
                                               ctypes.POINTER(ctypes.c_void_p)]),
        "GetEventParameter": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                             ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                                             ctypes.c_void_p]),
        "UnregisterEventHotKey": (ctypes.c_int32, [ctypes.c_void_p]),
        "RemoveEventHandler": (ctypes.c_int32, [ctypes.c_void_p]),
    }
    for name, (restype, argtypes) in signatures.items():
        fn = getattr(carbon, name)
        fn.restype, fn.argtypes = restype, argtypes
    return carbon


class CarbonHotkeys(QObject):
    activated = pyqtSignal(object)

    def __init__(self, handlers, carbon=None, parent=None):
        super().__init__(parent)
        self.carbon = carbon if carbon is not None else load_carbon()
        self.handlers = handlers
        self.refs = []
        self.handler_ref = ctypes.c_void_p()
        # ☠️ CFUNCTYPE 必须随注册器一直持有；被 GC 后系统回调会跳到已释放内存。
        self._callback = _HANDLER(self._handle_event)
        self.activated.connect(self._dispatch, Qt.ConnectionType.QueuedConnection)
        spec = EventTypeSpec(int.from_bytes(b"keyb", "big"), 6)  # kEventHotKeyPressed
        self.target = self.carbon.GetEventDispatcherTarget()
        status = self.carbon.InstallEventHandler(
            self.target, self._callback, 1, ctypes.byref(spec), None, ctypes.byref(self.handler_ref))
        if status:
            raise OSError(f"Carbon InstallEventHandler: {status}")

    def register(self):
        registered = []
        # macOS Option 对应 Windows Alt；同样是 Ctrl+Option+P/L/M/G/C。
        for hid, (letter, callback) in self.handlers.items():
            ref = ctypes.c_void_p()
            status = self.carbon.RegisterEventHotKey(
                _KEYCODES[letter], (1 << 12) | (1 << 11), EventHotKeyID(_SIGNATURE, hid),
                self.target, 0, ctypes.byref(ref))
            label = f"Ctrl+Option+{letter}"
            if status:
                print(f"⚠️  快捷键 {label} 注册失败（可能被其它程序占用）")
            else:
                self.refs.append(ref)
                registered.append(label)
        if registered:
            print(f"⌨️  全局快捷键已注册(系统级): {', '.join(registered)}")
        return registered

    def _handle_event(self, next_handler, event, user_data):
        try:
            hotkey = EventHotKeyID()
            status = self.carbon.GetEventParameter(
                event, int.from_bytes(b"----", "big"), int.from_bytes(b"hkid", "big"),
                None, ctypes.sizeof(hotkey), None, ctypes.byref(hotkey))
            if not status and hotkey.signature == _SIGNATURE and hotkey.id in self.handlers:
                self.activated.emit(self.handlers[hotkey.id][1])
                return 0
        except Exception as exc:
            print(f"⚠️  快捷键处理错误: {exc}")
        return -9874  # eventNotHandledErr，让其它处理器继续处理

    @pyqtSlot(object)
    def _dispatch(self, callback):
        try:
            callback()
        except Exception as exc:
            print(f"⚠️  快捷键处理错误: {exc}")

    def close(self):
        for ref in self.refs:
            self.carbon.UnregisterEventHotKey(ref)
        self.refs.clear()
        if self.handler_ref.value:
            self.carbon.RemoveEventHandler(self.handler_ref)
            self.handler_ref = ctypes.c_void_p()
