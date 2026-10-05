"""Whisper backend selection and construction."""
from __future__ import annotations

import platform
import sys

import realtime_subtitle.config as config


def selected_whisper_backend() -> str:
    backend = getattr(config, "WHISPER_BACKEND", "auto")
    backend = str(backend or "auto").strip().lower()
    if backend != "auto":
        return backend
    if sys.platform == "darwin" and platform.machine().lower() == "arm64":
        return "mlx"
    return "faster-whisper"


def create_whisper_model():
    backend = selected_whisper_backend()
    if backend == "mlx":
        from realtime_subtitle.asr.mlx_backend import MlxWhisperModel

        return MlxWhisperModel(config.WHISPER_MLX_REPO)
    if backend != "faster-whisper":
        raise ValueError(f"未知 Whisper 后端: {backend}")

    from realtime_subtitle.translate import translator_queue

    WhisperModel = translator_queue._ensure_ml_deps()
    # 先只认本地缓存：默认路径每次启动都去 HuggingFace 做一轮
    # etag 检查（实测热缓存下多花 1.4 秒，网络差时是十几秒超时）。
    # 只有本地没有模型（首次运行）才回落到网络下载
    try:
        return WhisperModel(
            config.WHISPER_MODEL,
            device=config.WHISPER_DEVICE,
            compute_type=config.WHISPER_COMPUTE_TYPE,
            local_files_only=True,
        )
    except Exception as e:
        # 不只是"没缓存"会走到这（CUDA错/缓存损坏也会），把真实原因
        # 带上——否则驱动问题会被误报成"在下载"，排障方向全错
        print(f"   本地缓存不可用({e.__class__.__name__}: {e})，"
              f"尝试从网络下载模型（首次需要几分钟）...")
        return WhisperModel(
            config.WHISPER_MODEL,
            device=config.WHISPER_DEVICE,
            compute_type=config.WHISPER_COMPUTE_TYPE,
        )
