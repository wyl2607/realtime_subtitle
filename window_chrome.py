"""
悬浮窗"chrome"：hover显隐的拖动条/按钮工具条淡入淡出、容器resize重排、
Ctrl+滚轮调字号、样式表刷新。以mixin形式并入 SubtitleWindow。
"""
from PyQt5.QtCore import Qt
import config

from window_frame import ResizableFramelessWidget


class WindowChromeMixin:

    def _position_chrome(self):
        """绝对定位标题条 + 右上角按钮 + 穿透指示器（不进布局）。"""
        w = self.container.width()
        h = ResizableFramelessWidget.DRAG_BAR_HEIGHT
        self.drag_bar.setGeometry(0, 0, w, h)
        self.btn_bar.adjustSize()
        self.btn_bar.move(w - self.btn_bar.width() - 10, max(0, (h - self.btn_bar.height()) // 2))
        self.ct_indicator.adjustSize()
        # 右上角常驻；穿透时 chrome 已藏，不会和 drag_bar/btn_bar 重叠
        self.ct_indicator.move(w - self.ct_indicator.width() - 8, 6)
        if self.drag_bar.isVisible():
            self.drag_bar.raise_()
        if self.btn_bar.isVisible():
            self.btn_bar.raise_()
        if self.ct_indicator.isVisible():
            self.ct_indicator.raise_()

    def _position_btn_bar(self):
        """兼容旧调用点：与 _position_chrome 等价"""
        self._position_chrome()

    def _set_controls_visible(self, visible):
        """drag_bar / btn_bar 淡入淡出。ct_indicator 不走这里（穿透可靠性优先）。

        HTCAPTION 门控读 drag_bar.isVisible()：淡出过程中 bar 仍 visible 可拖；
        动画结束后才 setVisible(False) 关门控。淡入前先 setVisible(True)。
        """
        # 穿透时窗口收不到鼠标，hover 失效；chrome 强制隐藏，只留穿透指示器
        if getattr(self, "_click_through", False):
            visible = False
        self._chrome_want_visible = bool(visible)
        if visible:
            # 淡入前先露出（opacity 可能仍是上次的中间值，从当前值继续）
            self.drag_bar.setVisible(True)
            self.btn_bar.setVisible(True)
            self.drag_bar.raise_()
            self.btn_bar.raise_()
            self._animate_chrome(1.0, 150)
        else:
            self._animate_chrome(0.0, 250)

    def _animate_chrome(self, target_opacity, duration_ms):
        """停掉旧动画，从当前 opacity 播到目标（对象复用，不 new）。"""
        pairs = (
            (self._drag_fade, self._drag_opacity),
            (self._btn_fade, self._btn_opacity),
        )
        for anim, effect in pairs:
            anim.stop()
            anim.setStartValue(effect.opacity())
            anim.setEndValue(target_opacity)
            anim.setDuration(duration_ms)
            anim.start()

    def _on_chrome_fade_finished(self):
        """淡出结束才真正隐藏，避免透明控件仍 isVisible 吃掉 HTCAPTION 门控。"""
        if self._chrome_want_visible:
            return
        self.drag_bar.setVisible(False)
        self.btn_bar.setVisible(False)

    def _on_container_resize(self):
        self.window.setGeometry(self.container.rect())  # 标签手动铺满（无布局）
        self._position_chrome()
        self._render()

    def _on_wheel(self, event):
        """Ctrl+滚轮调字号（高频操作，不该每次都开设置面板拖滑块）"""
        if not event.modifiers() & Qt.ControlModifier:
            return False
        step = 1 if event.angleDelta().y() > 0 else -1
        size = max(14, min(72, config.FONT_SIZE + step))
        if size != config.FONT_SIZE:
            # 通过设置面板滑块走，数值标签/config/样式一次同步
            s = self.settings_window.font_size_slider
            s['slider'].setValue(round(size / s['step']))
            self._render()
            self.show_status(f"🔠 字号 {size}（Ctrl+滚轮调节）")
        return True

    # ------------------------------------------------------------------
    # 样式与布局持久化
    # ------------------------------------------------------------------
    def _on_display_settings_change(self):
        """设置面板显示相关改动：刷样式 + 重渲染（颜色/双语/字体都落到 HTML）。"""
        self._apply_styles()
        self._render()

    def _apply_styles(self):
        """按当前config刷新字幕标签/历史窗口的样式（字号滑块实时调用）"""
        self.window.setStyleSheet(f"""
            QLabel {{
                background-color: rgba(0, 0, 0, {int(config.BACKGROUND_OPACITY)});
                color: {config.TEXT_COLOR};
                font-size: {config.FONT_SIZE}px;
                font-family: {config.FONT_FAMILY};
                padding: {config.PADDING};
                border-radius: {config.BORDER_RADIUS}px;
                border: 1px solid {config.BORDER_COLOR};
            }}
        """)
        if hasattr(self, "history_window"):
            self.history_window.text.setStyleSheet(f"""
                QTextEdit {{
                    background-color: rgb(20, 20, 20);
                    color: white;
                    font-size: {config.FONT_SIZE - 4}px;
                    font-family: {config.FONT_FAMILY};
                    border: none;
                    padding: 8px;
                }}
            """)
