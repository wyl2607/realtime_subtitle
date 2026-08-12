"""分钟级性能概况（`📈 概况:` 那一行）。

从 translator_queue.py 拆出来的（2026-08-12，继 lookup.py / transcript.py
之后的第三刀）。和前两刀同理：这一块只碰 `_stat_*` 字段和 `_stats_lock`，
和主链路的那几把锁（`_tx_lock` / `_asr_lock` / `_inflight_lock`）没有任何
交集，采样点只是被 ASR 线程和翻译 worker 各调一处。

☠️ 别把它当成"可有可无的日志"。`SHOW_PERFORMANCE` 默认关闭（长直播时每
0.5 秒刷一行会把 subtitle.log 撑很大），概况行是关掉它之后**仅剩的观测
手段**——约 1 行/分钟。CLAUDE.md 第 4 节第 20 条里那几条"看着像可优化点、
实测都不是"的结论（ASR 占空比、缓冲长短分桶、与翻译并发的影响）全靠这行
攒出来的跨天数据，start_subtitles.ps1 还专门为它做了日志归档。

☠️ 本 mixin **不自己 __init__**，下列状态由 `WhisperQueueTranslator.__init__`
建好（和 lookup.py / transcript.py 是同一个约定）：

    self._stats_lock         保护下面所有字段
    self._stats_t0           本轮统计的起点时刻
    self._stat_asr           每轮识别耗时
    self._stat_tx            每次 Ollama 翻译耗时
    self._stat_buf_max       音频缓冲峰值（秒）
    self._stat_merge         合并多块处理的轮数（GPU 落后的信号）
    self._stat_asr_shortbuf / _stat_asr_longbuf   按推理前缓冲长度分桶
    self._stat_asr_solo     / _stat_asr_overlap   按是否与翻译并发分桶

☠️ 下面这四个字段的**自增仍在宿主类里**，本 mixin 只负责读出来和清零——
契约是双向的，加计数器时两边都要改（漏改的表现是概况行里那一项恒为 0，
而这种"少了一个诊断项"极难被注意到）：

    self._stat_draft         草稿翻译次数          ← _maybe_draft 自增
    self._stat_dict          感叹词词典直译次数     ← _translation_worker 自增
    self._stat_held_release  句尾扣留到点放行次数   ← _release_held_boundary 自增
    self._stat_held_misfire  其中切在句中的次数     ← _append_committed 自增

线程约定：`_stat_note_asr` 跑在 ASR 线程、`_stat_note_tx` 跑在翻译 worker，
两者都只在 `_stats_lock` 内改字段；到点打印那段刻意放在**锁外**（print 不该
持着锁）。
"""
import time

import realtime_subtitle.config as config


class StatsMixin:
    """分钟级性能概况。状态由宿主类的 __init__ 提供，见模块 docstring。"""

    @staticmethod
    def _stats_enabled():
        """概况关掉时（STATS_SUMMARY_INTERVAL<=0）一个样本都不该再收。

        ☠️ 排空点只有 _stat_note_asr 里那个到点打印的分支，而它在
        `interval <= 0` 时直接 return。翻译耗时以前是在 _translation_worker
        里【无条件】 append 的，于是用户一旦按 config 注释把概况关掉，
        _stat_tx 就再也没有出口了——一条一句、长跑几小时只涨不消。
        采样入口统一走这个门。
        """
        return getattr(config, "STATS_SUMMARY_INTERVAL", 60) > 0

    def _stat_note_tx(self, elapsed):
        """记一次翻译耗时（概况关掉时直接丢弃，不进内存）。"""
        if not self._stats_enabled():
            return
        with self._stats_lock:
            self._stat_tx.append(elapsed)

    def _stat_note_asr(self, elapsed, buf_sec, n_items,
                       buf_before=None, overlapped=False):
        """记一轮识别指标；到间隔就打一行概况（跑在ASR线程，无音频时不打）

        buf_before/overlapped 是 2026-08-04 加的诊断项，用来回答两个之前
        answer 不了的问题（都需要真实使用数据，读代码得不出来）：
        1. **ASR 耗时到底受不受缓冲长度影响？** 分短/长缓冲两桶记 p50。
           faster-whisper 的 `pad_or_trim` 会把每段都补到固定 30 秒再进编码器
           （transcribe.py:1180 + feature_extractor nb_max_frames=3000），
           所以理论上编码开销与缓冲长度无关、只有解码随 token 数变。
           两桶 p50 若基本持平，就证实了这一点，`BUFFER_TRIM_SEC` 也就没有
           调小的价值——省得以后再有人去做那个 A/B。
        2. **翻译和识别同时跑会不会互相拖慢？** 按"推理开始时 Ollama 是否
           在飞"分桶记 p50，直接看差值。
        """
        interval = getattr(config, "STATS_SUMMARY_INTERVAL", 60)
        if not self._stats_enabled():
            return
        with self._stats_lock:
            self._stat_asr.append(elapsed)
            self._stat_buf_max = max(self._stat_buf_max, buf_sec)
            if buf_before is not None:
                # 分桶阈值取 BUFFER_KEEP_SEC：裁剪后停在它附近，涨到 TRIM 再裁，
                # 所以它天然就是"短缓冲/长缓冲"的分界
                bucket = (self._stat_asr_longbuf if buf_before >= config.BUFFER_KEEP_SEC
                          else self._stat_asr_shortbuf)
                bucket.append(elapsed)
            (self._stat_asr_overlap if overlapped else self._stat_asr_solo).append(elapsed)
            if n_items > 1:
                self._stat_merge += 1
            if time.time() - self._stats_t0 < interval:
                return
            asr, tx = sorted(self._stat_asr), sorted(self._stat_tx)
            merge, buf_max = self._stat_merge, self._stat_buf_max
            draft, dhit = self._stat_draft, self._stat_dict
            held, misfire = self._stat_held_release, self._stat_held_misfire
            shortb, longb = sorted(self._stat_asr_shortbuf), sorted(self._stat_asr_longbuf)
            solo, overlap = sorted(self._stat_asr_solo), sorted(self._stat_asr_overlap)
            self._stat_asr, self._stat_tx = [], []
            self._stat_merge, self._stat_buf_max = 0, 0.0
            self._stat_draft = self._stat_dict = 0
            self._stat_held_release = self._stat_held_misfire = 0
            self._stat_asr_shortbuf, self._stat_asr_longbuf = [], []
            self._stat_asr_solo, self._stat_asr_overlap = [], []
            self._stats_t0 = time.time()

        def pct(a, q):
            return a[min(len(a) - 1, int(q * len(a)))] if a else 0.0

        line = (f"📈 概况: 识别{len(asr)}次 p50 {pct(asr, .5):.2f}s p90 {pct(asr, .9):.2f}s"
                f" | 缓冲峰值 {buf_max:.1f}s")
        if merge:
            line += f" | 合并{merge}轮"
        if tx:
            line += f" | 翻译{len(tx)}次 p50 {pct(tx, .5):.1f}s p90 {pct(tx, .9):.1f}s"
        if draft:
            line += f" | 草稿{draft}"
        if dhit:
            line += f" | 词典直译{dhit}"
        if held:
            line += f" | 扣留放行{held}"
            if misfire:
                line += f"(切早{misfire})"
        # 诊断项：只在两桶都有样本时打，否则是噪声
        if shortb and longb:
            line += (f" | 缓冲短{pct(shortb, .5):.2f}s({len(shortb)})"
                     f"/长{pct(longb, .5):.2f}s({len(longb)})")
        if solo and overlap:
            line += (f" | 独占{pct(solo, .5):.2f}s({len(solo)})"
                     f"/与翻译并发{pct(overlap, .5):.2f}s({len(overlap)})")
        print(line)
