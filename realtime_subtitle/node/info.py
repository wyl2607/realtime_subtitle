"""节点能力与状态的收集（`GET /v1/info`，RFC P1）。

只用标准库：gateway 常驻，不能因为这里多 import 一个 numpy/mlx 就把内存
推过 50MB（RFC 架构选择第 1 条）。

所有系统命令（ioreg / pmset）都走 `run` 这一个可注入接口，单测在 Linux 上
用假输出替身，真机行为留给 Mac 验证。

隐私（S8）：`in_use` 只给布尔值，原始空闲秒数不出这个模块；`hw_hash` 只给
sha256 的前 16 位，原始 IOPlatformUUID 不外传。

「探测不出来」一律按对路由最保守的方向处理（in_use=True、on_ac=False）：
宁可少用这台机器，也不要在不知道用户是否正在用电脑时抢资源。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger("realtime_subtitle.node.info")

#: P1：HIDIdleTime < 60s 视为「有人在用」
IN_USE_IDLE_THRESHOLD_S = 60.0
HW_HASH_LEN = 16

#: 安装脚本（TK-003）生成 / worker 维护的文件都放在这个目录，和 UDS 同目录（S3）
STATE_DIR = Path("~/Library/Application Support/rs-node").expanduser()
NODE_ID_FILENAME = "node_id"
#: {"model","backend","rtf","translator"}：安装自检写入，之后每次会话结束滑动更新。
#: gateway 只读，不负责测 rtf（测 rtf 要跑模型，会把 numpy/mlx 拉进常驻进程）。
ASR_STATE_FILENAME = "asr_state.json"

DEFAULT_ASR = {"model": "whisper-large-v3-turbo", "backend": "mlx", "rtf": None}
DEFAULT_TRANSLATOR = "apple"

_TRANSLATOR_RE = re.compile(r"^(apple|ollama:[A-Za-z0-9_.:/-]{1,64})$")
_HID_IDLE_RE = re.compile(r'"HIDIdleTime"\s*=\s*(\d+)')
_PLATFORM_UUID_RE = re.compile(r'"IOPlatformUUID"\s*=\s*"([^"]+)"')

Runner = Callable[[list[str]], str]


def run_command(argv: list[str], timeout: float = 3.0) -> str:
    """真正执行系统命令；失败（不存在/超时/非 0）一律抛异常，由调用方降级。"""
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, check=True
    ).stdout


def parse_hid_idle_seconds(ioreg_output: str) -> float:
    """`ioreg -c IOHIDSystem` 里 HIDIdleTime 的单位是纳秒。取多个设备中最小的那个。"""
    values = [int(m) for m in _HID_IDLE_RE.findall(ioreg_output)]
    if not values:
        raise ValueError("no_hid_idle_time")
    return min(values) / 1e9


def parse_platform_uuid(ioreg_output: str) -> str:
    m = _PLATFORM_UUID_RE.search(ioreg_output)
    if not m:
        raise ValueError("no_platform_uuid")
    return m.group(1)


def parse_on_ac(pmset_output: str) -> bool:
    """`pmset -g batt` 首行形如 `Now drawing from 'AC Power'`；没有电池的台式机也是 AC。"""
    first = pmset_output.splitlines()[0] if pmset_output else ""
    if "AC Power" in first:
        return True
    if "Battery Power" in first or "UPS Power" in first:
        return False
    raise ValueError("unknown_power_source")


class NodeInfo:
    def __init__(self, *, run: Runner = run_command, state_dir: Path | None = None) -> None:
        self._run = run
        self._state_dir = Path(state_dir) if state_dir is not None else STATE_DIR
        self._hw_hash: str | None = None

    # 每一项都单独成方法：一项探测失败不影响其它字段。

    def in_use(self) -> bool:
        try:
            idle = parse_hid_idle_seconds(self._run(["ioreg", "-c", "IOHIDSystem", "-d", "4"]))
        except Exception as e:  # noqa: BLE001 - 只记类名，输出里可能有设备信息
            log.warning("probe_failed what=hid_idle err=%s", type(e).__name__)
            return True
        return idle < IN_USE_IDLE_THRESHOLD_S

    def on_ac(self) -> bool:
        try:
            return parse_on_ac(self._run(["pmset", "-g", "batt"]))
        except Exception as e:  # noqa: BLE001
            log.warning("probe_failed what=power err=%s", type(e).__name__)
            return False

    def hw_hash(self) -> str:
        # 平台 UUID 终身不变，探测一次就够
        if self._hw_hash is None:
            try:
                uuid = parse_platform_uuid(self._run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"]))
            except Exception as e:  # noqa: BLE001
                log.warning("probe_failed what=hw_hash err=%s", type(e).__name__)
                return ""
            self._hw_hash = hashlib.sha256(uuid.encode("utf-8")).hexdigest()[:HW_HASH_LEN]
        return self._hw_hash

    def node_id(self) -> str:
        try:
            return (self._state_dir / NODE_ID_FILENAME).read_text(encoding="utf-8").strip()
        except OSError as e:
            # 空串在客户端必然和清单对不上（S5），等于明确拒绝，而不是放行
            log.warning("probe_failed what=node_id err=%s", type(e).__name__)
            return ""

    def asr_and_translator(self) -> tuple[dict[str, Any], str]:
        asr = dict(DEFAULT_ASR)
        translator = DEFAULT_TRANSLATOR
        try:
            raw = json.loads((self._state_dir / ASR_STATE_FILENAME).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return asr, translator
        if not isinstance(raw, dict):
            return asr, translator
        for key in ("model", "backend"):
            if isinstance(raw.get(key), str) and raw[key]:
                asr[key] = raw[key]
        rtf = raw.get("rtf")
        if isinstance(rtf, int | float) and not isinstance(rtf, bool) and rtf >= 0:
            asr["rtf"] = float(rtf)
        tr = raw.get("translator")
        if isinstance(tr, str) and _TRANSLATOR_RE.match(tr):
            translator = tr
        return asr, translator

    def collect(self, *, busy: bool, worker: str) -> dict[str, Any]:
        """返回 P1 规定的 JSON。会阻塞（跑子进程），gateway 在线程池里调它。"""
        asr, translator = self.asr_and_translator()
        return {
            "v": 2,
            "node_id": self.node_id(),
            "hw_hash": self.hw_hash(),
            "asr": asr,
            "translator": translator,
            "busy": bool(busy),
            "on_ac": self.on_ac(),
            "in_use": self.in_use(),
            "worker": worker,
        }
