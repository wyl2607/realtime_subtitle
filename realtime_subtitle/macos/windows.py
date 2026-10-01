"""Qt 的 winId 在 Cocoa 上是 NSView，必须先取它所属的 NSWindow。"""
import logging
import sys


def native_window(widget):
    if sys.platform != "darwin":
        return None
    from PyQt6.QtWidgets import QApplication
    # ☠️ offscreen 的 winId 是伪句柄，交给 objc 会直接段错误，不能靠 except 兜底。
    if QApplication.platformName() != "cocoa":
        return None
    import objc
    return objc.objc_object(c_void_p=int(widget.winId())).window()


def accessory_app():
    if sys.platform != "darwin":
        return
    from PyQt6.QtWidgets import QApplication
    if QApplication.platformName() == "cocoa":
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
        NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)


def configure_overlay(widget, click_through=False):
    try:
        window = native_window(widget)
        if window is None:
            return False
        from AppKit import (
            NSStatusWindowLevel, NSWindowCollectionBehaviorCanJoinAllSpaces,
            NSWindowCollectionBehaviorFullScreenAuxiliary, NSWindowCollectionBehaviorStationary,
        )
        window.setLevel_(NSStatusWindowLevel)
        window.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorFullScreenAuxiliary
            | NSWindowCollectionBehaviorStationary)
        window.setHidesOnDeactivate_(False)
        window.setIgnoresMouseEvents_(bool(click_through))
        return True
    except Exception:
        logging.exception("macOS 字幕窗口属性设置失败")
        return False


def set_click_through(widget, enabled):
    try:
        window = native_window(widget)
        if window is None:
            return False
        window.setIgnoresMouseEvents_(bool(enabled))
        return True
    except Exception:
        logging.exception("macOS 鼠标穿透设置失败")
        return False


def focus_window(widget):
    """Accessory 没有 Dock 图标，打开设置/历史仍需显式激活应用。"""
    if sys.platform != "darwin":
        return
    try:
        configure_overlay(widget)
        window = native_window(widget)
        if window is not None:
            from AppKit import NSApplication
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            window.makeKeyAndOrderFront_(None)
        widget.raise_()
        widget.activateWindow()
    except Exception:
        logging.exception("macOS 附属窗口激活失败")
