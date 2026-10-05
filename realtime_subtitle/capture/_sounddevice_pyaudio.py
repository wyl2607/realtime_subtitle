"""Minimal PyAudio-shaped adapter backed by sounddevice for macOS input capture."""
from __future__ import annotations

import sounddevice as sd

paInt16 = "int16"


class _InputStream:
    def __init__(self, stream):
        self._stream = stream

    def read(self, num_frames, exception_on_overflow=True):
        data, overflowed = self._stream.read(num_frames)
        if overflowed and exception_on_overflow:
            raise RuntimeError("Input overflowed")
        return bytes(data)

    def stop_stream(self):
        self._stream.stop()

    def close(self):
        self._stream.close()


class PyAudio:
    def __init__(self):
        self._devices = list(sd.query_devices())

    def terminate(self):
        pass

    def get_device_count(self):
        return len(self._devices)

    def get_device_info_by_index(self, index):
        return _device_info(index, self._devices[index])

    def get_default_input_device_info(self):
        # sd.default.device 初始是 (-1, -1)＝"交给 PortAudio"，不是设备号；
        # 真正的默认输入要问 query_devices(kind="input")
        try:
            dev = sd.query_devices(kind="input")
        except Exception as e:
            raise RuntimeError("没有可用的默认输入设备") from e
        return _device_info(dev["index"], dev)

    def get_input_device_info_generator(self):
        for index, dev in enumerate(self._devices):
            if int(dev.get("max_input_channels") or 0) > 0:
                yield _device_info(index, dev)

    def open(self, format, channels, rate, input, input_device_index, frames_per_buffer):
        if format != paInt16:
            raise ValueError(f"unsupported format: {format}")
        if not input:
            raise ValueError("sounddevice adapter only supports input streams")
        stream = sd.RawInputStream(
            samplerate=rate,
            blocksize=frames_per_buffer,
            device=input_device_index,
            channels=channels,
            dtype="int16",
        )
        stream.start()
        return _InputStream(stream)


def _device_info(index, dev):
    return {
        "name": dev.get("name") or "",
        "index": index,
        "defaultSampleRate": dev.get("default_samplerate") or dev.get("defaultSampleRate"),
        "maxInputChannels": dev.get("max_input_channels") or dev.get("maxInputChannels") or 0,
    }
