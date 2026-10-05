"""macOS Core Audio Process Tap helper.

Process Tap is available on macOS 14.2+. A tap cannot be opened directly by
PortAudio, so we attach it to a non-private aggregate device and let the
existing sounddevice input path capture that aggregate by name.
"""
from __future__ import annotations

import atexit
import os
import platform
import struct
import uuid
from dataclasses import dataclass, field


DEFAULT_TAP_DEVICE_NAME = "RealtimeSubtitle System Audio"

# 权限不足时常见的 OSStatus（AudioHardwareBase.h）。其余选择子/字典键一律用
# pyobjc CoreAudio 绑定里的真常量——手写 FourCC 和键名极易猜错（'id2p' 曾被写成
# 'tpob'，'taps' 曾被写成 'tap_list'），而猜错的后果是静默退回 BlackHole
kAudioHardwareIllegalOperationError = 0x6E6F7065  # 'nope'
kAudioHardwareUnsupportedOperationError = 0x756E6F70  # 'unop'


@dataclass
class TapHandle:
    tap_id: int
    aggregate_id: int
    device_name: str
    _coreaudio: object = field(repr=False)
    _destroyed: bool = False

    def destroy(self) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        if self.aggregate_id:
            self._coreaudio.destroy_aggregate_device(self.aggregate_id)
        if self.tap_id:
            self._coreaudio.destroy_process_tap(self.tap_id)


def create_system_audio_tap(name: str = DEFAULT_TAP_DEVICE_NAME, _coreaudio=None) -> TapHandle | None:
    if not _is_macos_14_2_or_newer():
        print("⚠️  Core Audio Process Tap 需要 macOS 14.2 或更新版本，已回退旧的输入设备逻辑。")
        return None

    coreaudio = _coreaudio or _CoreAudio()
    tap_id = 0
    aggregate_id = 0
    try:
        _destroy_stale_aggregates(coreaudio, name)

        excluded = coreaudio.process_object_ids_for_pid(os.getpid())
        if not excluded:
            # 拿不到本进程的 AudioObjectID 时只能建全局 tap；风险是如果字幕程序
            # 自己播放提示音，提示音也可能被采进来。
            print("⚠️  无法解析本进程 AudioObjectID，系统音频 tap 将不排除本进程。")

        desc = coreaudio.make_global_tap_description(excluded)
        tap_id = coreaudio.create_process_tap(desc)
        tap_uid = coreaudio.get_tap_uid(tap_id)
        aggregate_uid = f"com.realtimesubtitle.tap.{uuid.uuid4()}"
        # 真机试出来的唯一可用配方（M2/macOS 27，四种组合逐个建、看输入流数）：
        # 私有 tap 只能挂进**私有**聚合设备（非私有的建得出来但输入流是 0）；
        # 还要以默认输出设备为主子设备当时钟、开漂移补偿。私有设备对本进程
        # 可见——PortAudio 就跑在本进程里，"私有会让 PortAudio 枚举不到"是错的
        aggregate_id = coreaudio.create_aggregate_device(
            name, aggregate_uid, tap_uid, coreaudio.default_output_uid())
        handle = TapHandle(tap_id, aggregate_id, name, coreaudio)
        # 异常退出时尽量收尾；残留的聚合设备会一直挂在用户的“音频 MIDI 设置”里。
        atexit.register(handle.destroy)
        return handle
    except CoreAudioError as e:
        print(f"⚠️  创建 macOS 系统音频 tap 失败：{e}")
        if aggregate_id:
            coreaudio.destroy_aggregate_device(aggregate_id)
        if tap_id:
            coreaudio.destroy_process_tap(tap_id)
        return None


def _is_macos_14_2_or_newer() -> bool:
    version = platform.mac_ver()[0]
    parts = tuple(int(p) for p in version.split(".")[:3] if p.isdigit())
    return parts >= (14, 2)


def _destroy_stale_aggregates(coreaudio, name: str) -> None:
    for device_id, device_name in coreaudio.aggregate_devices():
        if device_name == name:
            print(f"🧹 清理上次崩溃残留的 macOS 聚合设备: {name}")
            coreaudio.destroy_aggregate_device(device_id)


class CoreAudioError(RuntimeError):
    pass


def _status_message(action: str, status: int) -> str:
    text = f"{action} 返回 OSStatus {status}"
    if status in (kAudioHardwareIllegalOperationError, kAudioHardwareUnsupportedOperationError):
        text += "；通常是权限不足，需要在 系统设置→隐私与安全性→屏幕与系统音频录制/系统音频录制 里允许终端或 Python"
    return text


class _CoreAudio:
    """pyobjc CoreAudio 绑定上的薄封装（调用约定在真机上逐个试出来的）。

    - AudioObjectGetPropertyData：qualifier 为空时必须传 objc.NULL（传 None 会
      "converting to a C array" 报错），输出缓冲传 None 由 pyobjc 分配，返回
      (status, 实际字节数, bytes)
    - CFString 类属性（名字、tap UID）返回的是 8 字节指针，用 objc_object 包回来
    """

    def __init__(self):
        import CoreAudio as ca
        import objc
        self._ca = ca
        self._objc = objc

    def _addr(self, selector):
        ca = self._ca
        return ca.AudioObjectPropertyAddress(
            selector, ca.kAudioObjectPropertyScopeGlobal, ca.kAudioObjectPropertyElementMain)

    def _get(self, object_id, selector, qualifier=None):
        objc = self._objc
        qual = objc.NULL if qualifier is None else qualifier
        qsize = 0 if qualifier is None else len(qualifier)
        status, size = self._ca.AudioObjectGetPropertyDataSize(object_id, self._addr(selector), qsize, qual, None)
        self._check(status, f"AudioObjectGetPropertyDataSize({selector})")
        status, n, data = self._ca.AudioObjectGetPropertyData(
            object_id, self._addr(selector), qsize, qual, size, None)
        self._check(status, f"AudioObjectGetPropertyData({selector})")
        return bytes(data[:n])

    def _get_cfstring(self, object_id, selector) -> str:
        ptr = struct.unpack("Q", self._get(object_id, selector))[0]
        return str(self._objc.objc_object(c_void_p=ptr)) if ptr else ""

    def make_global_tap_description(self, excluded_processes):
        # 默认不静音原声（CATapUnmuted），用户照常听得到；私有 tap 只给本进程用
        desc = self._ca.CATapDescription.alloc().initStereoGlobalTapButExcludeProcesses_(
            list(excluded_processes))
        desc.setPrivate_(True)
        return desc

    def create_process_tap(self, desc) -> int:
        status, tap_id = self._ca.AudioHardwareCreateProcessTap(desc, None)
        self._check(status, "AudioHardwareCreateProcessTap")
        return int(tap_id)

    def destroy_process_tap(self, tap_id: int) -> None:
        self._check(self._ca.AudioHardwareDestroyProcessTap(tap_id), "AudioHardwareDestroyProcessTap")

    def create_aggregate_device(self, name: str, uid: str, tap_uid: str, main_uid: str) -> int:
        ca = self._ca
        key = lambda k: k.decode() if isinstance(k, bytes) else k  # noqa: E731  pyobjc 给的是 bytes
        desc = {
            key(ca.kAudioAggregateDeviceNameKey): name,
            key(ca.kAudioAggregateDeviceUIDKey): uid,
            key(ca.kAudioAggregateDeviceIsPrivateKey): True,
            key(ca.kAudioAggregateDeviceIsStackedKey): False,
            key(ca.kAudioAggregateDeviceMainSubDeviceKey): main_uid,
            key(ca.kAudioAggregateDeviceSubDeviceListKey): [{key(ca.kAudioSubDeviceUIDKey): main_uid}],
            key(ca.kAudioAggregateDeviceTapListKey): [{
                key(ca.kAudioSubTapUIDKey): tap_uid,
                key(ca.kAudioSubTapDriftCompensationKey): True,
            }],
            key(ca.kAudioAggregateDeviceTapAutoStartKey): True,
        }
        status, aggregate_id = ca.AudioHardwareCreateAggregateDevice(desc, None)
        self._check(status, "AudioHardwareCreateAggregateDevice")
        return int(aggregate_id)

    def default_output_uid(self) -> str:
        ca = self._ca
        dev = struct.unpack("I", self._get(ca.kAudioObjectSystemObject, ca.kAudioHardwarePropertyDefaultOutputDevice))[0]
        return self._get_cfstring(dev, ca.kAudioDevicePropertyDeviceUID)

    def destroy_aggregate_device(self, aggregate_id: int) -> None:
        self._check(self._ca.AudioHardwareDestroyAggregateDevice(aggregate_id),
                    "AudioHardwareDestroyAggregateDevice")

    def get_tap_uid(self, tap_id: int) -> str:
        return self._get_cfstring(tap_id, self._ca.kAudioTapPropertyUID)

    def process_object_ids_for_pid(self, pid: int) -> list[int]:
        try:
            raw = self._get(self._ca.kAudioObjectSystemObject,
                            self._ca.kAudioHardwarePropertyTranslatePIDToProcessObject,
                            struct.pack("i", pid))
        except CoreAudioError:
            return []
        obj = struct.unpack("I", raw)[0]
        return [obj] if obj else []

    def aggregate_devices(self) -> list[tuple[int, str]]:
        ca = self._ca
        raw = self._get(ca.kAudioObjectSystemObject, ca.kAudioHardwarePropertyDevices)
        found = []
        for device_id in struct.unpack(f"{len(raw) // 4}I", raw):
            try:
                cls = struct.unpack("I", self._get(device_id, ca.kAudioObjectPropertyClass))[0]
                if cls == ca.kAudioAggregateDeviceClassID:
                    found.append((device_id, self._get_cfstring(device_id, ca.kAudioObjectPropertyName)))
            except CoreAudioError:
                continue
        return found

    def _check(self, status: int, action: str) -> None:
        if status != 0:
            raise CoreAudioError(_status_message(action, int(status)))
