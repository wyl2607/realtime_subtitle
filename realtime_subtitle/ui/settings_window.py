"""
参数调节面板（⚙️）：tuning 持久化辅助函数 + 场景预设常量 + SettingsWindow。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

from PyQt6.QtWidgets import (
    QLabel, QWidget, QVBoxLayout, QHBoxLayout, QSlider,
    QPushButton, QGroupBox, QLineEdit, QCheckBox, QColorDialog, QFontComboBox,
    QScrollArea, QFrame, QApplication, QSpinBox, QButtonGroup,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont
import realtime_subtitle.config as config
from realtime_subtitle import language_policy
from realtime_subtitle.ui.window_frame import DraggableWidget
from realtime_subtitle.ui.window_geometry import (
    screen_scale_factor, settings_initial_geometry,
)


# 设置面板自身字号（100% 缩放起点）。字幕正文仍由 config.FONT_SIZE /「字幕字号」控制。
# 只乘 screen_scale_factor，不开 AA_EnableHighDpiScaling（见 CLAUDE.md 第 24 条）。
PANEL_FONT_PX_AT_100 = 17

STYLE_TEXT_COLORS = (
    ("白", "#F0F0F0"),
    ("暖黄", "#FFE894"),
    ("浅蓝", "#9FD3FF"),
    ("浅绿", "#B6F2A8"),
    ("浅灰", "#C8C8C8"),
)
BACKGROUND_COLORS = (
    ("黑", "#000000"),
    ("深灰", "#1E1E1E"),
    ("深蓝", "#0B1A2E"),
)
TEXT_EDGE_OPTIONS = (("none", "无"), ("shadow", "阴影"))
WINDOW_RADIUS_OPTIONS = (0, 8, 16)
DISPLAY_MODE_OPTIONS = (
    ("bilingual", "双语"),
    ("target", "仅译文"),
    ("source", "仅原文"),
)


def panel_font_px(scale=None):
    """面板控件像素字号。scale 缺省读主屏 DPI。"""
    if scale is None:
        scale = screen_scale_factor()
    return max(16, int(round(PANEL_FONT_PX_AT_100 * float(scale))))


def configured_language_pairs():
    """面板用的语言对列表。

    ☠️ 解析规则只有一份，在 language_policy 里（它不 import Qt，所以 UI 用它
    也不用把翻译模块拖进导入链）。以前这里自己读 LANGUAGE_PAIRS，漏了老配置
    的 LANGUAGE_CYCLE：后台有两个语言对、面板却一个按钮都没有。
    """
    return language_policy.language_pairs()


def pair_target(source):
    return language_policy.target_for(source)


def pair_label(source, target=None):
    return language_policy.pair_label(source, target)


def format_slider_value(value, step):
    """按步长显示：整数滑块不带 .000，小数位数跟步长走。"""
    step = float(step)
    value = float(value)
    if step >= 1:
        return str(int(round(value)))
    text = f"{step:.10f}".rstrip("0")
    decimals = len(text.split(".", 1)[1]) if "." in text else 0
    return f"{value:.{decimals}f}"


def config_dir():
    """云端/节点开关的目录。

    包一层是为了让面板测试替换本模块的 config_dir，而不必去改
    cloud_client.groq_key_path() 里面那个绑定好的函数。路径规则仍以
    cloud_client 为准：Windows 用 %APPDATA%，其它系统用 ~/.config。
    延迟导入：两个开关都关时，import 本模块不应把远端客户端带进来。
    """
    from realtime_subtitle.remote.cloud_client import config_dir as cloud_config_dir
    return cloud_config_dir()


def groq_key_file():
    return Path(config_dir()) / "groq.key"


def _pref_path(name):
    return Path(config_dir()) / name


def _atomic_private(path, data):
    """原子写，并把权限收成只有当前用户能读。

    key 不能先进一个谁都能读的临时文件再改权限：先按 0600 建临时文件，
    写完再 replace。Windows 上 os.open 的 mode 不起作用，所以在 replace
    之前对临时文件跑 icacls。NTFS 同卷 rename 会保留 DACL；若先替换再改
    权限，中间窗口和 icacls 失败都会把目标文件留成继承来的宽松 ACL。
    icacls 失败就删掉临时文件并抛 OSError，目标文件不会出现。
    USERNAME 只在这一支用来当 ACL 主体，不是密钥来源。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        if sys.platform == "win32":
            _icacls_user_only(tmp)
        else:
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    if sys.platform != "win32":
        os.chmod(path, 0o600)


def _icacls_user_only(path):
    """只给当前用户完全控制。命令按参数列表传，不经 shell，避免路径被拼接。"""
    user = os.environ.get("USERNAME")
    if not user:
        raise OSError("icacls failed")
    try:
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            check=True,
            shell=False,
        )
    except (OSError, subprocess.CalledProcessError):
        raise OSError("icacls failed") from None


def write_groq_key(text):
    """写入 groq.key。空串直接拒绝，避免一次空保存把已有 key 抹掉。"""
    key = (text or "").strip()
    if not key:
        raise ValueError("empty groq key")
    _atomic_private(groq_key_file(), (key + "\n").encode("utf-8"))


def clear_groq_key():
    try:
        groq_key_file().unlink()
    except FileNotFoundError:
        return


def groq_key_configured():
    """只回答「有没有 key」。读失败按没有处理，异常文本可能带路径，不往外抛。"""
    try:
        from realtime_subtitle.remote.cloud_client import read_groq_key
        return bool(read_groq_key(groq_key_file()))
    except Exception:
        return False


def _read_bool_pref(name, default):
    try:
        path = _pref_path(name)
        if not path.is_file():
            return default
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    if isinstance(data, dict) and "enabled" in data:
        return bool(data["enabled"])
    return default


def _write_bool_pref(name, enabled):
    payload = (json.dumps({"enabled": bool(enabled)}) + "\n").encode("utf-8")
    _atomic_private(_pref_path(name), payload)


def read_cloud_enabled():
    """没有 cloud.json 时：已经有 key 就默认开，否则关。老用户两边都没有，等于关。"""
    return _read_bool_pref("cloud.json", groq_key_configured())


def write_cloud_enabled(enabled):
    _write_bool_pref("cloud.json", enabled)


def read_node_enabled():
    """没有 node.json 就是关。缺文件不能把用户以后下发的偏好提前写成开。"""
    return _read_bool_pref("node.json", False)


def write_node_enabled(enabled):
    _write_bool_pref("node.json", enabled)


def nodes_file():
    return Path(config_dir()) / "nodes.json"


def load_node_entries():
    """只收有 id 和 url 的条目。文件缺失、不是数组、读坏了都当成没有节点。"""
    path = nodes_file()
    try:
        if not path.is_file():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    usable = []
    for entry in data:
        if isinstance(entry, dict) and entry.get("id") and entry.get("url"):
            usable.append(entry)
    return usable


def nodes_available():
    return bool(load_node_entries())


def cloud_active():
    """开关文件写着开、但 key 已经没了，就不能再上传。"""
    return bool(read_cloud_enabled()) and groq_key_configured()


def node_active():
    return bool(read_node_enabled()) and nodes_available()


def static_route_label():
    """没有接上路由器时，按落盘的开关给面板一个标签。"""
    if cloud_active():
        return "云端·Groq"
    if node_active():
        entries = load_node_entries()
        name = str(entries[0].get("id") or "") if entries else ""
        return f"混合·{name}" if name else "混合·节点"
    return "本机"


# 面板可调参数（存 window_state.json 的 "tuning"；FONT_SIZE/BACKGROUND_OPACITY
# 维持顶层独立键向后兼容，不进此集合）
TUNING_KEYS = (
    "CHUNK_SUBMIT_SECONDS",
    "BUFFER_TRIM_SEC",
    "IDLE_FLUSH_SEC",
    "ENERGY_THRESHOLD_SPEECH",
    "MAX_SUBTITLE_LENGTH",
    "MAX_SENTENCE_PAIRS",
    "LOOPBACK_DEVICE_NAME",
    "SHOW_BILINGUAL",
    "DISPLAY_MODE",
    "DRAFT_TRANSLATION",
    "SOURCE_TEXT_COLOR",
    "CHINESE_TEXT_COLOR",
    "BACKGROUND_COLOR",
    "TEXT_EDGE",
    "WINDOW_CORNER_RADIUS",
    "DRAFT_TEXT_COLOR",
    "UNSTABLE_TEXT_COLOR",
    "FONT_FAMILY",
    "TRANSLATION_STYLE",
    # 🎞 影院字幕条的样式。放这里而不是 state["cinema"]，是为了**只有一个
    # 数据源**——两边都存会在"面板改了值但没重启"时对不上。
    # state["cinema"] 现在只剩 screen_index（那是窗口状态不是 config 值，
    # 和 tv 的 screen_index 同类）。
    "CINEMA_FONT_SIZE",
    "CINEMA_SHOW_GERMAN",
    "CINEMA_BG_ALPHA",
    # 识别语言和四个场景模式正交，必须进 tuning 才能重启恢复，又不能进 PRESETS。
    "AUTO_DETECT_LANGUAGE",
    # ☠️ 源语言和目标语言要**分别**存。只存源语言的话，配了 zh→en 和 zh→de
    # 两条时重启就没法知道用户上次选的是哪一条，只能退回 target_for 的第一条。
    # 恢复时按整对校验（见 apply_tuning）。
    "SOURCE_LANGUAGE",
    "TARGET_LANGUAGE",
)


# 会被原样插进富文本/样式表的键，读回时必须过一遍校验（见 apply_tuning）
_COLOR_KEYS = (
    "SOURCE_TEXT_COLOR",
    "CHINESE_TEXT_COLOR",
    "BACKGROUND_COLOR",
    "DRAFT_TEXT_COLOR",
    "UNSTABLE_TEXT_COLOR",
)


def _display_mode_from_show_bilingual(show_bilingual):
    return "bilingual" if bool(show_bilingual) else "target"


def _normalize_display_mode(value, fallback=None):
    mode = str(value or "").strip().lower()
    valid = {mode for mode, _label in DISPLAY_MODE_OPTIONS}
    if mode in valid:
        return mode
    if fallback is not None:
        return _display_mode_from_show_bilingual(fallback)
    return "bilingual"


def _set_display_mode(mode):
    normalized = _normalize_display_mode(mode)
    config.DISPLAY_MODE = normalized
    config.SHOW_BILINGUAL = normalized == "bilingual"
    return normalized


def sync_display_mode_from_show_bilingual():
    """旧 SHOW_BILINGUAL 入口改动后，同步到三态 DISPLAY_MODE。"""
    return _set_display_mode(_display_mode_from_show_bilingual(
        getattr(config, "SHOW_BILINGUAL", True)))


def _sanitize_font_family(value):
    """字体族串会被插进 Qt 样式表（window_chrome._apply_styles 等）。

    剥掉能提前结束一条声明/整个规则块的字符，坏值就退回默认——否则
    window_state.json 里一个手改坏的值能让整张样式表失效，症状是"字幕突然
    没有样式了"，而没人会想到去看那个文件。
    """
    cleaned = str(value).replace("{", "").replace("}", "").replace(";", "").strip()
    return cleaned or "Microsoft YaHei, Arial"


def apply_tuning(tuning):
    """把 state['tuning'] 写回 config；坏键/类型异常跳过。

    ☠️ 颜色和字体不能只做 str() 就 setattr。它们随后会被**原样**插进富文本和
    样式表：颜色进 subtitle_render 的 `<span style="color:{...}">`（三处）、
    字体进 window_chrome/tv_window 的样式表 f-string。而这个函数的输入是
    window_state.json——一个手改坏的值现在会静默把整条字幕行渲染坏。
    同模块里本来就有写好的校验器 apply_text_color()，以前这里绕过了它。
    （能写 window_state.json 的人也能写 config_local.py，而后者是被 exec 的，
    所以这不是信任边界；这么改纯粹是因为校验器是现成的、成本为零。）
    """
    if not isinstance(tuning, dict):
        return
    has_display_mode = "DISPLAY_MODE" in tuning
    for key in TUNING_KEYS:
        if key not in tuning:
            continue
        try:
            val = tuning[key]
            if key in _COLOR_KEYS:
                apply_text_color(key, val)  # 自带 #rgb/#rrggbb 校验 + 规范化
                continue
            if key == "DISPLAY_MODE":
                _set_display_mode(val)
                continue
            if key == "TEXT_EDGE":
                edge = str(val).strip().lower()
                if edge not in {option for option, _label in TEXT_EDGE_OPTIONS}:
                    continue
                setattr(config, key, edge)
                continue
            if key == "WINDOW_CORNER_RADIUS":
                radius = int(val)
                if radius not in WINDOW_RADIUS_OPTIONS:
                    continue
                setattr(config, key, radius)
                setattr(config, "BORDER_RADIUS", radius)
                continue
            if key == "FONT_FAMILY":
                setattr(config, key, _sanitize_font_family(val))
                continue
            if key == "SOURCE_LANGUAGE":
                # 整对一起恢复：目标语言在同一份 tuning 里，能对上就用它，
                # 对不上（配置改过了）才退回这个源语言的默认目标
                src = str(val)
                if src not in language_policy.allowed_sources():
                    continue
                tgt = tuning.get("TARGET_LANGUAGE")
                tgt = str(tgt) if isinstance(tgt, str) else None
                if not tgt or (src, tgt) not in language_policy.allowed_pairs():
                    tgt = pair_target(src)
                setattr(config, "SOURCE_LANGUAGE", src)
                setattr(config, "TARGET_LANGUAGE", tgt)
                continue
            if key == "TARGET_LANGUAGE":
                continue  # 已经在 SOURCE_LANGUAGE 那一支里成对处理过
            current = getattr(config, key)
            if isinstance(current, bool):
                # json 可能给 0/1；bool 是 int 子类，必须先于 int 判断
                val = bool(val)
            elif isinstance(current, int):
                val = int(val)
            elif isinstance(current, float):
                val = float(val)
            elif isinstance(current, str):
                val = str(val)
            setattr(config, key, val)
            if key == "SHOW_BILINGUAL" and not has_display_mode:
                sync_display_mode_from_show_bilingual()
        except (TypeError, ValueError, AttributeError):
            continue


def collect_tuning():
    """从 config 组装 tuning dict（原样持久化当前值）。

    以前这里有一套"游戏模式期间豁免 CHUNK_SUBMIT_SECONDS/DRAFT_TRANSLATION"
    的逻辑——那是"临时开关+退出还原"语义的产物。现在四个模式平等并列、
    模式值就是用户的当前状态，必须照常存；留着豁免反而会让重启后面板值
    和恢复出来的模式高亮对不上。
    """
    return {key: getattr(config, key) for key in TUNING_KEYS}


def apply_text_color(config_key, hex_color):
    """写颜色到 config 并返回规范化 #rrggbb（可测，不弹对话框）。"""
    color = (hex_color or "").strip()
    if not color:
        raise ValueError("empty color")
    if not color.startswith("#"):
        color = "#" + color
    body = color[1:]
    if len(body) not in (3, 6) or any(c not in "0123456789abcdefABCDEF" for c in body):
        raise ValueError(f"bad color: {hex_color!r}")
    if len(body) == 3:
        body = "".join(c * 2 for c in body)
    normalized = "#" + body.lower()
    setattr(config, config_key, normalized)
    if config_key == "SOURCE_TEXT_COLOR":
        setattr(config, "TEXT_COLOR", "#" + body.upper())
    elif config_key == "TEXT_COLOR":
        setattr(config, "SOURCE_TEXT_COLOR", normalized)
    return normalized


def _style_color_button(button, hex_color, font_px=None):
    """颜色按钮底色显示当前色。"""
    px = font_px or button.font().pixelSize() or PANEL_FONT_PX_AT_100
    if px < 0:
        px = PANEL_FONT_PX_AT_100
    button.setStyleSheet(
        f"background-color: {hex_color}; color: #000; border: 1px solid #666; "
        f"padding: 6px 12px; min-width: 80px; min-height: {px + 8}px; "
        f"font-size: {px}px;"
    )


def snapshot_defaults():
    """config 出厂态快照（应用 state/tuning 之前调用）。含 FONT_SIZE/不透明度。"""
    defaults = {key: getattr(config, key) for key in TUNING_KEYS}
    defaults["FONT_SIZE"] = config.FONT_SIZE
    defaults["BACKGROUND_OPACITY"] = config.BACKGROUND_OPACITY
    return defaults


# 模式 → 图标：面板按钮、悬浮窗常驻指示器、状态提示共用这一份
MODE_ICONS = {"直播": "📺", "看剧": "🎬", "性能": "⚡", "精听": "🎧"}
_PRESET_BUTTONS = tuple((name, f"{icon} {name}") for name, icon in MODE_ICONS.items())

class SettingsWindow(DraggableWidget):
    """参数调节窗口"""

    def __init__(self, on_font_change=None, defaults=None, on_mode_change=None):
        super().__init__()
        self._on_font_change = on_font_change  # 显示相关改动 → 字幕/历史重刷样式+重渲染
        # 模式变更回调（main.py 构造完窗口后挂上 SubtitleApp._apply_mode）：
        # 面板按钮和"手动改参数变自定义"都通过它走那唯一一个入口
        self.on_mode_change = on_mode_change
        self.setWindowTitle("⚙️ 参数调节（可拖动）")
        self.setWindowFlags(Qt.WindowType.WindowStaysOnTopHint)

        # 真默认快照：由 SubtitleWindow 在应用 tuning 之前拍下并传入；
        # 单测直接 new 时回退到当前 config（等同出厂若未改过）。
        if defaults is not None:
            self._defaults = dict(defaults)
        else:
            self._defaults = snapshot_defaults()

        # 当前模式（None=自定义，即用户手动拨过滑块）
        self._active_preset = None
        self._applying_preset = False
        self._preset_buttons = {}  # name -> QPushButton
        self.on_language_change = None  # 源语言码 → main 走 request_switch_language
        self._lang_pair_buttons = {}  # (source, target) -> QPushButton
        # 🎞 影院条由 SubtitleWindow 建好之后挂进来（它比本面板晚构造）
        self._cinema_bar = None
        if bool(getattr(config, "SHOW_BILINGUAL", True)):
            _set_display_mode("bilingual")
        elif _normalize_display_mode(getattr(config, "DISPLAY_MODE", None)) == "bilingual":
            _set_display_mode("target")

        scale = screen_scale_factor()
        self._panel_font_px = panel_font_px(scale)
        self._apply_panel_chrome(scale)

        inner = QWidget()
        layout = QVBoxLayout(inner)

        # 模式（面板最顶部）
        preset_group = QGroupBox("模式")
        preset_layout = QHBoxLayout()
        for name, label in _PRESET_BUTTONS:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setToolTip(f"切换到「{name}」模式（整套套用：节奏/句对/双语/草稿/解码/语域）")
            px = self._panel_font_px
            btn.setFont(self.font())
            btn.setStyleSheet(
                f"QPushButton {{ padding: 8px 12px; font-size: {px}px; "
                f"min-height: {px + 12}px; }}"
                "QPushButton:checked {"
                "  background-color: #3a6ea5; color: white; font-weight: bold;"
                "  border: 1px solid #5a8ec5;"
                "}"
            )
            btn.clicked.connect(lambda checked=False, n=name: self._on_preset_clicked(n))
            self._preset_buttons[name] = btn
            preset_layout.addWidget(btn)
        preset_group.setLayout(preset_layout)
        layout.addWidget(preset_group)

        lang_group = QGroupBox("识别语言")
        lang_layout = QVBoxLayout()
        self.auto_detect_cb = QCheckBox("自动识别语言（在已配置的语言对之间切换）")
        self.auto_detect_cb.setFont(self.font())
        self.auto_detect_cb.setToolTip(
            "打开后连续多次检测到同一新语言才切换。和上面的直播/看剧/性能/精听无关。")
        self.auto_detect_cb.setChecked(bool(getattr(config, "AUTO_DETECT_LANGUAGE", False)))
        self.auto_detect_cb.toggled.connect(self._on_auto_detect_toggled)
        lang_layout.addWidget(self.auto_detect_cb)

        self.current_pair_label = QLabel(
            "当前: " + pair_label(config.SOURCE_LANGUAGE, config.TARGET_LANGUAGE))
        self.current_pair_label.setFont(self.font())
        lang_layout.addWidget(self.current_pair_label)

        pair_row = QWidget()
        pair_layout = QHBoxLayout()
        pair_layout.setContentsMargins(0, 0, 0, 0)
        px = self._panel_font_px
        for src, tgt in configured_language_pairs():
            btn = QPushButton(pair_label(src, tgt))
            btn.setCheckable(True)
            btn.setFont(self.font())
            btn.setStyleSheet(
                f"QPushButton {{ padding: 8px 12px; font-size: {px}px; "
                f"min-height: {px + 12}px; }}"
                "QPushButton:checked {"
                "  background-color: #3a6ea5; color: white; font-weight: bold;"
                "  border: 1px solid #5a8ec5;"
                "}"
            )
            btn.setToolTip("手动选择识别语言对；自动识别开着时也会立即生效并进入冷却")
            btn.clicked.connect(
                lambda checked=False, s=src, t=tgt: self._on_pair_clicked(s, t))
            # ☠️ 键是**整对**不是源语言：配了 zh→en 和 zh→de 两条时，按源语言
            # 索引会让后一个按钮把前一个挤掉，面板上只剩一个、还切不了目标语言
            self._lang_pair_buttons[(src, tgt)] = btn
            pair_layout.addWidget(btn)
        pair_row.setLayout(pair_layout)
        lang_layout.addWidget(pair_row)
        lang_group.setLayout(lang_layout)
        layout.addWidget(lang_group)
        self._sync_language_buttons()

        # 流式识别设置
        duration_group = QGroupBox("流式识别设置")
        duration_layout = QVBoxLayout()

        self.chunk_submit_slider = self._create_slider(
            "提交节奏(秒)", 0.3, 2.0, config.CHUNK_SUBMIT_SECONDS, 0.1,
            lambda v: setattr(config, 'CHUNK_SUBMIT_SECONDS', v)
        )
        self.buffer_trim_slider = self._create_slider(
            "识别缓冲上限(秒)", 6.0, 20.0, config.BUFFER_TRIM_SEC, 1.0,
            lambda v: setattr(config, 'BUFFER_TRIM_SEC', v)
        )
        self.idle_flush_slider = self._create_slider(
            "收尾静音(秒)", 1.0, 5.0, config.IDLE_FLUSH_SEC, 0.5,
            lambda v: setattr(config, 'IDLE_FLUSH_SEC', v)
        )

        duration_layout.addWidget(self.chunk_submit_slider['widget'])
        duration_layout.addWidget(self.buffer_trim_slider['widget'])
        duration_layout.addWidget(self.idle_flush_slider['widget'])
        duration_group.setLayout(duration_layout)

        # 音频设备 + 静音门
        energy_group = QGroupBox("音频捕获")
        energy_layout = QVBoxLayout()

        device_row = QWidget()
        device_layout = QHBoxLayout()
        device_layout.setContentsMargins(0, 0, 0, 0)
        device_label = QLabel("设备名包含:")
        self._fit_label_width(device_label)
        device_label.setToolTip("空=系统默认播放设备；填 FiiO / Speakers 等子串匹配 loopback（约5秒内热切换）")
        self.device_name_edit = QLineEdit()
        self.device_name_edit.setFont(self.font())
        self.device_name_edit.setPlaceholderText("空=默认播放设备")
        self.device_name_edit.setText(getattr(config, 'LOOPBACK_DEVICE_NAME', '') or '')
        self.device_name_edit.setToolTip(device_label.toolTip())
        self.device_name_edit.editingFinished.connect(self._on_device_name_changed)
        device_layout.addWidget(device_label)
        device_layout.addWidget(self.device_name_edit)
        device_row.setLayout(device_layout)
        energy_layout.addWidget(device_row)

        self.speech_threshold_slider = self._create_slider(
            "语音能量阈值", 0.005, 0.05, config.ENERGY_THRESHOLD_SPEECH, 0.001,
            lambda v: setattr(config, 'ENERGY_THRESHOLD_SPEECH', v)
        )

        energy_layout.addWidget(self.speech_threshold_slider['widget'])
        energy_group.setLayout(energy_layout)

        # 字幕显示设置
        display_group = QGroupBox("字幕显示设置")
        display_layout = QVBoxLayout()

        self.max_length_slider = self._create_slider(
            "最大字符数", 50, 600, config.MAX_SUBTITLE_LENGTH, 10,
            lambda v: setattr(config, 'MAX_SUBTITLE_LENGTH', int(v))
        )

        self.max_pairs_slider = self._create_slider(
            "句对条数上限", 1, 20, config.MAX_SENTENCE_PAIRS, 1,
            lambda v: setattr(config, 'MAX_SENTENCE_PAIRS', int(v))
        )

        def set_font_size(v):
            config.FONT_SIZE = int(v)
            if self._on_font_change:
                self._on_font_change()

        self.font_size_slider = self._create_spinbox(
            "字幕字号", 14, 72, config.FONT_SIZE, set_font_size
        )

        def set_bg_opacity(v):
            config.BACKGROUND_OPACITY = int(v)
            if self._on_font_change:
                self._on_font_change()  # 同一个回调：重刷字幕样式

        self.bg_opacity_slider = self._create_slider(
            "背景不透明度", 100, 255, config.BACKGROUND_OPACITY, 5, set_bg_opacity
        )

        style_group = QGroupBox("字幕样式")
        style_layout = QVBoxLayout()

        self.source_color_palette = self._create_color_palette_row(
            "原文颜色", "SOURCE_TEXT_COLOR", STYLE_TEXT_COLORS)
        self.target_color_palette = self._create_color_palette_row(
            "译文颜色", "CHINESE_TEXT_COLOR", STYLE_TEXT_COLORS)
        self.background_color_palette = self._create_color_palette_row(
            "背景颜色", "BACKGROUND_COLOR", BACKGROUND_COLORS, custom_label=None)
        self.text_edge_row = self._create_button_group_row(
            "文字边缘", TEXT_EDGE_OPTIONS, getattr(config, "TEXT_EDGE", "none"),
            self._set_text_edge)
        self.corner_radius_row = self._create_button_group_row(
            "窗口圆角", tuple((v, str(v)) for v in WINDOW_RADIUS_OPTIONS),
            getattr(config, "WINDOW_CORNER_RADIUS", 8), self._set_corner_radius)
        self.display_mode_row = self._create_button_group_row(
            "显示模式", DISPLAY_MODE_OPTIONS,
            _normalize_display_mode(getattr(config, "DISPLAY_MODE", None),
                                    getattr(config, "SHOW_BILINGUAL", True)),
            self._set_display_mode_from_ui)

        style_layout.addWidget(self.font_size_slider['widget'])
        style_layout.addWidget(self.source_color_palette['widget'])
        style_layout.addWidget(self.target_color_palette['widget'])
        style_layout.addWidget(self.background_color_palette['widget'])
        style_layout.addWidget(self.bg_opacity_slider['widget'])
        style_layout.addWidget(self.text_edge_row['widget'])
        style_layout.addWidget(self.corner_radius_row['widget'])
        style_layout.addWidget(self.display_mode_row['widget'])
        style_group.setLayout(style_layout)

        # 只显中文：旧二态入口保留，和「显示模式」双向同步。
        self.chinese_only_cb = QCheckBox("只显中文（隐藏德语原文）")
        self.chinese_only_cb.setFont(self.font())
        self.chinese_only_cb.setToolTip("兼容旧开关；等同于显示模式里的「仅译文」")
        self.chinese_only_cb.setChecked(getattr(config, "DISPLAY_MODE", "bilingual") == "target")
        self.chinese_only_cb.toggled.connect(self._on_chinese_only_toggled)

        # 草稿中文：游戏模式会禁用此开关
        self.draft_cb = QCheckBox("草稿中文（残句先出浅蓝译文）")
        self.draft_cb.setFont(self.font())
        self.draft_cb.setToolTip("翻译 worker 空闲时出草稿；游戏模式会强制关闭")
        self.draft_cb.setChecked(bool(getattr(config, "DRAFT_TRANSLATION", True)))
        self.draft_cb.toggled.connect(self._on_draft_toggled)

        self.color_btn_chinese = self.target_color_palette["custom"]
        self.color_btn_draft = self._make_color_button(
            "草稿中文", "DRAFT_TEXT_COLOR",
            getattr(config, "DRAFT_TEXT_COLOR", "#8fb8e0"),
        )
        self.color_btn_unstable = self._make_color_button(
            "未稳定德语", "UNSTABLE_TEXT_COLOR",
            getattr(config, "UNSTABLE_TEXT_COLOR", "#999999"),
        )
        color_row = QWidget()
        color_layout = QHBoxLayout()
        color_layout.setContentsMargins(0, 0, 0, 0)
        color_layout.addWidget(QLabel("辅助颜色:"))
        color_layout.addWidget(self.color_btn_draft)
        color_layout.addWidget(self.color_btn_unstable)
        color_layout.addStretch()
        color_row.setLayout(color_layout)

        # 字体族：config 存 "Family, Arial"，写回时保留 Arial 兜底
        font_row = QWidget()
        font_layout = QHBoxLayout()
        font_layout.setContentsMargins(0, 0, 0, 0)
        font_label = QLabel("字体:")
        self._fit_label_width(font_label)
        self.font_combo = QFontComboBox()
        self.font_combo.setMaxVisibleItems(20)
        primary = (config.FONT_FAMILY or "Microsoft YaHei").split(",")[0].strip()
        self.font_combo.setCurrentFont(QFont(primary))
        self.font_combo.currentFontChanged.connect(self._on_font_family_changed)
        font_layout.addWidget(font_label)
        font_layout.addWidget(self.font_combo)
        font_row.setLayout(font_layout)

        display_layout.addWidget(self.max_length_slider['widget'])
        display_layout.addWidget(self.max_pairs_slider['widget'])
        display_layout.addWidget(style_group)
        display_layout.addWidget(self.chinese_only_cb)
        display_layout.addWidget(self.draft_cb)
        display_layout.addWidget(color_row)
        display_layout.addWidget(font_row)
        display_group.setLayout(display_layout)

        # ---- 🎞 影院字幕条（全屏看剧叠在视频底部的那条）----
        # 单独一组：它和上面那些"悬浮窗长什么样"的项互不影响，混在一起会让人
        # 以为改字号也会动悬浮窗。三个控件都 mark_custom=False，理由见 _create_slider。
        cinema_group = QGroupBox("🎞 影院字幕条（Ctrl+Alt+C 开关）")
        cinema_layout = QVBoxLayout()

        def set_cinema_font(v):
            config.CINEMA_FONT_SIZE = int(v)
            self._restyle_cinema()

        self.cinema_font_slider = self._create_slider(
            "字幕条字号", config.CINEMA_FONT_SIZE_MIN, config.CINEMA_FONT_SIZE_MAX,
            config.CINEMA_FONT_SIZE, 2, set_cinema_font, mark_custom=False)

        def set_cinema_alpha(v):
            config.CINEMA_BG_ALPHA = int(v)
            self._restyle_cinema()

        self.cinema_alpha_slider = self._create_slider(
            "底衬浓度", 0, 255, getattr(config, "CINEMA_BG_ALPHA", 150), 5,
            set_cinema_alpha, mark_custom=False)
        self.cinema_alpha_slider['widget'].setToolTip(
            "文字背后那层黑的不透明度。0 = 全透明（亮画面上会看不清）")

        self.cinema_german_cb = QCheckBox("字幕条也显示德语原文")
        self.cinema_german_cb.setFont(self.font())
        self.cinema_german_cb.setToolTip("取消勾选 = 纯中文，最不挡画面")
        self.cinema_german_cb.setChecked(bool(getattr(config, "CINEMA_SHOW_GERMAN", True)))
        self.cinema_german_cb.toggled.connect(self._on_cinema_german_toggled)

        cinema_layout.addWidget(self.cinema_font_slider['widget'])
        cinema_layout.addWidget(self.cinema_alpha_slider['widget'])
        cinema_layout.addWidget(self.cinema_german_cb)
        cinema_group.setLayout(cinema_layout)

        # 识别来源和四个场景模式正交：不进 TUNING_KEYS，也不走 _apply_mode。
        # 控件初值不在这里 setChecked——那会在信号接通后把用户目录写成默认值。
        # 真正的显示在 _refresh_remote_controls，并且挡住信号。
        self.on_remote_change = None
        self.route_status_source = None
        remote_group = QGroupBox("识别来源")
        remote_layout = QVBoxLayout()

        self.node_cb = QCheckBox("使用算力节点")
        self.node_cb.setFont(self.font())
        self.node_cb.setToolTip("音频会发到你的节点机器")
        self.node_cb.toggled.connect(self._on_node_toggled)
        self.node_reason = QLabel("")
        self.node_reason.setFont(self.font())
        self.node_reason.setWordWrap(True)

        key_row = QWidget()
        key_layout = QHBoxLayout()
        key_layout.setContentsMargins(0, 0, 0, 0)
        self.groq_key_edit = QLineEdit()
        self.groq_key_edit.setFont(self.font())
        self.groq_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.groq_key_edit.setPlaceholderText("粘贴 Groq key")
        self.groq_key_edit.setToolTip("只写入本机 key 文件，不会回显已保存的 key")
        self.groq_save_btn = QPushButton("保存 key")
        self.groq_save_btn.setFont(self.font())
        self.groq_save_btn.clicked.connect(self._on_save_groq_key)
        self.groq_clear_btn = QPushButton("清除 key")
        self.groq_clear_btn.setFont(self.font())
        self.groq_clear_btn.clicked.connect(self._on_clear_groq_key)
        key_layout.addWidget(self.groq_key_edit)
        key_layout.addWidget(self.groq_save_btn)
        key_layout.addWidget(self.groq_clear_btn)
        key_row.setLayout(key_layout)

        self.cloud_cb = QCheckBox("云端识别")
        self.cloud_cb.setFont(self.font())
        self.cloud_cb.setToolTip("打开后音频会上传到 Groq")
        self.cloud_cb.toggled.connect(self._on_cloud_toggled)
        self.route_label = QLabel("当前路由：本机")
        self.route_label.setFont(self.font())

        remote_layout.addWidget(self.node_cb)
        remote_layout.addWidget(self.node_reason)
        remote_layout.addWidget(key_row)
        remote_layout.addWidget(self.cloud_cb)
        remote_layout.addWidget(self.route_label)
        remote_group.setLayout(remote_layout)

        # 添加到主布局
        layout.addWidget(duration_group)
        layout.addWidget(energy_group)
        layout.addWidget(display_group)
        layout.addWidget(cinema_group)
        layout.addWidget(remote_group)

        # 重置按钮
        reset_btn = QPushButton("🔄 恢复默认值")
        reset_btn.clicked.connect(self._reset_defaults)
        layout.addWidget(reset_btn)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(inner)
        self._scroll = scroll

        outer = QVBoxLayout()
        outer.setContentsMargins(8, 8, 8, 8)
        outer.addWidget(scroll)
        self.setLayout(outer)
        # 构造完再读盘。blockSignals 包在刷新里，避免把默认值写进用户目录。
        self._refresh_remote_controls()

    def _apply_panel_chrome(self, scale):
        """面板字体 + 受屏幕约束的初始几何。不开全局 DPI 缩放。"""
        px = self._panel_font_px
        font = QFont("Microsoft YaHei")
        font.setPixelSize(px)
        self.setFont(font)
        self.setStyleSheet(
            f"QWidget {{ font-size: {px}px; }}"
            f"QGroupBox {{ font-size: {px}px; font-weight: bold; "
            f"padding-top: {px + 8}px; }}"
            f"QCheckBox {{ font-size: {px}px; min-height: {px + 10}px; spacing: 8px; }}"
            f"QLineEdit, QComboBox, QFontComboBox {{ font-size: {px}px; "
            f"min-height: {px + 10}px; }}"
            f"QPushButton {{ font-size: {px}px; min-height: {px + 12}px; "
            f"padding: 8px 12px; }}"
            f"QLabel {{ font-size: {px}px; }}"
            f"QSlider {{ min-height: {max(24, px)}px; }}"
        )
        screen = QApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            x, y, w, h = settings_initial_geometry(
                area.x(), area.y(), area.width(), area.height(), scale)
            self.setGeometry(x, y, w, h)
        else:
            self.setGeometry(100, 100, 560, 720)

    def _fit_label_width(self, label):
        """标题宽度按当前字体测量，避免放大后被 120px 截断。"""
        fm = label.fontMetrics()
        label.setMinimumWidth(fm.horizontalAdvance(label.text()) + 12)

    def _create_slider(self, label, min_val, max_val, current_val, step, callback,
                       mark_custom=True):
        """创建滑块控件。

        mark_custom=False 用于**和四个模式正交**的控件：PRESETS 里没有任何一个
        模式定义 CINEMA_* 键，所以调影院条的字号并没有偏离当前模式，不该把
        左上角指示器打成「⚙️ 自定义」。（模式身份一掉就再也回不去，是这个面板
        本来就有的痛点，别再往里加新的触发点。）
        """
        widget = QWidget()
        layout = QHBoxLayout()

        # 标签
        label_widget = QLabel(f"{label}:")
        label_widget.setFont(self.font())
        self._fit_label_width(label_widget)

        # 滑块（用round避免浮点截断，如0.01/0.001=9.999...被int截成9）
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setMinimum(round(min_val / step))
        slider.setMaximum(round(max_val / step))
        slider.setValue(round(current_val / step))

        # 数值显示：位数跟步长走（字幕字号 24，不是 24.000）
        value_label = QLabel(format_slider_value(current_val, step))
        value_label.setFont(self.font())
        sample = format_slider_value(max_val, step)
        value_label.setMinimumWidth(value_label.fontMetrics().horizontalAdvance(sample) + 10)

        # 滑块变化时更新；手动改动会清掉预设高亮（apply_preset 期间跳过）
        def on_change(int_value):
            value = int_value * step
            shown = format_slider_value(value, step)
            value_label.setText(shown)
            if mark_custom:
                self._mark_custom()
            callback(value)
            print(f"📊 {label}: {shown}")

        slider.valueChanged.connect(on_change)

        layout.addWidget(label_widget)
        layout.addWidget(slider)
        layout.addWidget(value_label)
        widget.setLayout(layout)

        return {
            'widget': widget, 'slider': slider, 'label': value_label,
            'title': label_widget, 'step': step,
        }

    def _create_spinbox(self, label, min_val, max_val, current_val, callback,
                        mark_custom=True):
        """创建数值框控件；返回结构兼容旧 slider 测试和 Ctrl+滚轮路径。"""
        widget = QWidget()
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)

        label_widget = QLabel(f"{label}:")
        label_widget.setFont(self.font())
        self._fit_label_width(label_widget)

        spin = QSpinBox()
        spin.setFont(self.font())
        spin.setRange(int(min_val), int(max_val))
        spin.setSingleStep(1)
        spin.setValue(int(current_val))
        spin.setMinimumWidth(spin.fontMetrics().horizontalAdvance(str(max_val)) + 48)

        def on_change(value):
            if mark_custom:
                self._mark_custom()
            callback(int(value))
            print(f"📊 {label}: {int(value)}")

        spin.valueChanged.connect(on_change)
        layout.addWidget(label_widget)
        layout.addWidget(spin)
        layout.addStretch()
        widget.setLayout(layout)
        return {
            "widget": widget, "slider": spin, "label": spin,
            "title": label_widget, "step": 1,
        }

    def _make_segment_button(self, text):
        btn = QPushButton(text)
        btn.setCheckable(True)
        btn.setFont(self.font())
        px = self._panel_font_px
        btn.setStyleSheet(
            f"QPushButton {{ padding: 6px 10px; font-size: {px}px; "
            f"min-height: {px + 8}px; }}"
            "QPushButton:checked {"
            "  background-color: #3a6ea5; color: white; font-weight: bold;"
            "  border: 1px solid #5a8ec5;"
            "}"
        )
        return btn

    def _create_button_group_row(self, label, options, current, callback):
        row = QWidget()
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        label_widget = QLabel(f"{label}:")
        label_widget.setFont(self.font())
        self._fit_label_width(label_widget)
        layout.addWidget(label_widget)
        group = QButtonGroup(row)
        group.setExclusive(True)
        buttons = {}
        for value, text in options:
            btn = self._make_segment_button(text)
            btn.setChecked(value == current)
            group.addButton(btn)
            layout.addWidget(btn)
            buttons[value] = btn
        layout.addStretch()
        row.setLayout(layout)

        def on_clicked(button):
            for value, btn in buttons.items():
                if btn is button:
                    self._mark_custom()
                    callback(value)
                    if self._on_font_change:
                        self._on_font_change()
                    print(f"📊 {label}: {btn.text()}")
                    return

        group.buttonClicked.connect(on_clicked)
        return {"widget": row, "buttons": buttons, "group": group, "title": label_widget}

    def _create_color_palette_row(self, label, config_key, colors, custom_label="自定义…"):
        row = QWidget()
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        label_widget = QLabel(f"{label}:")
        label_widget.setFont(self.font())
        self._fit_label_width(label_widget)
        layout.addWidget(label_widget)
        group = QButtonGroup(row)
        group.setExclusive(True)
        buttons = {}
        for name, color in colors:
            btn = QPushButton()
            btn.setCheckable(True)
            btn.setToolTip(f"{name} {color}")
            btn.setFixedSize(34, 28)
            btn.setChecked(getattr(config, config_key, "").lower() == color.lower())
            self._style_swatch_button(btn, color)
            group.addButton(btn)
            layout.addWidget(btn)
            buttons[color.lower()] = btn
        custom = None
        if custom_label:
            custom = self._make_color_button(
                custom_label, config_key, getattr(config, config_key, "#ffffff"))
            layout.addWidget(custom)
        layout.addStretch()
        row.setLayout(layout)

        def on_clicked(button):
            for _name, color in colors:
                if buttons[color.lower()] is button:
                    self.apply_color(config_key, color)
                    self._refresh_palette_checks(config_key)
                    return

        group.buttonClicked.connect(on_clicked)
        return {
            "widget": row, "buttons": buttons, "custom": custom,
            "colors": tuple(color.lower() for _name, color in colors),
            "title": label_widget,
        }

    def _style_swatch_button(self, button, hex_color):
        border = "#ffffff" if button.isChecked() else "#666666"
        button.setStyleSheet(
            f"background-color: {hex_color}; border: 2px solid {border}; "
            "border-radius: 4px;"
        )

    def _refresh_palette_checks(self, config_key):
        rows = {
            "SOURCE_TEXT_COLOR": getattr(self, "source_color_palette", None),
            "CHINESE_TEXT_COLOR": getattr(self, "target_color_palette", None),
            "BACKGROUND_COLOR": getattr(self, "background_color_palette", None),
        }
        row = rows.get(config_key)
        if not row:
            return
        current = getattr(config, config_key, "").lower()
        for color, btn in row["buttons"].items():
            btn.blockSignals(True)
            btn.setChecked(color == current)
            btn.blockSignals(False)
            self._style_swatch_button(btn, color)

    def _make_color_button(self, label, config_key, hex_color):
        """颜色选择按钮：底色=当前色，点击弹 QColorDialog。"""
        btn = QPushButton(label)
        btn.setToolTip(f"点击选择{label}颜色")
        btn.setFont(self.font())
        _style_color_button(btn, hex_color, self._panel_font_px)
        btn.clicked.connect(lambda: self._pick_color(config_key, btn))
        return btn

    def _pick_color(self, config_key, button):
        """弹颜色对话框；有效则写 config + 刷按钮 + 重渲染。"""
        current = QColor(getattr(config, config_key, "#ffffff"))
        chosen = QColorDialog.getColor(current, self, "选择颜色")
        if not chosen.isValid():
            return
        self.apply_color(config_key, chosen.name(), button)

    def apply_color(self, config_key, hex_color, button=None):
        """应用颜色（可测入口，不弹对话框）。button 可选，用于刷新底色。"""
        self._mark_custom()
        normalized = apply_text_color(config_key, hex_color)
        if button is not None:
            _style_color_button(button, normalized)
        else:
            # 按 config_key 找对应按钮
            mapping = {
                "SOURCE_TEXT_COLOR": getattr(getattr(self, "source_color_palette", {}), "get", lambda *_: None)("custom"),
                "CHINESE_TEXT_COLOR": getattr(self, "color_btn_chinese", None),
                "BACKGROUND_COLOR": getattr(getattr(self, "background_color_palette", {}), "get", lambda *_: None)("custom"),
                "DRAFT_TEXT_COLOR": getattr(self, "color_btn_draft", None),
                "UNSTABLE_TEXT_COLOR": getattr(self, "color_btn_unstable", None),
            }
            btn = mapping.get(config_key)
            if btn is not None:
                _style_color_button(btn, normalized)
        self._refresh_palette_checks(config_key)
        if self._on_font_change:
            self._on_font_change()
        print(f"📊 {config_key}: {normalized}")
        return normalized

    def _set_text_edge(self, value):
        config.TEXT_EDGE = str(value)

    def _set_corner_radius(self, value):
        radius = int(value)
        config.WINDOW_CORNER_RADIUS = radius
        config.BORDER_RADIUS = radius

    def _set_display_mode_from_ui(self, value):
        mode = _set_display_mode(value)
        self._sync_display_controls(mode)

    def _sync_display_controls(self, mode=None):
        mode = mode or _normalize_display_mode(
            getattr(config, "DISPLAY_MODE", None), getattr(config, "SHOW_BILINGUAL", True))
        for value, btn in self.display_mode_row["buttons"].items():
            btn.blockSignals(True)
            btn.setChecked(value == mode)
            btn.blockSignals(False)
        self.chinese_only_cb.blockSignals(True)
        self.chinese_only_cb.setChecked(mode == "target")
        self.chinese_only_cb.blockSignals(False)

    def _sync_style_option_buttons(self):
        edge = getattr(config, "TEXT_EDGE", "none")
        for value, btn in self.text_edge_row["buttons"].items():
            btn.blockSignals(True)
            btn.setChecked(value == edge)
            btn.blockSignals(False)
        radius = int(getattr(config, "WINDOW_CORNER_RADIUS", 8))
        for value, btn in self.corner_radius_row["buttons"].items():
            btn.blockSignals(True)
            btn.setChecked(value == radius)
            btn.blockSignals(False)

    def _on_cinema_german_toggled(self, checked):
        """字幕条德语行开关。不 _mark_custom（PRESETS 不定义 CINEMA_* 键）。"""
        config.CINEMA_SHOW_GERMAN = bool(checked)
        self._restyle_cinema(clear=True)
        print(f"🎞 字幕条德语原文: {'显示' if checked else '隐藏'}")

    def _restyle_cinema(self, clear=False):
        """把改好的样式推给影院条。

        ☠️ 面板比 cinema_bar 先构造（SubtitleWindow.__init__ 的顺序），所以只能
        惰性取——但回调只可能在用户点面板时触发，那时它早就建好了。
        clear=True：德语开关切换时要连内容一起清，否则关掉德语后**上一行德语
        还留在屏幕上**，直到下一句才消失。
        """
        bar = getattr(self, "_cinema_bar", None)
        if bar is None:
            return
        if clear:
            bar.clear_text()
        bar._apply_font()
        bar._relayout()

    def _on_chinese_only_toggled(self, checked):
        """勾上=仅译文；取消=双语。仅原文需用「显示模式」三态按钮。"""
        self._mark_custom()
        mode = "target" if checked else "bilingual"
        _set_display_mode(mode)
        self._sync_display_controls(mode)
        print(f"📊 只显中文: {'开' if checked else '关'} (DISPLAY_MODE={config.DISPLAY_MODE})")
        if self._on_font_change:
            self._on_font_change()

    def _on_draft_toggled(self, checked):
        self._mark_custom()
        config.DRAFT_TRANSLATION = bool(checked)
        print(f"📊 草稿中文: {'开' if checked else '关'}")
        if self._on_font_change:
            # 关掉时要立刻把已经显示出来的那条草稿摘掉（清理在
            # _on_display_settings_change 里），不然它会一直挂着
            self._on_font_change()

    def _on_font_family_changed(self, font):
        family = font.family().strip() if font is not None else ""
        if not family:
            return
        self._mark_custom()
        config.FONT_FAMILY = f"{family}, Arial"
        print(f"📊 字体: {config.FONT_FAMILY}")
        if self._on_font_change:
            self._on_font_change()

    def _on_device_name_changed(self):
        """设备名子串写入 config；捕获线程约 5 秒内重开流"""
        self._mark_custom()
        name = self.device_name_edit.text().strip()
        config.LOOPBACK_DEVICE_NAME = name
        print(f"📊 捕获设备名包含: {name or '（系统默认）'}")

    def _emit_remote_change(self):
        callback = self.on_remote_change
        if callback is not None:
            callback()

    def _refresh_remote_controls(self):
        """控件显示只从文件读回。信号先挡住，否则 setChecked 会再写一遍。"""
        has_nodes = nodes_available()
        self.node_cb.blockSignals(True)
        self.node_cb.setEnabled(has_nodes)
        # 没有 nodes.json 时勾选框显示关，但不改 node.json：文件还没到，偏好先留着。
        self.node_cb.setChecked(bool(read_node_enabled()) and has_nodes)
        self.node_cb.blockSignals(False)
        if has_nodes:
            self.node_reason.setText("")
        else:
            self.node_reason.setText("没有 nodes.json，开关不可用。音频不会发到节点。")

        configured = groq_key_configured()
        self.cloud_cb.blockSignals(True)
        self.cloud_cb.setChecked(bool(read_cloud_enabled()) and configured)
        self.cloud_cb.blockSignals(False)

        # 正在输入时不要把框清掉，否则状态刷新会吞掉还没保存的 key。
        if not self.groq_key_edit.hasFocus():
            self.groq_key_edit.clear()
        if configured:
            self.groq_key_edit.setPlaceholderText("已保存（不会回显）")
        else:
            self.groq_key_edit.setPlaceholderText("粘贴 Groq key")

        label = static_route_label()
        source = self.route_status_source
        if source is not None:
            try:
                label = source() or label
            except Exception:
                label = static_route_label()
        self.route_label.setText(f"当前路由：{label}")

    def _on_node_toggled(self, checked):
        if not nodes_available():
            self._refresh_remote_controls()
            return
        try:
            write_node_enabled(bool(checked))
        except Exception as exc:
            print(f"⚠️  保存节点开关失败: {type(exc).__name__}")
        self._refresh_remote_controls()
        self._emit_remote_change()

    def _on_cloud_toggled(self, checked):
        try:
            write_cloud_enabled(bool(checked))
        except Exception as exc:
            print(f"⚠️  保存云端开关失败: {type(exc).__name__}")
        self._refresh_remote_controls()
        self._emit_remote_change()

    def _on_save_groq_key(self):
        text = self.groq_key_edit.text().strip()
        # 空保存什么都不做：密码框本来就不回显，空串不能当成「删掉 key」。
        if not text:
            return
        try:
            write_groq_key(text)
            write_cloud_enabled(True)
        except Exception as exc:
            print(f"⚠️  保存 key 失败: {type(exc).__name__}")
            return
        self.groq_key_edit.clear()
        self._refresh_remote_controls()
        self._emit_remote_change()

    def _on_clear_groq_key(self):
        try:
            clear_groq_key()
            write_cloud_enabled(False)
        except Exception as exc:
            print(f"⚠️  清除 key 失败: {type(exc).__name__}")
            return
        self.groq_key_edit.clear()
        self._refresh_remote_controls()
        self._emit_remote_change()

    # ------------------------------------------------------------------
    # 场景预设
    # ------------------------------------------------------------------
    def _mark_custom(self):
        """用户手动改控件 → 变"自定义"。应用模式期间跳过。

        这里是唯一一处"主动声明变成自定义"的地方；通知 main 让悬浮窗上的
        常驻指示器跟着改（指示器不能撒谎说还在某个模式里）。
        已经在主线程（滑块 valueChanged 回调），直接调不用走信号。
        """
        if getattr(self, "_applying_preset", False):
            return
        if self._active_preset is not None:
            self._active_preset = None
            self._sync_preset_button_highlight()
            if self.on_mode_change:
                self.on_mode_change(None)

    def _sync_preset_button_highlight(self):
        """按 _active_preset 刷新按钮 checked 态（高亮）。"""
        for name, btn in self._preset_buttons.items():
            btn.blockSignals(True)
            btn.setChecked(name == self._active_preset)
            btn.blockSignals(False)

    def restore_active_preset(self, name):
        """启动时只恢复高亮，不重新 apply（tuning 已含具体值）。"""
        presets = getattr(config, "PRESETS", {}) or {}
        if name and name in presets:
            self._active_preset = name
        else:
            self._active_preset = None
        self._sync_preset_button_highlight()

    def _on_auto_detect_toggled(self, checked):
        """自动识别和四个场景模式正交，不把指示器打成自定义。"""
        config.AUTO_DETECT_LANGUAGE = bool(checked)

    def _apply_language_locally(self, src, tgt=None):
        config.SOURCE_LANGUAGE = src
        config.TARGET_LANGUAGE = tgt or pair_target(src)
        self.current_pair_label.setText(
            "当前: " + pair_label(src, config.TARGET_LANGUAGE))

    def _sync_language_buttons(self):
        current = (getattr(config, "SOURCE_LANGUAGE", "de"),
                   getattr(config, "TARGET_LANGUAGE", "zh"))
        for pair, btn in self._lang_pair_buttons.items():
            btn.blockSignals(True)
            btn.setChecked(pair == current)
            btn.blockSignals(False)
        self.current_pair_label.setText("当前: " + pair_label(*current))

    def _on_pair_clicked(self, src, tgt=None):
        """手动选语言对：有回调就交给 main（走 request_switch_language），
        高亮等实际应用后再刷，避免「点了但还没切上」的假状态。"""
        if self.on_language_change:
            self.on_language_change(src, tgt)
            self._sync_language_buttons()
            return
        self._apply_language_locally(src, tgt)
        self._sync_language_buttons()

    def _on_preset_clicked(self, name):
        """模式按钮：只把请求转给 main 的唯一入口 _apply_mode。

        面板不再自己动控件（以前那套 apply_preset/_apply_preset_key 的 widget
        旁路已删）——config 由 _apply_mode 写，控件显示由它回调回来的
        mode 信号 → _on_mode_applied → refresh_from_config 统一刷。
        按钮 clicked 自带的 toggle 会被随后的 restore_active_preset 覆盖成正确态。
        """
        if self.on_mode_change:
            self.on_mode_change(name)
        else:
            # 没接线（单测直接 new 的场景）：至少别让按钮高亮撒谎
            self._sync_preset_button_highlight()

    def _reset_defaults(self):
        """恢复到传入的 defaults 快照（config.py + config_local 出厂态）。

        不碰 groq.key / cloud.json / node.json：它们不是场景模式的一部分，
        「恢复默认」不能把已经保存的 key 或节点开关清掉。
        """
        # 通过滑块 setValue 触发 valueChanged，自动同步 config 和数值标签
        pairs = [
            (self.chunk_submit_slider, 'CHUNK_SUBMIT_SECONDS'),
            (self.buffer_trim_slider, 'BUFFER_TRIM_SEC'),
            (self.idle_flush_slider, 'IDLE_FLUSH_SEC'),
            (self.speech_threshold_slider, 'ENERGY_THRESHOLD_SPEECH'),
            (self.max_length_slider, 'MAX_SUBTITLE_LENGTH'),
            (self.max_pairs_slider, 'MAX_SENTENCE_PAIRS'),
            (self.font_size_slider, 'FONT_SIZE'),
            (self.bg_opacity_slider, 'BACKGROUND_OPACITY'),
        ]
        for slider_info, key in pairs:
            if key not in self._defaults:
                continue
            slider_info['slider'].setValue(round(self._defaults[key] / slider_info['step']))

        self.device_name_edit.setText(self._defaults.get('LOOPBACK_DEVICE_NAME', '') or '')
        self._on_device_name_changed()

        # checkbox / 颜色 / 字体
        show_bi = self._defaults.get('SHOW_BILINGUAL', True)
        mode = self._defaults.get("DISPLAY_MODE", _display_mode_from_show_bilingual(show_bi))
        if bool(show_bi):
            mode = "bilingual"
        elif _normalize_display_mode(mode) == "bilingual":
            mode = "target"
        _set_display_mode(mode)
        self._sync_display_controls(config.DISPLAY_MODE)

        draft = self._defaults.get('DRAFT_TRANSLATION', True)
        self.draft_cb.setChecked(bool(draft))

        for key, btn in (
            ("SOURCE_TEXT_COLOR", self.source_color_palette["custom"]),
            ("CHINESE_TEXT_COLOR", self.color_btn_chinese),
            ("DRAFT_TEXT_COLOR", self.color_btn_draft),
            ("UNSTABLE_TEXT_COLOR", self.color_btn_unstable),
        ):
            if key in self._defaults:
                self.apply_color(key, self._defaults[key], btn)
        if "BACKGROUND_COLOR" in self._defaults:
            self.apply_color("BACKGROUND_COLOR", self._defaults["BACKGROUND_COLOR"])

        if "TEXT_EDGE" in self._defaults:
            config.TEXT_EDGE = self._defaults["TEXT_EDGE"]
        if "WINDOW_CORNER_RADIUS" in self._defaults:
            self._set_corner_radius(self._defaults["WINDOW_CORNER_RADIUS"])
        self._sync_style_option_buttons()

        if 'TRANSLATION_STYLE' in self._defaults:
            config.TRANSLATION_STYLE = self._defaults['TRANSLATION_STYLE']

        if 'FONT_FAMILY' in self._defaults:
            primary = (self._defaults['FONT_FAMILY'] or "Microsoft YaHei").split(",")[0].strip()
            self.font_combo.blockSignals(True)
            self.font_combo.setCurrentFont(QFont(primary))
            self.font_combo.blockSignals(False)
            config.FONT_FAMILY = self._defaults['FONT_FAMILY']
            if self._on_font_change:
                self._on_font_change()

        if 'AUTO_DETECT_LANGUAGE' in self._defaults:
            self.auto_detect_cb.setChecked(bool(self._defaults['AUTO_DETECT_LANGUAGE']))

        if 'SOURCE_LANGUAGE' in self._defaults:
            src = self._defaults['SOURCE_LANGUAGE']
            tgt = self._defaults.get('TARGET_LANGUAGE')
            if not tgt or (src, tgt) not in language_policy.allowed_pairs():
                tgt = pair_target(src)
            if self.on_language_change:
                self.on_language_change(src, tgt)
            else:
                self._apply_language_locally(src, tgt)
            self._sync_language_buttons()

        print("🔄 参数已恢复默认值")

    def refresh_from_config(self):
        """按当前 config 重刷所有控件（不触发写回回调）。

        游戏模式热键改 config 后面板必须跟上；blockSignals 防止 setValue
        再把值写回 config 或刷屏打印。valueChanged 被挡时数值标签手动同步。
        """
        pairs = [
            (self.chunk_submit_slider, config.CHUNK_SUBMIT_SECONDS),
            (self.buffer_trim_slider, config.BUFFER_TRIM_SEC),
            (self.idle_flush_slider, config.IDLE_FLUSH_SEC),
            (self.speech_threshold_slider, config.ENERGY_THRESHOLD_SPEECH),
            (self.max_length_slider, config.MAX_SUBTITLE_LENGTH),
            (self.max_pairs_slider, config.MAX_SENTENCE_PAIRS),
            (self.font_size_slider, config.FONT_SIZE),
            (self.bg_opacity_slider, config.BACKGROUND_OPACITY),
            (self.cinema_font_slider, config.CINEMA_FONT_SIZE),
            (self.cinema_alpha_slider, config.CINEMA_BG_ALPHA),
        ]
        for info, val in pairs:
            slider = info['slider']
            step = info['step']
            slider.blockSignals(True)
            slider.setValue(round(val / step))
            if info['label'] is not slider:
                info['label'].setText(format_slider_value(val, step))
            slider.blockSignals(False)

        self.device_name_edit.blockSignals(True)
        self.device_name_edit.setText(getattr(config, 'LOOPBACK_DEVICE_NAME', '') or '')
        self.device_name_edit.blockSignals(False)

        mode = _normalize_display_mode(
            getattr(config, "DISPLAY_MODE", None), getattr(config, "SHOW_BILINGUAL", True))
        if bool(getattr(config, "SHOW_BILINGUAL", True)):
            mode = "bilingual"
        elif mode == "bilingual":
            mode = "target"
        _set_display_mode(mode)
        self._sync_display_controls(mode)

        self.draft_cb.blockSignals(True)
        self.draft_cb.setChecked(bool(getattr(config, "DRAFT_TRANSLATION", True)))
        self.draft_cb.blockSignals(False)

        self.cinema_german_cb.blockSignals(True)
        self.cinema_german_cb.setChecked(bool(getattr(config, "CINEMA_SHOW_GERMAN", True)))
        self.cinema_german_cb.blockSignals(False)

        for key, btn in (
            ("SOURCE_TEXT_COLOR", self.source_color_palette["custom"]),
            ("CHINESE_TEXT_COLOR", self.color_btn_chinese),
            ("DRAFT_TEXT_COLOR", self.color_btn_draft),
            ("UNSTABLE_TEXT_COLOR", self.color_btn_unstable),
        ):
            if btn is not None:
                _style_color_button(btn, getattr(config, key, "#ffffff"))
            self._refresh_palette_checks(key)
        self._refresh_palette_checks("BACKGROUND_COLOR")
        self._sync_style_option_buttons()

        primary = (getattr(config, "FONT_FAMILY", "Microsoft YaHei") or "Microsoft YaHei").split(",")[0].strip()
        self.font_combo.blockSignals(True)
        self.font_combo.setCurrentFont(QFont(primary))
        self.font_combo.blockSignals(False)

        self.auto_detect_cb.blockSignals(True)
        self.auto_detect_cb.setChecked(bool(getattr(config, "AUTO_DETECT_LANGUAGE", False)))
        self.auto_detect_cb.blockSignals(False)
        self._sync_language_buttons()
        self._refresh_remote_controls()
