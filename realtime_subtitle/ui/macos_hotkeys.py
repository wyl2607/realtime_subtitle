"""macOS global hotkeys via Carbon RegisterEventHotKey.

Carbon hotkey events are delivered by the app's main Cocoa/Qt event loop, so
registration must happen on the main thread after QApplication exists.
"""
from __future__ import annotations

import ctypes
import sys
import threading
from collections.abc import Callable


OSStatus = ctypes.c_int32
UInt32 = ctypes.c_uint32
OptionBits = UInt32
EventHotKeyRef = ctypes.c_void_p
EventHandlerRef = ctypes.c_void_p
EventHandlerCallRef = ctypes.c_void_p
EventRef = ctypes.c_void_p
EventTargetRef = ctypes.c_void_p
EventParamName = UInt32
EventParamType = UInt32
ByteCount = ctypes.c_uint32


def _fourcc(value: str) -> int:
    return int.from_bytes(value.encode("ascii"), "big")


eventClassKeyboard = _fourcc("keyb")
kEventHotKeyPressed = 5
kEventParamDirectObject = _fourcc("----")
typeEventHotKeyID = _fourcc("hkid")

# HIToolbox/Events.h: controlKey = 1 << 12, optionKey = 1 << 11.
controlKey = 0x1000
optionKey = 0x0800
_MODIFIERS = controlKey | optionKey

# HIToolbox/Events.h kVK_ANSI_* virtual key codes; these are layout-independent
# hardware positions for the same user-facing Ctrl+Alt letters as Windows.
kVK_ANSI_P = 0x23
kVK_ANSI_L = 0x25
kVK_ANSI_M = 0x2E
kVK_ANSI_G = 0x05
kVK_ANSI_C = 0x08


class EventTypeSpec(ctypes.Structure):
    _fields_ = [
        ("eventClass", UInt32),
        ("eventKind", UInt32),
    ]


class EventHotKeyID(ctypes.Structure):
    _fields_ = [
        ("signature", UInt32),
        ("id", UInt32),
    ]


EventHandlerUPP = ctypes.CFUNCTYPE(OSStatus, EventHandlerCallRef, EventRef, ctypes.c_void_p)

_SIGNATURE = _fourcc("RSHK")
_HOTKEYS: dict[str, tuple[int, int]] = {
    "Ctrl+Alt+P": (1, kVK_ANSI_P),
    "Ctrl+Alt+L": (2, kVK_ANSI_L),
    "Ctrl+Alt+M": (3, kVK_ANSI_M),
    "Ctrl+Alt+G": (4, kVK_ANSI_G),
    "Ctrl+Alt+C": (5, kVK_ANSI_C),
}

_carbon = None
_configured = False
_target = None
_handler_ref = EventHandlerRef()
_handler_proc = None
_registered_refs: dict[int, EventHotKeyRef] = {}
_handlers_by_id: dict[int, Callable[[], object]] = {}


def _carbon_handle():
    global _carbon
    if _carbon is None:
        _carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
    return _carbon


def _set_signature(func, argtypes, restype):
    try:
        func.argtypes = argtypes
        func.restype = restype
    except AttributeError:
        pass


def _configure(carbon) -> None:
    global _configured
    if _configured:
        return
    _set_signature(
        carbon.GetApplicationEventTarget,
        [],
        EventTargetRef,
    )
    _set_signature(
        carbon.InstallEventHandler,
        [EventTargetRef, EventHandlerUPP, UInt32, ctypes.POINTER(EventTypeSpec), ctypes.c_void_p,
         ctypes.POINTER(EventHandlerRef)],
        OSStatus,
    )
    _set_signature(
        carbon.RegisterEventHotKey,
        [UInt32, UInt32, EventHotKeyID, EventTargetRef, OptionBits, ctypes.POINTER(EventHotKeyRef)],
        OSStatus,
    )
    _set_signature(
        carbon.UnregisterEventHotKey,
        [EventHotKeyRef],
        OSStatus,
    )
    _set_signature(
        carbon.GetEventParameter,
        [EventRef, EventParamName, EventParamType, ctypes.c_void_p, ByteCount, ctypes.c_void_p,
         ctypes.c_void_p],
        OSStatus,
    )
    _configured = True


def _ensure_handler(carbon):
    global _target, _handler_ref, _handler_proc
    if _target is None:
        _target = carbon.GetApplicationEventTarget()
    if not _target:
        return -1
    if _handler_proc is not None:
        return 0

    def _on_hotkey(_next_handler, event, _user_data):
        hotkey_id = EventHotKeyID()
        status = carbon.GetEventParameter(
            event,
            kEventParamDirectObject,
            typeEventHotKeyID,
            None,
            ctypes.sizeof(hotkey_id),
            None,
            ctypes.byref(hotkey_id),
        )
        if status != 0 or hotkey_id.signature != _SIGNATURE:
            return status
        handler = _handlers_by_id.get(hotkey_id.id)
        if handler is None:
            return 0
        try:
            handler()
        except Exception as exc:
            print(f"⚠️  快捷键处理错误: {exc}")
        return 0

    # ctypes 回调对象必须保存在模块变量上；否则被 GC 后 Carbon 再回调会崩溃。
    _handler_proc = EventHandlerUPP(_on_hotkey)
    spec = EventTypeSpec(eventClassKeyboard, kEventHotKeyPressed)
    return carbon.InstallEventHandler(
        _target,
        _handler_proc,
        1,
        ctypes.byref(spec),
        None,
        ctypes.byref(_handler_ref),
    )


def register(handlers: dict[str, Callable[[], object]]) -> list[str]:
    """Register supported macOS hotkeys and return the labels that succeeded."""
    if sys.platform != "darwin":
        return []
    if threading.current_thread() is not threading.main_thread():
        print("⚠️  macOS 全局快捷键注册必须在主线程执行")
        return []
    carbon = _carbon_handle()
    _configure(carbon)
    status = _ensure_handler(carbon)
    if status != 0:
        print(f"⚠️  macOS 全局快捷键初始化失败（Carbon错误 {status}）")
        return []

    registered: list[str] = []
    for label, handler in handlers.items():
        hotkey = _HOTKEYS.get(label)
        if hotkey is None:
            print(f"⚠️  快捷键 {label} 注册失败（macOS暂未配置该键码）")
            continue
        hotkey_id, keycode = hotkey
        if hotkey_id in _registered_refs:
            carbon.UnregisterEventHotKey(_registered_refs.pop(hotkey_id))
        ref = EventHotKeyRef()
        status = carbon.RegisterEventHotKey(
            keycode,
            _MODIFIERS,
            EventHotKeyID(_SIGNATURE, hotkey_id),
            _target,
            0,
            ctypes.byref(ref),
        )
        if status == 0:
            _registered_refs[hotkey_id] = ref
            _handlers_by_id[hotkey_id] = handler
            registered.append(label)
        else:
            _handlers_by_id.pop(hotkey_id, None)
            print(f"⚠️  快捷键 {label} 注册失败（可能被其它程序占用，Carbon错误 {status}）")
    return registered


def unregister_all() -> None:
    """Unregister all hotkeys registered by this module."""
    if not _registered_refs:
        _handlers_by_id.clear()
        return
    carbon = _carbon_handle()
    for hotkey_id, ref in list(_registered_refs.items()):
        try:
            carbon.UnregisterEventHotKey(ref)
        except Exception as exc:
            print(f"⚠️  快捷键注销失败（id={hotkey_id}）: {exc}")
        finally:
            _registered_refs.pop(hotkey_id, None)
            _handlers_by_id.pop(hotkey_id, None)
