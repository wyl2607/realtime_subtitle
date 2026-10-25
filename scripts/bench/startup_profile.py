#!/usr/bin/env python3
"""Mac 桌面字幕冷启动 / 常驻体检（只读测量，不改生产行为）。

量四件事：
  1. `python -X importtime` 的 import 大块（PyQt6 / numpy / requests /
     sounddevice / mlx / faster_whisper…），用来判断"窗口出现前"卡在哪；
  2. 启动各阶段（子进程 stdout 时间戳 + 一个只碰 UI 类的探针）：
     import 完 → 窗口创建 → 事件循环 → Whisper 开始加载 → 后端选定 →
     所有组件完成 → "✅已就绪"；
  3. 就绪后的稳态内存：进程 RSS、峰值 RSS、MLX active/peak，以及哪些重量级
     模块真的被 import 了（Apple 后端下不该有 torch/ctranslate2）；
  4. 空闲 N 秒的 CPU 时间 + flush/ASR 调用次数（判定"没声音时是否还在每
     0.5 秒空转"）。

用法（在仓库根目录）：
  python scripts/bench/startup_profile.py                 # import 表 + 启动阶段
  python scripts/bench/startup_profile.py --idle-seconds 30
  python scripts/bench/startup_profile.py --no-imports    # 只跑启动
  python scripts/bench/startup_profile.py --no-audio      # 不碰音频设备（空闲专用）
  python scripts/bench/startup_profile.py --mlx-load      # 另起进程量 MLX 加载
  python scripts/bench/startup_profile.py --json /tmp/p.json

复测（权威数字应在串行、离线、接电源时跑）：
  HF_HUB_OFFLINE=1 caffeinate -i python scripts/bench/startup_profile.py \\
      --idle-seconds 30 --json /tmp/startup.json

☠️ 测量期间不要同时跑其它识别/翻译（另一份字幕、rslite、benchmark）：共享
GPU 和内存，Windows/Linux 上的经验（CLAUDE.md 第 4 节第 11 条）在这里同样成立。
子进程用临时的 HOME，不会读写 ~/.config/realtime_subtitle；运行时文件
（window_state.json / lookup_cache.json / transcripts/）会落在仓库根，
脚本跑完会把这次新建的删掉。

☠️ 探针只 patch 已经随 app 一起导入的 UI 类（SubtitleWindow / 容器 showEvent），
**不在构造前预导入 translator_queue / audio_capture / mlx_backend**——那几个
模块的导入位置正是被测对象（惰性导入优化），提前导入会把收益抵消掉。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# 启动阶段要单独点名的 import。mlx / faster_whisper 在 import 阶段通常不在，
# 它们由 _load_models 里的后端路径惰性导入——这本身就是一条结论。
IMPORT_OF_INTEREST = (
    "PyQt6.QtWidgets",
    "PyQt6.QtCore",
    "PyQt6.QtGui",
    "numpy",
    "requests",
    "sounddevice",
    "mlx",
    "mlx_whisper",
    "faster_whisper",
    "faster_whisper.vad",
    "ctranslate2",
    "torch",
    "opencc",
    "huggingface_hub",
    "realtime_subtitle.app",
    "realtime_subtitle.translate.translator_queue",
    "realtime_subtitle.capture.audio_capture",
)

# 稳态体检关注的模块：谁在 sys.modules 里 = 谁已经付过 import + 常驻成本。
HEAVY_MODULES = (
    "torch",
    "ctranslate2",
    "faster_whisper",
    "faster_whisper.vad",
    "transformers",
    "mlx",
    "mlx_whisper",
    "numpy",
    "requests",
    "sounddevice",
    "opencc",
    "huggingface_hub",
    "PyQt6.QtWidgets",
)

# 子进程 stdout 里现成的启动里程碑（不改生产代码，只读日志）。
LOG_MILESTONES = (
    ("🔧 正在初始化组件", "init_components"),
    ("✅ 字幕窗口已创建", "window_created"),
    ("🎬 字幕窗口事件循环启动", "loop_started"),
    ("🔄 正在加载 Whisper 模型", "whisper_load_start"),
    ("🍎 句子翻译使用 Apple Translation", "backend_apple"),
    ("🦙 句子翻译使用 Ollama", "backend_ollama"),
    ("✅ 所有组件初始化完成", "components_done"),
    ("✅ 应用已成功启动", "app_started"),
)

# 本进程可能新建的运行时文件；跑前快照、跑后只删这次多出来的。
_RUNTIME_PATHS = (
    "window_state.json",
    "window_state.json.tmp",
    "window_state.json.bak",
    "lookup_cache.json",
    "lookup_cache.json.tmp",
    "transcripts",
    "subtitle.pid",
    "realtime_subtitle.lock",
    ".stop",
    ".paused",
)

_IMPORTTIME_RE = re.compile(r"^import time:\s+(\d+) \|\s+(\d+) \|\s+(\S.*)$")
_FOOTPRINT_RE = re.compile(r"phys_footprint(?:_peak)?:\s+([0-9.]+)\s*([KMGTP]?B)")


def parse_importtime(text: str) -> dict[str, dict[str, int]]:
    """把 `python -X importtime` 的 stderr 解析成 {模块: {self_us, cumulative_us}}。

    同名模块可能因为嵌套 import 出现多次；取最后一次（完整累计值）。
    """
    found: dict[str, dict[str, int]] = {}
    for line in text.splitlines():
        match = _IMPORTTIME_RE.match(line)
        if not match:
            continue
        self_us, cumulative_us, name = match.groups()
        found[name.strip()] = {
            "self_us": int(self_us),
            "cumulative_us": int(cumulative_us),
        }
    return found


def summarize_importtime(modules: dict[str, dict[str, int]], want=IMPORT_OF_INTEREST):
    rows = []
    for name in want:
        item = modules.get(name)
        rows.append({
            "module": name,
            "self_ms": round(item["self_us"] / 1000.0, 1) if item else None,
            "cumulative_ms": round(item["cumulative_us"] / 1000.0, 1) if item else None,
        })
    return rows


def _env_for_child(home: str) -> dict[str, str]:
    env = os.environ.copy()
    # 子进程只看到临时 HOME：云配置目录（~/.config/realtime_subtitle）不会被读，
    # 更不会被写。HF 缓存是只读输入，显式指回真实位置（local_files_only 用它）。
    real_home = Path(env.get("HOME") or Path.home())
    env["HOME"] = home
    env.setdefault("HF_HOME", str(real_home / ".cache" / "huggingface"))
    env.setdefault("HF_HUB_OFFLINE", "1")
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONUNBUFFERED"] = "1"
    env["REALTIME_SUBTITLE_NO_SINGLETON"] = "1"
    env["PYTHONPATH"] = (
        str(REPO_ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    )
    return env


def _runtime_snapshot() -> dict[str, bool]:
    return {name: (REPO_ROOT / name).exists() for name in _RUNTIME_PATHS}


def _cleanup_runtime_files(before: dict[str, bool]) -> list[str]:
    removed = []
    for name, existed in before.items():
        if existed:
            continue
        path = REPO_ROOT / name
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            removed.append(name + "/")
        elif path.exists():
            try:
                path.unlink()
                removed.append(name)
            except OSError:
                pass
    return removed


def run_import_profile(home: str, timeout: float) -> list[dict]:
    proc = subprocess.run(
        [sys.executable, "-X", "importtime", "-c", "import realtime_subtitle.app"],
        cwd=str(REPO_ROOT),
        env=_env_for_child(home),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise SystemExit(f"import 测量失败（exit {proc.returncode}）:\n{proc.stderr[-2000:]}")
    return summarize_importtime(parse_importtime(proc.stderr))


def run_mlx_probe(home: str, timeout: float) -> dict:
    """独立进程：本地 snapshot 解析 + MLX 权重加载各花多久。

    走的是生产同一个 load_mlx_model；local_files_only=True 时不发 etag 请求。
    """
    code = r"""
import json, time
import realtime_subtitle.config as config
from huggingface_hub import snapshot_download

t = time.perf_counter()
snapshot_download(config.WHISPER_MLX_REPO, local_files_only=True)
snap = time.perf_counter() - t

t = time.perf_counter()
from realtime_subtitle.asr.mlx_backend import load_mlx_model
load_mlx_model(config.WHISPER_MLX_REPO)
load = time.perf_counter() - t

import mlx.core as mx
print(json.dumps({
    "snapshot_local_seconds": round(snap, 3),
    "load_seconds": round(load, 3),
    "mlx_active_mb": round(mx.metal.get_active_memory() / 1e6, 1),
    "mlx_peak_mb": round(mx.metal.get_peak_memory() / 1e6, 1),
    "mlx_cache_mb": round(mx.metal.get_cache_memory() / 1e6, 1),
}))
"""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        env=_env_for_child(home),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise SystemExit(f"MLX 探针失败（exit {proc.returncode}）:\n{proc.stderr[-2000:]}")
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
    return json.loads(line[-1]) if line else {}


def child_main(args: argparse.Namespace) -> int:
    """子进程：起一次真实的 SubtitleApp（offscreen），只做 UI 级探针。"""

    t0 = time.perf_counter()
    marks: list[dict] = []
    counters = {"flush_pending": 0, "process_inbox": 0, "process_items": 0}

    def mark(name: str, detail: str = "") -> None:
        elapsed = time.perf_counter() - t0
        marks.append({"name": name, "t": round(elapsed, 3), "detail": detail})
        print(f"PROFILE\t{elapsed:.3f}\t{name}\t{detail}", flush=True)

    os.environ.setdefault("REALTIME_SUBTITLE_NO_SINGLETON", "1")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    # --no-audio 只用于空闲体检：用一个桩模块顶掉采集，避免麦克风/BlackHole
    # 依赖和"录到别人说话"。它也会跳过 sounddevice 的 import，所以这一模式
    # 的启动阶段数字不能当基线。
    if args.no_audio:
        import types

        stub = types.ModuleType("realtime_subtitle.capture.audio_capture")

        class _NullCapture:
            def __init__(self, **kw):
                del kw

            def start(self):
                return None

            def stop(self):
                return None

        stub.AudioCapture = _NullCapture
        stub.PAUSE_FLAG_FILE = str(REPO_ROOT / ".paused")
        stub.STOP_FLAG_FILE = str(REPO_ROOT / ".stop")
        sys.modules[stub.__name__] = stub

    import realtime_subtitle.app as appmod
    mark("import_app")

    from PyQt6.QtCore import QTimer

    # 这两个类随 app 一起导入，patch 它们不会替被测代码提前 import 任何东西。
    from realtime_subtitle.ui.subtitle_window import SubtitleWindow
    from realtime_subtitle.ui.window_frame import ResizableFramelessWidget

    state = {
        "ready_at": None,
        "idle_started": False,
        "exiting": False,
    }
    window_construct_started = [None]

    original_window_init = SubtitleWindow.__init__

    def window_init(self, *a, **kw):
        window_construct_started[0] = time.perf_counter()
        mark("window_construct_start")
        try:
            return original_window_init(self, *a, **kw)
        finally:
            mark("window_construct_end",
                 f"{time.perf_counter() - window_construct_started[0]:.3f}s")

    SubtitleWindow.__init__ = window_init

    original_show_event = ResizableFramelessWidget.showEvent

    def show_event(self, event):
        if not getattr(self, "_profile_shown", False):
            self._profile_shown = True
            mark("window_show_event")
        return original_show_event(self, event)

    ResizableFramelessWidget.showEvent = show_event

    original_status = SubtitleWindow.show_status

    def show_status(self, msg, *a, **kw):
        text = str(msg)
        if "已就绪" in text and state["ready_at"] is None:
            state["ready_at"] = time.perf_counter() - t0
            mark("ready", text.replace("\t", " "))
        return original_status(self, msg, *a, **kw)

    SubtitleWindow.show_status = show_status

    def _rss_mb():
        try:
            out = subprocess.check_output(
                ["ps", "-o", "rss=", "-p", str(os.getpid())], text=True
            ).strip()
            return round(int(out) / 1024.0, 1)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None

    def _footprint_mb():
        """Activity Monitor 口径的 phys_footprint（含被压缩页），比 ps rss 稳。

        ☠️ macOS 会在内存压力下把空闲进程的页压缩/换出，`ps -o rss` 会从
        1GB 掉到 200MB，看着像"内存优化了"，其实没有。
        """
        try:
            out = subprocess.check_output(
                ["footprint", "-p", str(os.getpid())],
                text=True, stderr=subprocess.DEVNULL, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        scale = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3,
                 "TB": 1024 ** 4, "PB": 1024 ** 5}
        values = {}
        for match in _FOOTPRINT_RE.finditer(out):
            key = "peak" if match.group(0).startswith("phys_footprint_peak") else "current"
            values[key] = round(float(match.group(1)) * scale[match.group(2)]
                                / 1024 ** 2, 1)
        return values or None

    def _mlx_memory():
        mx = sys.modules.get("mlx.core")
        if mx is None:
            return {}
        try:
            metal = mx.metal
            return {
                "active_mb": round(metal.get_active_memory() / 1e6, 1),
                "peak_mb": round(metal.get_peak_memory() / 1e6, 1),
                "cache_mb": round(metal.get_cache_memory() / 1e6, 1),
            }
        except Exception:
            return {}

    def _loaded_modules():
        return {name: (name in sys.modules) for name in HEAVY_MODULES}

    # 就绪后才 import/ patch 翻译器：它此时已经由 _load_models 导入，零额外成本，
    # 也不会把 import 挪到启动路径前面。
    def _instrument_idle():
        try:
            from realtime_subtitle.translate.translator_queue import WhisperQueueTranslator
        except Exception:
            return
        for attr, key in (("flush_pending", "flush_pending"),
                          ("_process_inbox", "process_inbox"),
                          ("_process_items", "process_items")):
            original = getattr(WhisperQueueTranslator, attr)
            if getattr(original, "_profile_counted", False):
                continue

            def wrapper(self, *a, _orig=original, _key=key, **kw):
                counters[_key] += 1
                return _orig(self, *a, **kw)

            wrapper._profile_counted = True
            setattr(WhisperQueueTranslator, attr, wrapper)

    app_ref: list = []

    def _finish_idle(started, cpu_started, rss_started, footprint_started,
                     counters_started):
        if state["exiting"]:
            return
        state["exiting"] = True
        wall = time.perf_counter() - started
        cpu = time.process_time() - cpu_started
        deltas = {k: counters[k] - counters_started.get(k, 0) for k in counters}
        idle = {
            "seconds": round(wall, 2),
            "cpu_seconds": round(cpu, 3),
            "cpu_percent": round(100.0 * cpu / wall, 2) if wall else None,
            "rss_start_mb": rss_started,
            "rss_end_mb": _rss_mb(),
            "footprint_start_mb": footprint_started,
            "footprint_end_mb": _footprint_mb(),
            "mlx_end": _mlx_memory(),
            "calls": deltas,
        }
        print("IDLE " + json.dumps(idle, ensure_ascii=False), flush=True)
        mark("idle_done", json.dumps(deltas, ensure_ascii=False))
        try:
            app_ref[0].quit()
        except Exception:
            pass

    def _poll_ready():
        if state["exiting"] or state["idle_started"]:
            return
        if state["ready_at"] is None:
            QTimer.singleShot(50, _poll_ready)
            return
        state["idle_started"] = True
        _instrument_idle()
        translator = getattr(app_ref[1], "translator", None)
        ready_stats = {
            "rss_mb": _rss_mb(),
            "peak_rss_mb": round(_peak_rss_mb(), 1),
            "footprint_mb": _footprint_mb(),
            "mlx": _mlx_memory(),
            "modules": _loaded_modules(),
            "backend": getattr(translator, "_translate_backend", None),
        }
        print("READY " + json.dumps(ready_stats, ensure_ascii=False), flush=True)
        marks.append({"name": "ready_stats", "t": None, "detail": ready_stats})
        if args.idle_seconds <= 0:
            state["exiting"] = True
            app_ref[0].quit()
            return
        started = time.perf_counter()
        cpu_started = time.process_time()
        rss_started = ready_stats["rss_mb"]
        footprint_started = ready_stats["footprint_mb"]
        counters_started = dict(counters)
        QTimer.singleShot(
            int(args.idle_seconds * 1000),
            lambda: _finish_idle(started, cpu_started, rss_started,
                                 footprint_started, counters_started),
        )

    app = appmod.SubtitleApp()
    app_ref.extend([app.subtitle_window.app, app])
    mark("app_constructed")

    QTimer.singleShot(0, lambda: (mark("loop_started"), _poll_ready()))

    try:
        app.start()
    except SystemExit:
        pass
    except Exception as exc:
        mark("start_failed", f"{type(exc).__name__}: {exc}")
        return 1

    print("MARKS " + json.dumps(marks, ensure_ascii=False), flush=True)
    return 0


def _peak_rss_mb() -> float:
    """macOS 上 ru_maxrss 单位是字节（Linux 上是 KB）。"""
    import resource

    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6


def run_stage_profile(args: argparse.Namespace, home: str) -> dict:
    cmd = [sys.executable, str(Path(__file__).resolve()), "--child",
           "--idle-seconds", str(args.idle_seconds)]
    if args.no_audio:
        cmd.append("--no-audio")
    started_at = time.perf_counter()
    stderr_path = Path(home) / "startup_profile.stderr"
    stderr_file = stderr_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        cwd=str(REPO_ROOT),
        env=_env_for_child(home),
        stdout=subprocess.PIPE,
        stderr=stderr_file,
        text=True,
        bufsize=1,
    )
    # 子进程若在第一条输出前就卡死，for line 会永远阻塞——用看门狗兜底。
    import threading

    watchdog = threading.Timer(max(1.0, args.timeout), proc.kill)
    watchdog.daemon = True
    watchdog.start()
    line_times: list[dict] = []
    result = {"exit": None, "marks": [], "ready": None, "idle": None,
              "log_times": line_times}
    try:
        for line in proc.stdout:
            now = time.perf_counter() - started_at
            line = line.rstrip("\n")
            if line.startswith("PROFILE\t"):
                parts = (line.split("\t") + ["", "", ""])[:4]
                result["marks"].append(
                    {"t": float(parts[1]), "name": parts[2], "detail": parts[3]})
            elif line.startswith("READY "):
                result["ready"] = json.loads(line[len("READY "):])
            elif line.startswith("IDLE "):
                result["idle"] = json.loads(line[len("IDLE "):])
            elif line.startswith("MARKS "):
                result["marks"] = json.loads(line[len("MARKS "):])
            else:
                line_times.append({"t": round(now, 3), "line": line})
    finally:
        watchdog.cancel()
        try:
            result["exit"] = proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            result["exit"] = proc.wait()
        stderr_file.close()
        try:
            result["stderr_tail"] = stderr_path.read_text(
                encoding="utf-8", errors="replace").splitlines()[-12:]
        except OSError:
            result["stderr_tail"] = []
    if not result["marks"] and line_times:
        result["stdout_tail"] = [item["line"] for item in line_times[-12:]]
    else:
        result["stdout_tail"] = []
    result["stage_table"] = _log_stages(result)
    return result


def _log_stages(result: dict) -> list[dict]:
    """从 stdout 行时间戳里抽里程碑，并按 PROFILE 的 ready 对齐到同一时钟。"""
    stages = []
    for item in result.get("log_times") or []:
        for needle, name in LOG_MILESTONES:
            if needle in item["line"]:
                stages.append({"t_parent": item["t"], "name": name,
                               "detail": item["line"].strip()})
                break
    # 用第一条非空日志（_print_header 的横幅）对齐：它紧跟在 import_app 之后。
    marks = {m["name"]: m["t"] for m in result.get("marks") or [] if m["t"] is not None}
    first_line = next(
        (item["t"] for item in result.get("log_times") or [] if item["line"].strip()),
        None,
    )
    offset = None
    if first_line is not None and "import_app" in marks:
        offset = first_line - marks["import_app"]
    if offset is not None:
        for name in ("ready", "window_show_event"):
            if name in marks:
                stages.append({"t_parent": round(marks[name] + offset, 3),
                               "name": name, "detail": "(PROFILE)"})
        stages.sort(key=lambda item: item["t_parent"])
    return stages


def _print_import_table(rows: list[dict]) -> None:
    print("\n=== Python import（cumulative = 该模块及其依赖的总耗时）===")
    print(f"{'module':46} {'self ms':>9} {'cumulative ms':>14}")
    for row in rows:
        cum = row["cumulative_ms"]
        if cum is None:
            print(f"{row['module']:46} {'-':>9} {'(not imported)':>14}")
        else:
            print(f"{row['module']:46} {row['self_ms']:>9.1f} {cum:>14.1f}")


def _print_stages(result: dict, no_audio: bool) -> None:
    title = "=== 启动阶段（从 Popen 起算；stdout 行时间戳 + UI 探针）==="
    if no_audio:
        title += "\n    ⚠️ --no-audio：采集模块被桩替换，阶段数字只作空闲参考"
    print("\n" + title)
    print(f"{'t (s)':>8}  {'stage':24}  detail")
    stages = _log_stages(result)
    prev = None
    for item in stages:
        t = item["t_parent"]
        delta = "" if prev is None else f"  (+{t - prev:.3f}s)"
        print(f"{t:>8.3f}  {item['name']:24}  {item['detail'][:60]}{delta}")
        prev = t

    ready = result.get("ready")
    if ready:
        print("\n=== 就绪时内存 / 已加载模块 ===")
        print(f"RSS {ready['rss_mb']} MB · 峰值 {ready['peak_rss_mb']} MB · "
              f"翻译后端 {ready.get('backend')}")
        print("footprint " + json.dumps(ready.get("footprint_mb"), ensure_ascii=False))
        if ready.get("mlx"):
            print("MLX " + json.dumps(ready["mlx"], ensure_ascii=False))
        loaded = [name for name, yes in (ready.get("modules") or {}).items() if yes]
        absent = [name for name, yes in (ready.get("modules") or {}).items() if not yes]
        print("已加载: " + (", ".join(loaded) or "(none)"))
        print("未加载: " + (", ".join(absent) or "(none)"))

    idle = result.get("idle")
    if idle:
        print(f"\n=== 空闲 {idle['seconds']:.0f} 秒 ===")
        print(f"CPU {idle['cpu_seconds']}s / {idle['seconds']}s = {idle['cpu_percent']}%"
              f"（单核占比）· RSS {idle['rss_start_mb']}→{idle['rss_end_mb']} MB")
        print("footprint " + json.dumps(
            {"start_mb": idle.get("footprint_start_mb"),
             "end_mb": idle.get("footprint_end_mb")}, ensure_ascii=False))
        print("调用次数 " + json.dumps(idle["calls"], ensure_ascii=False))
        if idle.get("mlx_end"):
            print("MLX " + json.dumps(idle["mlx_end"], ensure_ascii=False))

    if result["exit"] not in (0, None):
        print(f"\n⚠️  子进程退出码 {result['exit']}")
        if result.get("stderr_tail"):
            print("stderr 尾部: " + " | ".join(result["stderr_tail"]))
        if result.get("stdout_tail"):
            print("stdout 尾部: " + " | ".join(result["stdout_tail"]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mac 桌面字幕启动/常驻体检")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--idle-seconds", type=float, default=0.0,
                        help="就绪后静置采样多少秒（0 = 不采样）")
    parser.add_argument("--no-audio", action="store_true",
                        help="用桩替掉音频采集（隔离设备/麦克风；阶段数字仅供参考）")
    parser.add_argument("--no-imports", action="store_true", help="跳过 import 表")
    parser.add_argument("--mlx-load", action="store_true",
                        help="另起进程量本地 snapshot 解析 + MLX 权重加载")
    parser.add_argument("--json", dest="json_out", default=None,
                        help="把原始结果写到这个文件")
    parser.add_argument("--timeout", type=float, default=240.0,
                        help="子进程超时（秒）")
    args = parser.parse_args(argv)

    if args.child:
        return child_main(args)

    before = _runtime_snapshot()
    home = tempfile.mkdtemp(prefix="rs-startup-profile-")
    result: dict = {"repo": str(REPO_ROOT), "host": os.uname().nodename}
    try:
        if not args.no_imports:
            result["imports"] = run_import_profile(home, args.timeout)
            _print_import_table(result["imports"])
        result["stages"] = run_stage_profile(args, home)
        _print_stages(result["stages"], args.no_audio)
        if args.mlx_load:
            result["mlx"] = run_mlx_probe(home, args.timeout)
            print("\n=== MLX 加载（独立进程，HF_HUB_OFFLINE=1）===")
            print(json.dumps(result["mlx"], ensure_ascii=False, indent=2))
    finally:
        shutil.rmtree(home, ignore_errors=True)
        removed = _cleanup_runtime_files(before)
        result["removed_runtime_files"] = removed
        if removed:
            print("\n已清理本次运行产生的运行时文件: " + ", ".join(removed))

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"原始结果: {args.json_out}")
    return 0 if result.get("stages", {}).get("exit", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
