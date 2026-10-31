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


def _rgba_color(color, alpha):
    """把颜色换成真正的 rgba（淡入用）：alpha=0 完全透明，不是变黑。

    默认背景是黑的，乘 RGB 也看不出区别；但用户能把背景改成浅色，那时乘
    RGB 会先闪一块深色再亮起来，而 rgba 在任何底色上都对。
    """
    rgb = _hex_to_rgb(color)
    if rgb is None:
        return color
    a = max(0, min(255, round(alpha * 255)))
    return f"rgba({rgb[0]},{rgb[1]},{rgb[2]},{a})"


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

    def _pair_html(self, german, chinese, *, dim=False, top_gap=False, key=None, alpha=None):
        """一条已完成句对的富文本块。

        alpha 只用于新句淡入：把颜色换成 rgba（alpha 从 0 到 1），不改 CSS
        opacity——后者会把整块一起压暗，和 dim 叠起来看不对。
        """
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
        if alpha is not None and alpha < 1.0:
            source_color = _rgba_color(source_color, alpha)
            target_color = _rgba_color(target_color, alpha)
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

    # ------------------------------------------------------------------
    # 出现体验：粘住可见集合 / 最短显示时长 / 新句淡入
    # ------------------------------------------------------------------
    def _now(self):
        """单调时钟秒数（minHold 一律走它，测试/探针可替换成虚拟时钟）。"""
        return time.monotonic()

    @staticmethod
    def _pair_key(item):
        """句对的稳定标识：优先用业务 key，没有就退回首次加入时的墙钟。

        update_dst 会整个替换 tuple，所以 id(item) 不稳定；item[2] 是 append
        时写下的墙钟，替换时保持不变，拿它当无 key 句对的身份正合适。
        """
        key = item[6] if len(item) >= 7 else None
        if key:
            return key
        stamp = item[2] if len(item) >= 3 else None
        return f"@ts:{stamp}"

    def _html_for_keys(self, keys, item_map, fixed, fade=True):
        """按 key 顺序拼整段富文本；fade=False 时忽略淡入 alpha（终值）。"""
        blocks = []
        last = len(keys) - 1
        for index, key in enumerate(keys):
            _, german, chinese = item_map[key]
            alpha = None
            if fade and self._fade_key is not None and key == self._fade_key:
                alpha = self._fade_alpha
            blocks.append(self._pair_html(
                german, chinese,
                dim=(index < last), top_gap=(index > 0), key=key, alpha=alpha,
            ))
        return self._join_render_blocks(blocks, fixed)

    def _fits_keys(self, keys, item_map, fixed, doc, avail_h):
        doc.setHtml(self._html_for_keys(keys, item_map, fixed))
        return doc.size().height() <= avail_h

    def _fit_from_scratch(self, item_keys, item_map, fixed, doc, avail_h):
        """从最新往回二分，找「装得下」的最多句对（窗口/字号/模式变了才走）。"""
        cap = len(item_keys)
        if not cap:
            return []
        shown = 1
        lo, hi = 2, cap
        while lo <= hi:
            mid = (lo + hi) // 2
            if self._fits_keys(item_keys[cap - mid:], item_map, fixed, doc, avail_h):
                shown = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return item_keys[cap - shown:]

    def _schedule_hold(self):
        """按 _hold_deadline 起一个单发定时器；没有待释放就停掉。"""
        timer = getattr(self, "_hold_timer", None)
        if timer is None:
            return
        if self._hold_deadline is None:
            timer.stop()
            return
        remaining_ms = max(1, int((self._hold_deadline - self._now()) * 1000) + 1)
        timer.start(remaining_ms)

    def _reset_appear_hold(self):
        """语言切换时清掉旧的 minHold 截止点。

        旧截止点是按旧语言句子的上屏时刻算的；留着它到点重渲染，会把刚上屏
        的新语言句子当成「早该走了」直接放走。这里同时把当前可见句的计时
        重新锚到此刻，切完语言重新给满一个 minHold。
        """
        self._hold_deadline = None
        timer = getattr(self, "_hold_timer", None)
        if timer is not None:
            timer.stop()
        now = self._now()
        visible = getattr(self, "_visible_pair_keys", [])
        self._shown_at = {key: now for key in visible}
        self._render_lang_revision = int(getattr(self, "language_revision", 0))
        self._stop_fade()

    def _on_hold_release(self):
        """minHold 到期：放开受保护的旧句，重新按「装得下」收拢。"""
        self._hold_deadline = None
        timer = getattr(self, "_hold_timer", None)
        if timer is not None:
            timer.stop()
        self._render(_allow_evict=True)

    def _stop_fade(self):
        anim = getattr(self, "_fade_anim", None)
        if anim is not None:
            anim.stop()
        self._fade_key = None
        self._fade_alpha = 1.0

    def _finish_fade(self):
        """立即把淡入推到终值（新句抢进来/探针不等 150ms 时用）。"""
        if self._fade_key is None:
            return
        self._stop_fade()
        self._render_fade_frame()

    def _on_fade_value(self, value):
        self._fade_alpha = float(value)
        self._render_fade_frame()

    def _on_fade_finished(self):
        self._fade_alpha = 1.0
        self._fade_key = None
        self._render_fade_frame()

    def _render_fade_frame(self):
        """淡入每一帧只重建 HTML + setText，不重跑可见集合二分。"""
        keys = getattr(self, "_fade_view_keys", None)
        if not keys:
            return
        html_str = self._html_for_keys(keys, self._fade_item_map, self._fade_fixed, fade=True)
        # _last_html 始终保留终值版本：点词命中测试按它重建排版，没必要
        # 去解析一个可能正淡到一半颜色的文档；屏幕上的标签才用 faded 版。
        self.window.setText(html_str)
        self.window.show()

    def _render(self, _allow_evict=False):
        """把句对历史+live行渲染成富文本。

        显示几条句对由真实排版高度决定：用 QTextDocument 按 QLabel 相同的
        字体/宽度排版测高，从最新往旧塞，塞到窗口装不下为止——窗口拉多大
        就填多满（瀑布式），不再靠折行估算（之前估得保守，顶部留大片空黑）。

        2026-10-31 P4：可见集合「粘住」。live 行变高/草稿/译文原位替换不再
        把已上屏句对挤出去——装不下就让 QLabel 底对齐把顶部裁掉（比消失再
        回来更不闪）。只有新句对到来装不下、窗口/字号/模式变化、minHold
        到期这三种情况才收缩。
        """
        now = self._now()
        live_block = self._live_block_html()
        status = html.escape(self.status_line) if self.status_line else ""
        fixed = [b for b in (live_block, status) if b]

        # 和 QLabel 一致的排版环境（padding 15px 20px + 2px边框）；缓存复用
        doc = self._doc_for_render()
        avail_h = self.window.height() - 40

        # 从最新往回只构造到 MAX_SENTENCE_PAIRS 条为止（理由见旧实现：
        # sentence_pairs 内存里留 50 条，上屏上限只有 4-20 条）。
        keep = max(1, int(getattr(config, "MAX_SENTENCE_PAIRS", 20)))
        items = []
        for item in reversed(self.sentence_pairs):
            german, chinese = item[0], item[1]
            key = self._pair_key(item)
            block = self._pair_html(german, chinese, key=key)
            if not block:
                continue
            items.append((key, german, chinese))
            if len(items) >= keep:
                break
        items.reverse()
        item_map = {key: (key, german, chinese) for key, german, chinese in items}
        item_keys = [key for key, _, _ in items]

        # 布局签名只包含会改变「装得下几条」的东西；live/草稿/译文内容不算。
        layout_sig = (
            self.window.width(), self.window.height(),
            int(config.FONT_SIZE), _display_mode(), keep,
        )
        env_changed = layout_sig != getattr(self, "_render_layout_sig", None)
        self._render_layout_sig = layout_sig
        lang = int(getattr(self, "language_revision", 0))
        if lang != getattr(self, "_render_lang_revision", None):
            self._reset_appear_hold()

        had_fade_key = self._fade_key
        if not items:
            self._visible_pair_keys = []
            self._shown_at.clear()
            self._hold_deadline = None
            self._schedule_hold()
            if had_fade_key is not None:
                self._stop_fade()
            if not fixed:
                return  # 什么都没有就保持现状（避免闪空白）
            html_str = self._join_render_blocks([], fixed)
            self._last_html = html_str
            self.window.setText(html_str)
            self.window.show()
            return

        prev_keys = [key for key in self._visible_pair_keys if key in item_map]
        if env_changed or not prev_keys:
            # 环境变了（或首次渲染）允许按当前高度重新收拢；这里不保护
            # minHold——用户把窗口缩小/换字号时本来就预期历史条数变化。
            selected = self._fit_from_scratch(item_keys, item_map, fixed, doc, avail_h)
            protected_key = None
        else:
            # 已上屏集合是一个后缀：从最旧那句开始取到最新，天然包含新到的句对。
            start = item_keys.index(prev_keys[0])
            selected = list(item_keys[start:])
            prev_set = set(prev_keys)
            has_new = any(key not in prev_set for key in selected)
            protected_key = None
            if _allow_evict or has_new:
                while selected and not self._fits_keys(selected, item_map, fixed, doc, avail_h):
                    if len(selected) <= 1:
                        break
                    oldest = selected[0]
                    shown_at = self._shown_at.get(oldest)
                    if shown_at is not None and (now - shown_at) < float(design_tokens.MOTION_MIN_HOLD_SEC):
                        # 未满最短停留：保留它，允许顶部被裁，等定时器到期再放。
                        protected_key = oldest
                        break
                    selected.pop(0)

        if not selected and item_keys:
            selected = [item_keys[-1]]

        now_visible = set(selected)
        for key in list(self._shown_at):
            if key not in now_visible:
                del self._shown_at[key]
        for key in selected:
            if key not in self._shown_at:
                self._shown_at[key] = now

        self._visible_pair_keys = list(selected)

        # 淡入只给「从未上过屏」的 key；选最新那个（通常也是刚到的句对）。
        new_fade_keys = [key for key in selected if key not in self._ever_shown]
        chosen = new_fade_keys[-1] if new_fade_keys else None
        if chosen is not None and self._fade_key is not None and self._fade_key != chosen:
            # 淡入进行中又来新句：旧动画直接结束到终值，再淡入新的。
            self._stop_fade()
        if chosen is not None:
            self._fade_key = chosen
            self._fade_alpha = 0.0
        elif self._fade_key is not None and self._fade_key not in now_visible:
            self._stop_fade()
        self._ever_shown.update(now_visible)

        html_str = self._html_for_keys(selected, item_map, fixed, fade=False)
        self._last_html = html_str
        if chosen is not None or self._fade_key is not None:
            html_str = self._html_for_keys(selected, item_map, fixed, fade=True)
        self.window.setText(html_str)
        self.window.show()

        # 淡入每帧/结束时也复用这份可见集合，只换颜色 alpha。
        self._fade_view_keys = list(selected)
        self._fade_item_map = item_map
        self._fade_fixed = fixed

        if protected_key is not None:
            self._hold_deadline = self._shown_at.get(protected_key, now) + float(
                design_tokens.MOTION_MIN_HOLD_SEC)
        else:
            self._hold_deadline = None
        self._schedule_hold()

        if chosen is not None:
            anim = getattr(self, "_fade_anim", None)
            if anim is not None:
                anim.stop()
                anim.setDuration(int(design_tokens.MOTION_FADE_IN_MS))
                anim.setStartValue(0.0)
                anim.setEndValue(1.0)
                anim.start()

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
