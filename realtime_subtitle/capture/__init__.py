import sys

from .audio_capture import AudioCapture, PAUSE_FLAG_FILE, STOP_FLAG_FILE


def make_audio_capture(callback, on_status=None):
    """仅在选择 macOS 后端时导入子进程采集，Windows 保持 WASAPI。"""
    if sys.platform == "darwin":
        from .macos_capture import MacOSAudioCapture
        return MacOSAudioCapture(callback, on_status)
    if sys.platform == "win32":
        return AudioCapture(callback, on_status)
    raise RuntimeError(f"系统音频捕获不支持此平台: {sys.platform}")


__all__ = ["AudioCapture", "make_audio_capture", "PAUSE_FLAG_FILE", "STOP_FLAG_FILE"]
