"""
参数调节面板（⚙️）：tuning 持久化辅助函数 + 场景预设常量 + SettingsWindow。
"""
from PyQt5.QtWidgets import (
    QLabel, QWidget, QVBoxLayout, QHBoxLayout, QSlider,
    QPushButton, QGroupBox, QLineEdit, QCheckBox, QColorDialog, QFontComboBox,
)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QFont
import config

from window_frame import DraggableWidget


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
    "DRAFT_TRANSLATION",
    "CHINESE_TEXT_COLOR",
    "DRAFT_TEXT_COLOR",
    "UNSTABLE_TEXT_COLOR",
    "FONT_FAMILY",
)

# 游戏模式热键临时改写，保存 tuning 时需豁免（沿用上次已存值）
_GAME_MODE_TUNING_KEYS = ("CHUNK_SUBMIT_SECONDS", "DRAFT_TRANSLATION")


def apply_tuning(tuning):
    """把 state['tuning'] 写回 config；坏键/类型异常跳过。"""
    if not isinstance(tuning, dict):
        return
    for key in TUNING_KEYS:
        if key not in tuning:
            continue
        try:
            val = tuning[key]
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
        except (TypeError, ValueError, AttributeError):
            continue


def collect_tuning(*, game_mode_active=False, previous_tuning=None):
    """从 config 组装 tuning dict；游戏模式期间豁免热键接管的键。"""
    tuning = {key: getattr(config, key) for key in TUNING_KEYS}
    if game_mode_active:
        prev = previous_tuning if isinstance(previous_tuning, dict) else {}
        for key in _GAME_MODE_TUNING_KEYS:
            if key in prev:
                tuning[key] = prev[key]
            else:
                tuning.pop(key, None)
    return tuning


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
    return normalized


def _style_color_button(button, hex_color):
    """颜色按钮底色显示当前色。"""
    button.setStyleSheet(
        f"background-color: {hex_color}; color: #000; border: 1px solid #666; "
        f"padding: 4px 10px; min-width: 72px;"
    )


def snapshot_defaults():
    """config 出厂态快照（应用 state/tuning 之前调用）。含 FONT_SIZE/不透明度。"""
    defaults = {key: getattr(config, key) for key in TUNING_KEYS}
    defaults["FONT_SIZE"] = config.FONT_SIZE
    defaults["BACKGROUND_OPACITY"] = config.BACKGROUND_OPACITY
    return defaults


# 场景预设按钮：(config.PRESETS 键, 按钮文案)
_PRESET_BUTTONS = (
    ("直播", "📺 直播"),
    ("看剧", "🎬 看剧"),
    ("游戏", "🎮 游戏"),
    ("精听", "🎧 精听"),
)

# 预设键 → SettingsWindow 控件属性名（滑块 dict 或 QCheckBox 等）
# 完整性测试与 apply_preset 共用，防以后加键漏接
PRESET_CONTROL_ATTRS = {
    "CHUNK_SUBMIT_SECONDS": "chunk_submit_slider",
    "IDLE_FLUSH_SEC": "idle_flush_slider",
    "MAX_SENTENCE_PAIRS": "max_pairs_slider",
    "SHOW_BILINGUAL": "chinese_only_cb",  # 勾选 = 只显中文 = 非双语
    "DRAFT_TRANSLATION": "draft_cb",
}

class SettingsWindow(DraggableWidget):
    """参数调节窗口"""

    def __init__(self, on_font_change=None, defaults=None):
        super().__init__()
        self._on_font_change = on_font_change  # 显示相关改动 → 字幕/历史重刷样式+重渲染
        self.setWindowTitle("⚙️ 参数调节（可拖动）")
        self.setWindowFlags(Qt.WindowStaysOnTopHint)
        # 场景预设 + checkbox/颜色/字体后内容变高；高度放宽，小屏仍可拖看全
        self.setGeometry(100, 100, 520, 1060)

        # 真默认快照：由 SubtitleWindow 在应用 tuning 之前拍下并传入；
        # 单测直接 new 时回退到当前 config（等同出厂若未改过）。
        if defaults is not None:
            self._defaults = dict(defaults)
        else:
            self._defaults = snapshot_defaults()

        # 场景预设状态（None=自定义）；游戏模式热键互斥见 set_game_mode
        self._active_preset = None
        self._applying_preset = False
        self._game_mode_active = False
        self._preset_buttons = {}  # name -> QPushButton

        layout = QVBoxLayout()

        # 场景预设（面板最顶部）
        preset_group = QGroupBox("场景预设")
        preset_layout = QHBoxLayout()
        for name, label in _PRESET_BUTTONS:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setToolTip(f"一键应用「{name}」场景参数（仅覆盖节奏/句对/双语/草稿）")
            btn.setStyleSheet(
                "QPushButton { padding: 6px 10px; }"
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
        device_label.setMinimumWidth(120)
        device_label.setToolTip("空=系统默认播放设备；填 FiiO / Speakers 等子串匹配 loopback（约5秒内热切换）")
        self.device_name_edit = QLineEdit()
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

        self.font_size_slider = self._create_slider(
            "字体大小", 14, 36, config.FONT_SIZE, 1, set_font_size
        )

        def set_bg_opacity(v):
            config.BACKGROUND_OPACITY = int(v)
            if self._on_font_change:
                self._on_font_change()  # 同一个回调：重刷字幕样式

        self.bg_opacity_slider = self._create_slider(
            "背景不透明度", 100, 255, config.BACKGROUND_OPACITY, 5, set_bg_opacity
        )

        # 只显中文：勾上 = 隐藏德语原文（SHOW_BILINGUAL=False）
        self.chinese_only_cb = QCheckBox("只显中文（隐藏德语原文）")
        self.chinese_only_cb.setToolTip("勾选后同窗口可多显示约一倍句对")
        self.chinese_only_cb.setChecked(not getattr(config, "SHOW_BILINGUAL", True))
        self.chinese_only_cb.toggled.connect(self._on_chinese_only_toggled)

        # 草稿中文：游戏模式会禁用此开关
        self.draft_cb = QCheckBox("草稿中文（残句先出浅蓝译文）")
        self.draft_cb.setToolTip("翻译 worker 空闲时出草稿；游戏模式会强制关闭")
        self.draft_cb.setChecked(bool(getattr(config, "DRAFT_TRANSLATION", True)))
        self.draft_cb.toggled.connect(self._on_draft_toggled)

        # 三个颜色按钮：正式中文 / 草稿中文 / 未稳定德语
        self.color_btn_chinese = self._make_color_button(
            "正式中文", "CHINESE_TEXT_COLOR",
            getattr(config, "CHINESE_TEXT_COLOR", "#c8c8c8"),
        )
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
        color_layout.addWidget(QLabel("颜色:"))
        color_layout.addWidget(self.color_btn_chinese)
        color_layout.addWidget(self.color_btn_draft)
        color_layout.addWidget(self.color_btn_unstable)
        color_layout.addStretch()
        color_row.setLayout(color_layout)

        # 字体族：config 存 "Family, Arial"，写回时保留 Arial 兜底
        font_row = QWidget()
        font_layout = QHBoxLayout()
        font_layout.setContentsMargins(0, 0, 0, 0)
        font_label = QLabel("字体:")
        font_label.setMinimumWidth(120)
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
        display_layout.addWidget(self.font_size_slider['widget'])
        display_layout.addWidget(self.bg_opacity_slider['widget'])
        display_layout.addWidget(self.chinese_only_cb)
        display_layout.addWidget(self.draft_cb)
        display_layout.addWidget(color_row)
        display_layout.addWidget(font_row)
        display_group.setLayout(display_layout)

        # 添加到主布局
        layout.addWidget(duration_group)
        layout.addWidget(energy_group)
        layout.addWidget(display_group)

        # 重置按钮
        reset_btn = QPushButton("🔄 恢复默认值")
        reset_btn.clicked.connect(self._reset_defaults)
        layout.addWidget(reset_btn)

        layout.addStretch()
        self.setLayout(layout)

    def _create_slider(self, label, min_val, max_val, current_val, step, callback):
        """创建滑块控件"""
        widget = QWidget()
        layout = QHBoxLayout()

        # 标签
        label_widget = QLabel(f"{label}:")
        label_widget.setMinimumWidth(120)

        # 滑块（用round避免浮点截断，如0.01/0.001=9.999...被int截成9）
        slider = QSlider(Qt.Horizontal)
        slider.setMinimum(round(min_val / step))
        slider.setMaximum(round(max_val / step))
        slider.setValue(round(current_val / step))

        # 数值显示
        value_label = QLabel(f"{current_val:.3f}")
        value_label.setMinimumWidth(60)

        # 滑块变化时更新；手动改动会清掉预设高亮（apply_preset 期间跳过）
        def on_change(int_value):
            value = int_value * step
            value_label.setText(f"{value:.3f}")
            self._mark_custom()
            callback(value)
            print(f"📊 {label}: {value:.3f}")

        slider.valueChanged.connect(on_change)

        layout.addWidget(label_widget)
        layout.addWidget(slider)
        layout.addWidget(value_label)
        widget.setLayout(layout)

        return {'widget': widget, 'slider': slider, 'label': value_label, 'step': step}

    def _make_color_button(self, label, config_key, hex_color):
        """颜色选择按钮：底色=当前色，点击弹 QColorDialog。"""
        btn = QPushButton(label)
        btn.setToolTip(f"点击选择{label}颜色")
        _style_color_button(btn, hex_color)
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
                "CHINESE_TEXT_COLOR": getattr(self, "color_btn_chinese", None),
                "DRAFT_TEXT_COLOR": getattr(self, "color_btn_draft", None),
                "UNSTABLE_TEXT_COLOR": getattr(self, "color_btn_unstable", None),
            }
            btn = mapping.get(config_key)
            if btn is not None:
                _style_color_button(btn, normalized)
        if self._on_font_change:
            self._on_font_change()
        print(f"📊 {config_key}: {normalized}")
        return normalized

    def _on_chinese_only_toggled(self, checked):
        """勾上=只显中文 → SHOW_BILINGUAL=False。"""
        self._mark_custom()
        config.SHOW_BILINGUAL = not bool(checked)
        print(f"📊 只显中文: {'开' if checked else '关'} (SHOW_BILINGUAL={config.SHOW_BILINGUAL})")
        if self._on_font_change:
            self._on_font_change()

    def _on_draft_toggled(self, checked):
        self._mark_custom()
        config.DRAFT_TRANSLATION = bool(checked)
        print(f"📊 草稿中文: {'开' if checked else '关'}")

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

    # ------------------------------------------------------------------
    # 场景预设
    # ------------------------------------------------------------------
    def _mark_custom(self):
        """用户手动改控件 → 清预设高亮。apply_preset 期间跳过。"""
        if getattr(self, "_applying_preset", False):
            return
        if self._active_preset is not None:
            self._active_preset = None
            self._sync_preset_button_highlight()

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

    def _on_preset_clicked(self, name):
        """按钮 clicked 会先 toggle checked；无论成功失败都重同步高亮。"""
        self.apply_preset(name)
        self._sync_preset_button_highlight()

    def apply_preset(self, name):
        """经控件 setValue/setChecked 应用预设，走既有回调写 config。

        游戏模式开启时不应用（热键线程状态，面板不反向关闭）。
        成功返回 True，互斥/未知名返回 False。
        """
        if getattr(self, "_game_mode_active", False):
            msg = "先按 Ctrl+Alt+G 关闭游戏模式再切预设"
            print(f"⚠️ {msg}")
            return False
        presets = getattr(config, "PRESETS", {}) or {}
        if name not in presets:
            print(f"⚠️ 未知预设: {name}")
            return False
        params = presets[name]
        self._applying_preset = True
        try:
            for key, val in params.items():
                self._apply_preset_key(key, val)
            self._active_preset = name
            self._sync_preset_button_highlight()
            print(f"🎬 场景预设: {name}")
        finally:
            self._applying_preset = False
        return True

    def _apply_preset_key(self, key, val):
        """单键经对应控件写入（不 setattr 绕过 UI）。"""
        if key == "CHUNK_SUBMIT_SECONDS":
            info = self.chunk_submit_slider
            info["slider"].setValue(round(float(val) / info["step"]))
        elif key == "IDLE_FLUSH_SEC":
            info = self.idle_flush_slider
            info["slider"].setValue(round(float(val) / info["step"]))
        elif key == "MAX_SENTENCE_PAIRS":
            info = self.max_pairs_slider
            info["slider"].setValue(round(float(val) / info["step"]))
        elif key == "SHOW_BILINGUAL":
            # 勾「只显中文」= 非双语
            self.chinese_only_cb.setChecked(not bool(val))
        elif key == "DRAFT_TRANSLATION":
            self.draft_cb.setChecked(bool(val))
        else:
            print(f"⚠️ 预设键无面板控件，已跳过: {key}")

    def _reset_defaults(self):
        """恢复到传入的 defaults 快照（config.py + config_local 出厂态）。"""
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
        self.chinese_only_cb.setChecked(not bool(show_bi))

        draft = self._defaults.get('DRAFT_TRANSLATION', True)
        self.draft_cb.setChecked(bool(draft))

        for key, btn in (
            ("CHINESE_TEXT_COLOR", self.color_btn_chinese),
            ("DRAFT_TEXT_COLOR", self.color_btn_draft),
            ("UNSTABLE_TEXT_COLOR", self.color_btn_unstable),
        ):
            if key in self._defaults:
                self.apply_color(key, self._defaults[key], btn)

        if 'FONT_FAMILY' in self._defaults:
            primary = (self._defaults['FONT_FAMILY'] or "Microsoft YaHei").split(",")[0].strip()
            self.font_combo.blockSignals(True)
            self.font_combo.setCurrentFont(QFont(primary))
            self.font_combo.blockSignals(False)
            config.FONT_FAMILY = self._defaults['FONT_FAMILY']
            if self._on_font_change:
                self._on_font_change()

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
        ]
        for info, val in pairs:
            slider = info['slider']
            step = info['step']
            slider.blockSignals(True)
            slider.setValue(round(val / step))
            info['label'].setText(f"{val:.3f}")
            slider.blockSignals(False)

        self.device_name_edit.blockSignals(True)
        self.device_name_edit.setText(getattr(config, 'LOOPBACK_DEVICE_NAME', '') or '')
        self.device_name_edit.blockSignals(False)

        self.chinese_only_cb.blockSignals(True)
        self.chinese_only_cb.setChecked(not bool(getattr(config, "SHOW_BILINGUAL", True)))
        self.chinese_only_cb.blockSignals(False)

        self.draft_cb.blockSignals(True)
        self.draft_cb.setChecked(bool(getattr(config, "DRAFT_TRANSLATION", True)))
        self.draft_cb.blockSignals(False)

        for key, btn in (
            ("CHINESE_TEXT_COLOR", self.color_btn_chinese),
            ("DRAFT_TEXT_COLOR", self.color_btn_draft),
            ("UNSTABLE_TEXT_COLOR", self.color_btn_unstable),
        ):
            _style_color_button(btn, getattr(config, key, "#ffffff"))

        primary = (getattr(config, "FONT_FAMILY", "Microsoft YaHei") or "Microsoft YaHei").split(",")[0].strip()
        self.font_combo.blockSignals(True)
        self.font_combo.setCurrentFont(QFont(primary))
        self.font_combo.blockSignals(False)

    def set_game_mode(self, active: bool):
        """游戏模式接管提交节奏 + 草稿中文：禁用控件，避免和热键互相踩。"""
        self._game_mode_active = bool(active)
        slider = self.chunk_submit_slider['slider']
        if active:
            slider.setEnabled(False)
            slider.setToolTip("游戏模式接管中(Ctrl+Alt+G)")
            self.draft_cb.setEnabled(False)
            self.draft_cb.setToolTip("游戏模式接管中(Ctrl+Alt+G)")
        else:
            slider.setEnabled(True)
            slider.setToolTip("")
            self.draft_cb.setEnabled(True)
            self.draft_cb.setToolTip("翻译 worker 空闲时出草稿；游戏模式会强制关闭")

