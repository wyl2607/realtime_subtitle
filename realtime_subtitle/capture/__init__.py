"""采集源按需导入，系统字幕路径无需安装/加载音频驱动。"""

__all__ = ["AudioCapture", "PAUSE_FLAG_FILE", "STOP_FLAG_FILE"]


def __getattr__(name):
    # ☠️ 导入 live_captions 也会先执行本文件；这里不能提前拉起 soxr/音频设备。
    if name in __all__:
        from . import audio_capture
        return getattr(audio_capture, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
