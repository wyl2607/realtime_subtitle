"""
把ASR/翻译事件变成屏幕上的富文本：线程安全对外接口 + 渲染槽函数 +
HTML/QTextDocument构建 + 点词查词命中测试。以mixin形式并入 SubtitleWindow。
"""
import html
import time
import uuid
from PyQt6.QtCore import Qt
import realtime_subtitle.config as config
import realtime_subtitle.ui.design_tokens as design_tokens


def _hex_to_rgb(color):
    body = (color or "").strip()
    if body.startswith("#"):
        body = body[1:]
    if len(body) == 3:
        body = "".join(c * 2 for c in body)
    if len(body) != 6:
        return None
    try:
        return tuple(int(body[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None


def _dim_color(color, factor=design_tokens.OPACITY_OLDER_PAIR):
    rgb = _hex_to_rgb(color)
    if rgb is None:
        return color
    return "#" + "".join(f"{round(channel * factor):02x}" for channel in rgb)


def _display_mode():
    mode = str(getattr(config, "DISPLAY_MODE", "") or "").lower()
    if mode not in {"bilingual", "target", "source"}:
        return "bilingual" if getattr(config, "SHOW_BILINGUAL", True) else "target"
    if getattr(config, "SHOW_BILINGUAL", True):
        return "bilingual"
    return "target" if mode == "bilingual" else mode


class LiveTextRenderMixin:

    def update_live(self, committed, unstable):
        """更新live德语行：committed=已提交未翻译（白），unstable=未稳定尾部（灰）"""
        self.signals.live.emit(committed or "", unstable or "")

    def add_pair(self, german, chinese, t0=None, t1=None, key=None, revision=0):
        """一段句对翻译完成，加入历史。

        兼容旧的 add_pair(german, chinese, revision)；新路径带 t0/t1/key。
        """
        if t1 is None and key is None and isinstance(t0, int):
            revision = t0
            t0 = None
        meta = {"revision": int(revision), "t0": t0, "t1": t1,
                "key": key, "source": "local"}
        self.signals.pair.emit(german or "", chinese or "", meta)

    def update_draft(self, chinese, revision=0):
        """live 行的草稿译文（线程安全）。正式句对完成后自动清掉"""
        self.signals.draft.emit(chinese or "", int(revision))

    def on_translate_failed(self, source_text, t0, t1, key, revision, reason):
        """翻译失败或被丢弃（线程安全，从后台/worker线程调用）"""
        self.signals.translate_failed.emit(
            source_text or "", t0, t1, key or "", int(revision or 0), str(reason or "")
        )

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

    def _update_draft(self, chinese, revision=0):
        if self._is_stale_language(revision):
            return
        self.live_draft = chinese
        self.tv_window.update_draft(chinese)
        self.cinema_bar.update_draft(chinese)
        self._render()

    def _on_translate_failed(self, source_text, t0, t1, key, revision, reason):
        if self._is_stale_language(revision):
            return
        untranslated = design_tokens.TEXT_UNTRANSLATED
        found = False
        for index, item in enumerate(self.sentence_pairs):
            if len(item) >= 7 and item[6] == key:
                self.sentence_pairs[index] = (
                    item[0] or source_text, untranslated, item[2], item[3], item[4], item[5], item[6]
                )
                found = True
                break
        if not found:
            meta = {"revision": revision, "t0": t0, "t1": t1, "key": key, "source": "local"}
            self._add_pair(source_text, untranslated, meta)
        else:
            self._render()
            self.history_window.update_dst(key, untranslated, german=source_text)
            self.tv_window.update_dst(key, untranslated, german=source_text)
            self.cinema_bar.update_dst(key, untranslated, german=source_text)

    def update_dst(self, key, dst, german=None):
        """按 key 原地更新译文（主窗、历史窗、电视窗、影院条一致）"""
        if not key:
            return
        changed = False
        actual_german = german
        for index, item in enumerate(self.sentence_pairs):
            if len(item) >= 7 and item[6] == key:
                if not actual_german:
                    actual_german = item[0]
                self.sentence_pairs[index] = (
                    item[0], dst, item[2], item[3], item[4], item[5], item[6]
                )
                changed = True
                break
        if changed:
            self._render()
            self.history_window.update_dst(key, dst, german=actual_german)
            self.tv_window.update_dst(key, dst, german=actual_german)
            self.cinema_bar.update_dst(key, dst, german=actual_german)

    def _is_stale_language(self, revision):
        """这条事件是切换语言**之前**发出的吗（审核 O04）。

        ☠️ 判据必须在这里——最后消费的地方——而不是发出前再查一次代数：
        翻译 worker 查完代数到真正回调之间锁是放开的，中间插进一次切换，
        旧语言的句对就会落在新语言的画面上。事件自带代数，这里比一次即可。
        """
        current = getattr(self, "language_revision", 0)
        return int(revision or 0) < current

    @staticmethod
    def _pair_meta(meta):
        if isinstance(meta, dict):
            return (
                int(meta.get("revision") or 0),
                meta.get("t0"),
                meta.get("t1"),
                meta.get("source") or "local",
                meta.get("key"),
            )
        return int(meta or 0), None, None, "local", None

    def _add_pair(self, german, chinese, meta=0):
        revision, t0, t1, source, key = self._pair_meta(meta)
        if self._is_stale_language(revision):
            if config.SHOW_PERFORMANCE:
                print(f"   🌐 丢弃切换语言前的句对（代数 {revision}）")
            return
        if key:
            for index, item in enumerate(self.sentence_pairs):
                if len(item) >= 7 and item[6] == key:
                    self.update_dst(key, chinese, german=german)
                    return
        self.sentence_pairs.append((german, chinese, time.time(), t0, t1, source, key))
        while len(self.sentence_pairs) > self.HISTORY_KEEP:
            self.sentence_pairs.pop(0)
        self.live_draft = ""  # 正式翻译到了，草稿退场
        self.status_line = ""
        self._status_clear_timer.stop()
        self.history_window.append_pair(german, chinese, key=key)
        self.tv_window.append_pair(chinese, key=key, german=german)
        self.cinema_bar.append_pair(german, chinese, key=key)
        self._render()
        if config.SHOW_PERFORMANCE:
            print(f"💬 字幕: {chinese[:50]}{'...' if len(chinese) > 50 else ''}")

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

    def _pair_html(self, german, chinese, *, dim=False, top_gap=False, key=None):
        """一条已完成句对的富文本块"""
        lines = []
        opacity = design_tokens.OPACITY_OLDER_PAIR if dim else 1.0
        mode = _display_mode()
        source_size = round(config.FONT_SIZE * design_tokens.TYPE_SOURCE_SCALE)
        source_color = getattr(config, "SOURCE_TEXT_COLOR",
                               getattr(config, "TEXT_COLOR", design_tokens.COLOR_SUBTITLE_SOURCE))
        is_untranslated = (chinese in (design_tokens.TEXT_UNTRANSLATED, "翻译中…"))
        if is_untranslated:
            target_color = design_tokens.COLOR_UNTRANSLATED
        else:
            target_color = getattr(config, "CHINESE_TEXT_COLOR", design_tokens.COLOR_SUBTITLE_TRANSLATION)
        if dim:
            source_color = _dim_color(source_color, opacity)
            target_color = _dim_color(target_color, opacity)
        if german and (mode in {"bilingual", "source"} or is_untranslated or not chinese):
            lines.append(
                f'<span style="color:{source_color};font-size:{source_size}px">'
                f'{html.escape(self._clip(german))}</span>'
            )
        if chinese and (mode in {"bilingual", "target"} or is_untranslated):
            if chinese == design_tokens.TEXT_UNTRANSLATED and key:
                display_zh = (
                    f'<a href="retry:{html.escape(str(key))}" '
                    f'style="color:{target_color};text-decoration:none;">'
                    f'{html.escape(chinese)}</a>'
                )
            else:
                display_zh = html.escape(self._clip(chinese))
            lines.append(
                f'<span style="color:{target_color};font-size:{config.FONT_SIZE}px">'
                f'{display_zh}</span>'
            )
        if not lines:
            return ""
        gap = "margin-top:0.45em;" if top_gap else ""
        opacity_style = f"opacity:{opacity:.2f};" if dim else ""
        return f'<div style="{gap}{opacity_style}">' + "<br>".join(lines) + "</div>"

    @staticmethod
    def _join_render_blocks(pair_blocks, fixed_blocks):
        parts = []
        if pair_blocks:
            parts.append("".join(pair_blocks))
        if fixed_blocks:
            if parts:
                parts.append("<br>")
            parts.append("<br>".join(fixed_blocks))
        return "".join(parts)

    def _live_block_html(self):
        """live行的富文本块：德语（白+灰色未稳定尾部）+ 草稿中文。

        勾了「只显中文（隐藏德语原文）」时 live 行的德语也要藏——之前只有正式
        句对遵守这个开关，说话过程中德语照样往外冒，和复选框文案直接矛盾。
        藏掉之后这段时间显示的是草稿中文，不会变成空白。
        """
        mode = _display_mode()
        show_source = mode in {"bilingual", "source"}
        show_target = mode in {"bilingual", "target"}
        live_parts = []
        if show_source and self.live_committed:
            live_parts.append(html.escape(self._clip(self.live_committed)))
        if show_source and self.live_unstable:
            color = getattr(config, "UNSTABLE_TEXT_COLOR", "#999999")
            live_parts.append(f'<span style="color:{color}"><i>{html.escape(self._clip(self.live_unstable))}</i></span>')
        live_block = " ".join(live_parts)
        if show_target and self.live_draft:
            # 草稿中文：正式翻译还没到之前先给一版（浅蓝斜体和正式中文区分）
            draft_color = getattr(config, "DRAFT_TEXT_COLOR", design_tokens.COLOR_SUBTITLE_DRAFT)
            live_block += ('<br>' if live_block else '') + \
                f'<span style="color:{draft_color}"><i>{html.escape(self._clip(self.live_draft))}</i></span>'
        return live_block

    def _build_doc(self, html_str=""):
        """按 QLabel 相同的字体/宽度新建 QTextDocument。

        点词命中测试必须用独立文档（勿共享 _doc_cache：命中中途 _render
        可能 setHtml 改掉内容）。排版参数（margin 0 / width-46 / pixelSize）
        与瀑布填充、点词坐标强绑定，勿改。
        """
        from PyQt6.QtGui import QTextDocument, QFont
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
        from PyQt6.QtGui import QTextDocument, QFont
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

        # 从最新往回只构造到 MAX_SENTENCE_PAIRS 条为止。sentence_pairs 内存里
        # 留着 HISTORY_KEEP(50) 条，而上屏的最多 MAX_SENTENCE_PAIRS 条（「⚡性能」
        # 模式只有 4 条）——以前是把 50 条全部 escape+_clip+拼接一遍再切，
        # 其中绝大多数永远不会被显示。这段正好落在草稿流式那条 0.15 秒一次的
        # 热路径上（on_partial → _update_draft → _render）。
        # 结果与"全构造再取末尾 MAX 条非空"完全等价：这里同样跳过空块继续往回找
        keep = max(1, int(getattr(config, "MAX_SENTENCE_PAIRS", 20)))
        pair_items = []
        for item in reversed(self.sentence_pairs):
            g, c = item[0], item[1]
            k = item[6] if len(item) >= 7 else None
            block = self._pair_html(g, c, key=k)
            if not block:
                continue
            pair_items.append((g, c, k))
            if len(pair_items) >= keep:
                break
        pair_items.reverse()
        cap = len(pair_items)

        def pair_blocks_for(count):
            selected = pair_items[len(pair_items) - count:]
            return [
                self._pair_html(
                    g, c,
                    dim=(idx < len(selected) - 1),
                    top_gap=(idx > 0),
                    key=k,
                )
                for idx, (g, c, k) in enumerate(selected)
            ]

        def fits(count):
            doc.setHtml(self._join_render_blocks(pair_blocks_for(count), fixed))
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

        pair_blocks = pair_blocks_for(shown) if shown else []
        if not pair_blocks and not fixed:
            return  # 什么都没有就保持现状（避免闪空白）

        self._last_html = self._join_render_blocks(pair_blocks, fixed)
        self.window.setText(self._last_html)
        self.window.show()

    def _handle_retry_click(self, key):
        """点击「未翻译 ⟳」触发重试"""
        retry_fn = getattr(self, "on_retry_translation", None)
        success = False
        if retry_fn is not None:
            try:
                success = bool(retry_fn(key))
            except Exception as e:
                print(f"⚠️ retry_translation 出错: {e}")
                success = False
        if success:
            self.update_dst(key, "翻译中…")
        else:
            self.show_status("这句已过期，无法重试")

    # ------------------------------------------------------------------
    # 点词查词与未翻译重试
    # ------------------------------------------------------------------
    def _on_label_click(self, pos):
        """容器上的原地单击：命中未翻译链接触发重试；命中德语词发起词典查询。

        QLabel 的富文本没有词级点击API，这里用和渲染完全相同的
        QTextDocument 重建排版，documentLayout().hitTest 找到字符位置，
        再取词。注意 QLabel 是 AlignBottom：文档从内容区底部往上排。
        """
        if not self._last_html:
            return
        lp = pos - self.window.pos()  # 容器坐标 → 字幕标签坐标
        if not self.window.rect().contains(lp):
            return

        from PyQt6.QtCore import QPointF
        from PyQt6.QtGui import QTextCursor
        doc = self._build_doc(self._last_html)
        # 内容区 = 标签减 padding(15px 20px) 和 2px 边框
        content_x0, content_y0 = 22, 17
        content_h = self.window.height() - 34
        doc_y0 = content_y0 + max(0, content_h - doc.size().height())  # AlignBottom
        doc_pt = QPointF(lp.x() - content_x0, lp.y() - doc_y0)
        hit = doc.documentLayout().hitTest(doc_pt, Qt.HitTestAccuracy.ExactHit)
        if hit < 0:
            return  # 点在空白处

        anchor = doc.documentLayout().anchorAt(doc_pt)
        if not anchor:
            cursor = QTextCursor(doc)
            cursor.setPosition(hit)
            anchor = cursor.charFormat().anchorHref()
        if anchor and anchor.startswith("retry:"):
            key = anchor[len("retry:"):]
            self._handle_retry_click(key)
            return

        if not self.on_lookup:
            return

        cursor = QTextCursor(doc)
        cursor.setPosition(hit)
        cursor.select(QTextCursor.SelectionType.WordUnderCursor)
        word = cursor.selectedText().strip(".,!?…:;\"'«»()")
        # 只查拉丁字母词（德语/英语）；点到中文/数字/空白不弹窗
        if len(word) < 2 or not all(c.isalpha() and ord(c) < 0x2E80 for c in word):
            return
        context = cursor.block().text()  # 该词所在行做上下文

        from PyQt6.QtGui import QCursor
        self._lookup_anchor = QCursor.pos()
        self._lookup_context = context  # 深度解释用整句，不靠弹窗事后再猜
        # ☠️ 一次点击一个 request_id：translator 的 seq 门控挡不住
        # "worker 判定没过期 → emit 进 Qt 队列 → 用户点了同一个词的新句境 →
        # 旧 emit 才被主线程执行"。只比词名会把旧释义安到新句上。
        request_id = uuid.uuid4().hex
        self._lookup_pending_word = word
        self._lookup_pending_id = request_id
        # 点新词会使进行中的深度解释过时（共享 WordPopup，旧结果回来别盖查词）
        self._deep_explain_seq = getattr(self, "_deep_explain_seq", 0) + 1
        # HTTP 超时 15s；弹窗要比它多留一点余量，避免 8-15s 区间先消失再闪回
        self.word_popup.show_at(
            self._lookup_anchor,
            f"🔍 <b>{html.escape(word)}</b> 查询中…",
            16000,
            show_deep=False,
            show_web=False,
        )
        try:
            self.on_lookup(word, context, request_id)
        except TypeError:
            self.on_lookup(word, context)

    def show_lookup_result(self, word, text, request_id=None):
        """词典查询完成（线程安全，从查词线程调）"""
        self.signals.lookup.emit(word or "", text or "", False, request_id or "")

    def show_lookup_partial(self, word, text, request_id=None):
        """流式生成中的半成品（线程安全）：已经成行的部分先上屏"""
        self.signals.lookup.emit(word or "", text or "", True, request_id or "")

    def _show_lookup(self, word, text, partial=False, request_id=""):
        pending_id = getattr(self, "_lookup_pending_id", None)
        if pending_id is not None and request_id and request_id != pending_id:
            return  # 排在 Qt 队列里的旧请求，别盖上去
        pending = getattr(self, "_lookup_pending_word", None)
        if pending is not None and word and word != pending:
            return  # 排在 Qt 队列里的旧词结果，用户已经点别的了，别盖上去
        body = html.escape(text).replace("\n", "<br>")
        context = getattr(self, "_lookup_context", "") or ""
        if partial:
            # 中间态：不给按钮（还没查完，点「深度解释」会和在飞的查词抢
            # Ollama），末尾一个省略号提示还在写。仍走 show_at 而不是
            # update_content——弹窗是"锚在鼠标上方向上长"的，只 adjustSize
            # 不重新定位的话每多一行就往下压一点，最后定稿时再跳回去
            self.word_popup.show_at(
                getattr(self, "_lookup_anchor", self.container.pos()),
                f"📖 <b>{html.escape(word)}</b><br>{body}…",
                16000,
                show_deep=False,
                show_web=False,
                context=context,
            )
            return
        # 查词完成后露出「深度解释」；web 按钮等深度解释结果再出
        self.word_popup.show_at(
            getattr(self, "_lookup_anchor", self.container.pos()),
            f"📖 <b>{html.escape(word)}</b><br>{body}",
            show_deep=bool(context.strip()),
            show_web=False,
            context=context,
        )
