"""Windows 11 22H2+ 本机实时字幕输入；不采集或上传音频/文本。

可选依赖：``python -m pip install uiautomation``（自带 comtypes 依赖）。
仅选择此识别源时探测依赖，真正的 UIA 导入和 COM 操作都在专用线程。
识别语言由 Windows 实时字幕设置决定，本模块不修改系统语言。
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from difflib import SequenceMatcher
import importlib
import importlib.util
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from threading import Event, Thread, current_thread
import time

from realtime_subtitle.paths import repo_path
from realtime_subtitle.ui.design_tokens import TEXT_LOCAL_UNAVAILABLE_RECONNECTING_PREFIX

POLL_INTERVAL_SECONDS = 0.15
FINALIZE_STABLE_SECONDS = 0.8  # 句末仍可能被系统修订；末句等短暂稳定后才交给翻译。
FORCE_FLUSH_CHARS = 240  # 无标点长句也要有延迟上限，避免草稿无限增长。
FORCE_FLUSH_SECONDS = 12.0  # 持续增长不能重置此计时，否则不停说话永远无法翻译。
RECONNECT_INITIAL_SECONDS = 0.5
RECONNECT_MAX_SECONDS = 8.0
STATUS_LOCAL = "本机·Windows实时字幕"
_SENTENCE_END = re.compile(r'''[.!?…。！？]+["'“”‘’»」』）)\]]*''')


@dataclass(frozen=True)
class CaptionUpdate:
    finalized: list[str]
    draft: str


class CaptionDiffer:
    """把滚动/修订的完整快照变成一次性句子和草稿（now 使用单调秒）。

    记录的是当前快照中已交付的位置，不是全局文本集合：同一句说两遍仍交付两次。
    已定稿区域的后续修订不重发；稳定阈值就是系统尚未完成修订时的折中。
    UIA 暂时返回空文本时保留位置，避免字幕恢复后把旧句重放。
    """

    def __init__(self, stable_seconds=FINALIZE_STABLE_SECONDS,
                 max_chars=FORCE_FLUSH_CHARS, max_seconds=FORCE_FLUSH_SECONDS):
        if stable_seconds < 0 or max_chars < 1 or max_seconds <= 0:
            raise ValueError("字幕稳定时间/强制分段阈值无效")
        self.stable_seconds = stable_seconds
        self.max_chars = max_chars
        self.max_seconds = max_seconds
        self._text = ""
        self._consumed = 0
        self._candidate = ""
        self._candidate_since = None
        self._pending_since = None

    def _map_consumed(self, text):
        """滚屏删除的是前缀；编辑映射把已提交边界跟着移动，不能靠全文去重。"""
        old = self._text
        if not old:
            return 0, False
        if text.startswith(old):
            return self._consumed, True
        matcher = SequenceMatcher(None, old, text, autojunk=False)
        blocks = matcher.get_matching_blocks()
        # 新快照开头能在旧快照找到，是滚屏；否则只有足够相似才按末句修订处理。
        # ☠️ 仅有句号或空格重合并不表示同一段，会把下一段新字幕整段吞掉。
        anchor = next((b for b in blocks if b.b == 0 and b.size), None)
        related = (anchor is not None and anchor.size >= min(4, len(text))) or matcher.ratio() >= 0.6
        if not related:
            return 0, False
        cursor = self._consumed
        for kind, a0, a1, b0, b1 in matcher.get_opcodes():
            if kind == "insert":
                if cursor <= a0:
                    return b0, True
            elif cursor <= a1:
                if kind == "equal":
                    return b0 + max(0, cursor - a0), True
                # 被修订的已定稿尾巴不能作为一个新句子再次交付。
                return (b1 if cursor > a0 else b0), True
        return len(text), True

    def update(self, text, now=None):
        now = time.monotonic() if now is None else now
        text = " ".join((text or "").split())
        if not text:
            self._candidate = ""
            self._candidate_since = None
            self._pending_since = None
            return CaptionUpdate([], "")

        if self._text:
            # 一条无标点长句也可能跨过顶端滚屏：保存消失的未交付前缀，否则会吞词。
            blocks = SequenceMatcher(None, self._text, text, autojunk=False).get_matching_blocks()
            anchor = next((block for block in blocks if block.b == 0 and block.size >= 4), None)
            if anchor and anchor.a > self._consumed:
                text = self._text[self._consumed:anchor.a] + text
                self._text = self._text[self._consumed:]
                self._consumed = 0
        cursor, related = self._map_consumed(text)
        if not related:
            self._pending_since = None
            self._candidate = ""
            self._candidate_since = None
        self._text = text
        self._consumed = cursor
        if text[cursor:].strip() and self._pending_since is None:
            self._pending_since = now
        finalized = []
        for match in _SENTENCE_END.finditer(text, cursor):
            # 西文缩写/小数/邮件中的点不是句末；中文句号不要求后接空格。
            if match.group() == ".":
                if match.end() < len(text) and not text[match.end()].isspace():
                    continue
                token = text[:match.end()].rsplit(" ", 1)[-1]
                if re.search(r"(?:[^\W\d_]\.){2,}$", token):
                    continue
            sentence = text[cursor:match.end()].strip()
            if not sentence:
                continue
            follows = bool(text[match.end():].strip())
            if sentence != self._candidate:
                self._candidate = sentence
                self._candidate_since = now
            stable = now - self._candidate_since >= self.stable_seconds
            if not follows and not stable:
                break
            finalized.append(sentence)
            cursor = match.end()
            self._candidate = ""
            self._candidate_since = None
            self._pending_since = now

        tail = text[cursor:].strip()
        while len(tail) >= self.max_chars:
            end = self.max_chars
            # 尽量保留西文整词；中文没有空格时直接按字符切。
            space = tail.rfind(" ", 0, end + 1)
            if space >= end // 2:
                end = space
            finalized.append(tail[:end].strip())
            cursor = len(text) - len(tail) + end
            tail = text[cursor:].strip()
            self._pending_since = now
            self._candidate = ""
            self._candidate_since = None
        if (tail and self._pending_since is not None
                and now - self._pending_since >= self.max_seconds):
            finalized.append(tail)
            cursor = len(text)
            tail = ""
            self._candidate = ""
            self._candidate_since = None
        self._consumed = cursor
        if not tail:
            self._pending_since = None
        return CaptionUpdate(finalized, tail)


def _windows_supported():
    if sys.platform != "win32":
        return False
    version = sys.getwindowsversion()
    return version.major >= 10 and version.build >= 22621


def _find_executable():
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    candidates = [Path(system_root) / "System32" / "LiveCaptions.exe"]
    located = shutil.which("LiveCaptions.exe")
    if located:
        candidates.append(Path(located))
    return next((str(path) for path in candidates
                 if path.is_file() and os.access(path, os.X_OK)), None)


def _check_uia_available():
    try:
        if any(importlib.util.find_spec(name) is None for name in ("uiautomation", "comtypes")):
            return "缺少可选依赖，请运行 python -m pip install uiautomation"
    except (ImportError, ValueError, AttributeError):
        return "UIA 可选依赖损坏，请运行 python -m pip install --upgrade uiautomation"
    # import comtypes 本身也会初始化 COM；预检不能污染 Qt/主线程的 COM apartment。
    errors = []
    ready = Event()
    def probe():
        try:
            auto = importlib.import_module("uiautomation")
            with auto.UIAutomationInitializerInThread():
                pass
        except Exception:
            errors.append(True)
        finally:
            ready.set()
    Thread(target=probe, name="live-captions-preflight", daemon=True).start()
    if not ready.wait(5) or errors:
        return "UIA 可选依赖无法加载，请运行 python -m pip install --upgrade uiautomation"
    return None


def prepare_live_captions_reader(source, hide_window=True):
    """选择失败时明确提示并返回 None，让调用方走原有 Whisper 启动路径。"""
    if source != "livecaptions":
        return None
    reason = None
    if not _windows_supported():
        reason = "仅支持 Windows 11 22H2 或更新版本"
    else:
        reason = _check_uia_available()
    executable = None if reason else _find_executable()
    if not reason and not executable:
        reason = "未找到 LiveCaptions.exe，请确认 Windows 已安装实时字幕功能"
    if reason:
        print(f"⚠️ Windows 实时字幕不可用：{reason}；回退 Whisper。")
        return None
    return LiveCaptionsReader(executable, hide_window=hide_window)


def _running_caption_pids():
    # tasklist 是 Windows 自带工具；不经 shell，不解析本地化的提示行。
    result = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq LiveCaptions.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, timeout=3, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return {int(row[1]) for row in csv.reader(io.StringIO(result.stdout))
            if len(row) >= 2 and row[0].casefold() == "livecaptions.exe" and row[1].isdigit()}


class LiveCaptionsReader:
    """专用线程拥有全部 COM 控件；回调不能直接操作 Qt 控件，须走现有信号路径。"""

    def __init__(self, executable, hide_window=True):
        self.executable = executable
        self.hide_window = hide_window
        self._stop = Event()
        self._thread = None
        self._hidden_windows = {}
        self._differ = CaptionDiffer()

    def start(self, on_update, on_status=None):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._differ = CaptionDiffer()
        self._thread = Thread(target=self._run, args=(on_update, on_status),
                              name="live-captions", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        thread = self._thread
        # ☠️ 不能从调用方线程操作 UIA；恢复窗口放在 reader 的 finally、COM 释放之前。
        if thread and thread is not current_thread():
            thread.join(timeout=5)

    def _find_controls(self, auto):
        pids = _running_caption_pids()
        if not pids:
            process = subprocess.Popen([self.executable], shell=False)
            pids.add(process.pid)
        for window in auto.GetRootControl().GetChildren():
            if window.ProcessId not in pids:
                continue
            element = window.Control(AutomationId="CaptionsTextBlock", searchDepth=12)
            if element.Exists(0, 0):
                return window, element
        raise LookupError("Windows 实时字幕窗口或 CaptionsTextBlock 暂不可用")

    @staticmethod
    def _user32():
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        # ☠️ ctypes 默认按 int 传参；64 位 HWND 必须显式声明，不能被截断。
        for name in ("IsWindowVisible", "IsIconic", "IsZoomed", "IsWindow"):
            function = getattr(user32, name)
            function.argtypes = [wintypes.HWND]
            function.restype = wintypes.BOOL
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        return user32

    def _hide_window(self, window):
        if not self.hide_window:
            return
        user32 = self._user32()
        handle = window.NativeWindowHandle
        if not handle or handle in self._hidden_windows or not user32.IsWindowVisible(handle):
            return
        # 记住用户原来的最大化/最小化状态，停止时恢复，绝不终止系统字幕进程。
        state = 2 if user32.IsIconic(handle) else 3 if user32.IsZoomed(handle) else 8
        self._hidden_windows[handle] = state
        user32.ShowWindow(handle, 0)

    def _restore_windows(self):
        if not self._hidden_windows:
            return
        user32 = self._user32()
        for handle, state in self._hidden_windows.items():
            if user32.IsWindow(handle):
                user32.ShowWindow(handle, state)
        self._hidden_windows.clear()

    def _wait_retry(self, delay, on_update):
        until = time.monotonic() + delay
        while not self._stop.is_set() and not os.path.exists(repo_path(".stop")):
            # 继续心跳，让串行 ASR 执行器可以消费语言切换等控制消息。
            on_update([], "")
            remaining = until - time.monotonic()
            if remaining <= 0:
                return
            self._stop.wait(min(POLL_INTERVAL_SECONDS, remaining))

    def _run(self, on_update, on_status):
        delay = RECONNECT_INITIAL_SECONDS
        while not self._stop.is_set() and not os.path.exists(repo_path(".stop")):
            try:
                # UIA 官方要求 COM 初始化、控件创建、使用和释放在同一线程。
                auto = importlib.import_module("uiautomation")
                with auto.UIAutomationInitializerInThread():
                    try:
                        window, element = self._find_controls(auto)
                        try:
                            self._hide_window(window)
                        except Exception:
                            # 某些系统布局不允许隐藏；这不能阻止读取字幕。
                            print("⚠️ 无法隐藏 Windows 实时字幕窗口，将保持可见。")
                        if on_status:
                            on_status(STATUS_LOCAL)
                        while not self._stop.is_set() and not os.path.exists(repo_path(".stop")):
                            if not element.Exists(0, 0):
                                raise LookupError("Windows 实时字幕元素已消失")
                            # UIA 的 Name 属性不是 Qt 枚举，避免仓库的 Qt 短名扫描误报。
                            result = self._differ.update(getattr(element, "Name"))
                            if os.path.exists(repo_path(".paused")):
                                on_update([], "")
                            else:
                                on_update(result.finalized, result.draft)
                            delay = RECONNECT_INITIAL_SECONDS
                            self._stop.wait(POLL_INTERVAL_SECONDS)
                    finally:
                        # 异常路径也恢复，隐藏的窗口否则可能无法在重连时被 UIA 找到。
                        self._restore_windows()
                        window = element = None
            except Exception:
                if self._stop.is_set():
                    break
                if on_status:
                    on_status(f"{TEXT_LOCAL_UNAVAILABLE_RECONNECTING_PREFIX}Windows实时字幕（{delay:g}秒）")
                self._wait_retry(delay, on_update)
                delay = min(delay * 2, RECONNECT_MAX_SECONDS)
