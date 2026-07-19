"""
把ASR/翻译事件变成屏幕上的富文本：线程安全对外接口 + 渲染槽函数 +
HTML/QTextDocument构建 + 点词查词命中测试。以mixin形式并入 SubtitleWindow。
"""
import html
from PyQt5.QtCore import Qt
import config


class LiveTextRenderMixin:

    def update_live(self, committed, unstable):
        """更新live德语行：committed=已提交未翻译（白），unstable=未稳定尾部（灰）"""
        self.signals.live.emit(committed or "", unstable or "")

    def add_pair(self, german, chinese):
        """一段德语翻译完成，加入历史句对"""
        self.signals.pair.emit(german or "", chinese or "")

    def update_draft(self, chinese):
        """live德语的草稿中文（线程安全）。正式句对完成后自动清掉"""
        self.signals.draft.emit(chinese or "")

    def update_subtitle(self, text):
        """旧接口：当一条完成句对显示（兼容保留）"""
        self.signals.update.emit(text)

    def show_status(self, text):
        """显示一条状态提示（线程安全）。不进句对历史，下次内容更新时自然消失"""
        self.signals.status.emit(text)

    # ------------------------------------------------------------------
    # 主线程槽函数
    # ------------------------------------------------------------------
    def _update_live(self, committed, unstable):
        if (committed == self.live_committed and unstable == self.live_unstable
                and not self.status_line):
            return  # 内容没变不重排——识别端每0.5秒来一次"无新提交"的空转
        self.live_committed = committed
        self.live_unstable = unstable
        if not committed:
            self.live_draft = ""  # live德语没了，草稿跟着失效
        self.status_line = ""
        self._status_clear_timer.stop()
        self._render()

    def _update_draft(self, chinese):
        self.live_draft = chinese
        self.tv_window.update_draft(chinese)
        self._render()

    def _add_pair(self, german, chinese):
        self.sentence_pairs.append((german, chinese))
        while len(self.sentence_pairs) > self.HISTORY_KEEP:
            self.sentence_pairs.pop(0)
        self.live_draft = ""  # 正式翻译到了，草稿退场
        self.status_line = ""
        self._status_clear_timer.stop()
        self.history_window.append_pair(german, chinese)
        self.tv_window.append_pair(chinese)
        self._render()
        if config.SHOW_PERFORMANCE:
            print(f"💬 字幕: {chinese[:50]}{'...' if len(chinese) > 50 else ''}")

    def _update_text(self, text):
        """旧接口的槽：当作一条无原文的句对处理"""
        if text and text.strip():
            self._add_pair("", text)

    def _show_status(self, text):
        """状态提示：追加在当前内容底部；5 秒后自动清空（新 status 重置计时）。"""
        self.status_line = text
        self._render()
        self._status_clear_timer.start(5000)

    def _clear_status(self):
        """status 超时回调：置空并重绘（避免安静段提示挂几分钟）。

        _render 在「无句对/无 live」时会 early-return 保底不闪空白；
        若上次屏上只有 status，这里主动摘掉，避免 status_line 空了字还挂着。
        """
        if not self.status_line:
            return
        cleared = self.status_line
        self.status_line = ""
        self._render()
        esc = html.escape(cleared)
        if not esc or not self._last_html or esc not in self._last_html:
            return  # _render 已用新内容覆盖
        parts = [p for p in self._last_html.split("<br>") if p != esc]
        if parts:
            self._last_html = "<br>".join(parts)
            self.window.setText(self._last_html)
        else:
            self._last_html = ""
            self.window.setText("🎬 等待音频输入...")

    @staticmethod
    def _clip(text):
        if len(text) > config.MAX_SUBTITLE_LENGTH:
            return text[:config.MAX_SUBTITLE_LENGTH] + "..."
        return text

    def _pair_html(self, german, chinese):
        """一条已完成句对的富文本块"""
        lines = []
        if german and getattr(config, "SHOW_BILINGUAL", True):
            lines.append(html.escape(self._clip(german)))
        if chinese:
            color = getattr(config, "CHINESE_TEXT_COLOR", "#c8c8c8")
            lines.append(f'<span style="color:{color}">{html.escape(self._clip(chinese))}</span>')
        return "<br>".join(lines)

    def _live_block_html(self):
        """live行的富文本块：德语（白+灰色未稳定尾部）+ 草稿中文"""
        live_parts = []
        if self.live_committed:
            live_parts.append(html.escape(self._clip(self.live_committed)))
        if self.live_unstable:
            color = getattr(config, "UNSTABLE_TEXT_COLOR", "#999999")
            live_parts.append(f'<span style="color:{color}"><i>{html.escape(self._clip(self.live_unstable))}</i></span>')
        live_block = " ".join(live_parts)
        if self.live_draft:
            # 草稿中文：正式翻译还没到之前先给一版（浅蓝斜体和正式中文区分）
            draft_color = getattr(config, "DRAFT_TEXT_COLOR", "#8fb8e0")
            live_block += ('<br>' if live_block else '') + \
                f'<span style="color:{draft_color}"><i>{html.escape(self._clip(self.live_draft))}</i></span>'
        return live_block

    def _build_doc(self, html_str=""):
        """按 QLabel 相同的字体/宽度新建 QTextDocument。

        点词命中测试必须用独立文档（勿共享 _doc_cache：命中中途 _render
        可能 setHtml 改掉内容）。排版参数（margin 0 / width-46 / pixelSize）
        与瀑布填充、点词坐标强绑定，勿改。
        """
        from PyQt5.QtGui import QTextDocument, QFont
        doc = QTextDocument()
        doc.setDocumentMargin(0)
        font = QFont(config.FONT_FAMILY.split(",")[0].strip())
        font.setPixelSize(config.FONT_SIZE)
        doc.setDefaultFont(font)
        doc.setTextWidth(max(100, self.window.width() - 46))
        if html_str:
            doc.setHtml(html_str)
        return doc

    def _doc_for_render(self):
        """_render 测高用：font/size/width 未变则复用同一 QTextDocument。"""
        from PyQt5.QtGui import QTextDocument, QFont
        family = config.FONT_FAMILY.split(",")[0].strip()
        size = config.FONT_SIZE
        text_width = max(100, self.window.width() - 46)
        key = (family, size, text_width)
        if self._doc_cache is not None and self._doc_cache_key == key:
            return self._doc_cache
        doc = QTextDocument()
        doc.setDocumentMargin(0)
        font = QFont(family)
        font.setPixelSize(size)
        doc.setDefaultFont(font)
        doc.setTextWidth(text_width)
        self._doc_cache = doc
        self._doc_cache_key = key
        return doc

    def _render(self):
        """把句对历史+live行渲染成富文本。

        显示几条句对由真实排版高度决定：用 QTextDocument 按 QLabel 相同的
        字体/宽度排版测高，从最新往旧塞，塞到窗口装不下为止——窗口拉多大
        就填多满（瀑布式），不再靠折行估算（之前估得保守，顶部留大片空黑）。
        """
        live_block = self._live_block_html()
        status = html.escape(self.status_line) if self.status_line else ""
        fixed = [b for b in (live_block, status) if b]

        # 和 QLabel 一致的排版环境（padding 15px 20px + 2px边框）；缓存复用
        doc = self._doc_for_render()
        avail_h = self.window.height() - 40

        pair_blocks = [b for b in (self._pair_html(g, c) for g, c in self.sentence_pairs) if b]
        cap = min(len(pair_blocks), getattr(config, "MAX_SENTENCE_PAIRS", 20))

        def fits(count):
            doc.setHtml("<br>".join(pair_blocks[len(pair_blocks) - count:] + fixed))
            return doc.size().height() <= avail_h

        # 二分找能装下的最多句对数（fits随count单调递减；每次渲染最多
        # ~5次排版测量，之前线性试装最多20次，UI线程上省一截）。
        # 最新一条保底：窗口再小也至少显示1条
        shown = 0
        if cap:
            shown = 1
            lo, hi = 2, cap
            while lo <= hi:
                mid = (lo + hi) // 2
                if fits(mid):
                    shown = mid
                    lo = mid + 1
                else:
                    hi = mid - 1

        blocks = (pair_blocks[len(pair_blocks) - shown:] if shown else []) + fixed
        if not blocks:
            return  # 什么都没有就保持现状（避免闪空白）

        self._last_html = "<br>".join(blocks)
        self.window.setText(self._last_html)
        self.window.show()

    # ------------------------------------------------------------------
    # 点词查词
    # ------------------------------------------------------------------
    def _on_label_click(self, pos):
        """容器上的原地单击：命中字幕文字里的德语词就发起词典查询。

        QLabel 的富文本没有词级点击API，这里用和渲染完全相同的
        QTextDocument 重建排版，documentLayout().hitTest 找到字符位置，
        再取词。注意 QLabel 是 AlignBottom：文档从内容区底部往上排。
        """
        if not self.on_lookup or not self._last_html:
            return
        lp = pos - self.window.pos()  # 容器坐标 → 字幕标签坐标
        if not self.window.rect().contains(lp):
            return

        from PyQt5.QtCore import QPointF
        from PyQt5.QtGui import QTextCursor
        doc = self._build_doc(self._last_html)
        # 内容区 = 标签减 padding(15px 20px) 和 2px 边框
        content_x0, content_y0 = 22, 17
        content_h = self.window.height() - 34
        doc_y0 = content_y0 + max(0, content_h - doc.size().height())  # AlignBottom
        hit = doc.documentLayout().hitTest(
            QPointF(lp.x() - content_x0, lp.y() - doc_y0), Qt.ExactHit)
        if hit < 0:
            return  # 点在空白处

        cursor = QTextCursor(doc)
        cursor.setPosition(hit)
        cursor.select(QTextCursor.WordUnderCursor)
        word = cursor.selectedText().strip(".,!?…:;\"'«»()")
        # 只查拉丁字母词（德语/英语）；点到中文/数字/空白不弹窗
        if len(word) < 2 or not all(c.isalpha() and ord(c) < 0x2E80 for c in word):
            return
        context = cursor.block().text()  # 该词所在行做上下文

        from PyQt5.QtGui import QCursor
        self._lookup_anchor = QCursor.pos()
        self.word_popup.show_at(self._lookup_anchor,
                                f"🔍 <b>{html.escape(word)}</b> 查询中…", 8000)
        self.on_lookup(word, context)

    def show_lookup_result(self, word, text):
        """词典查询完成（线程安全，从翻译线程调）"""
        self.signals.lookup.emit(word or "", text or "")

    def _show_lookup(self, word, text):
        body = html.escape(text).replace("\n", "<br>")
        self.word_popup.show_at(
            getattr(self, "_lookup_anchor", self.container.pos()),
            f"📖 <b>{html.escape(word)}</b><br>{body}")
