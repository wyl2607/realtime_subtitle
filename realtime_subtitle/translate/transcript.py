"""字幕存档：每天一个明文文件 + 保留期清理。

从 translator_queue.py 拆出来的（2026-08-12，继 lookup.py 之后的第二刀）。
拆它的理由和拆查词那次一样——不是"行数多"，而是这一块和主链路（识别→切句→
翻译队列）**一个锁都不共享**：它只碰 `_transcript_*` 三个字段，被调用点也只有
翻译 worker 那一处。放在 1850 行的大类里会让人误以为它和那 5 把锁有关系。

☠️ 这不只是"存个日志"，是隐私面：本程序抓的是**系统全部声音**（可能含语音
通话），存档是**明文**、按天一个文件、`SAVE_TRANSCRIPT` 默认开着。保留期
（`TRANSCRIPT_KEEP_DAYS`）默认 0 = 永久保留，这是有意的默认（transcripts 是
拿来回看和学德语的，自动删掉用户攒的语料不该是默认），但代价要写清楚。

☠️ 本 mixin **不自己 __init__**，下列状态由 `WhisperQueueTranslator.__init__`
建好（和 lookup.py / UI 那两个 mixin 是同一个约定）：

    self._transcript_ok    存档是否可用（目录建失败/写失败一次就置 False）
    self._transcript_dir   存档目录绝对路径（走 paths.repo_path，别用 __file__）

`_transcript_day` 是本 mixin 自己的类属性，不需要宿主类提供。

线程约定：`_save_transcript` 只在 `_tx_executor` 这一个线程里被调用
（`_translation_worker` 的两个出口），所以 `_transcript_day` 不用加锁。
加新的调用点之前先确认这一条还成立。
"""
import os
import time

import realtime_subtitle.config as config


class TranscriptMixin:
    """字幕存档。状态由宿主类的 __init__ 提供，见模块 docstring。"""

    # 上一条存档写在哪一天（跨天要重跑保留期清理，见 _save_transcript）。
    # 放类属性而不是只在 __init__ 里赋值：单测习惯用 __new__ 造个壳只塞它关心的
    # 几个字段，__init__ 中途失败时也一样是半成品——存档路径不该因此炸
    _transcript_day = None

    # ------------------------------------------------------------------
    # 字幕记录
    # ------------------------------------------------------------------
    def _prune_old_transcripts(self):
        """启动时清掉超过 TRANSCRIPT_KEEP_DAYS 天的存档（0 = 永久保留）。

        ⚠️ 这是隐私措施，不是省磁盘：本程序抓的是系统全部声音，存档是明文，
        而且 SAVE_TRANSCRIPT 默认开着。以前没有任何保留期，装多久就攒多久。

        按**文件名**里的日期判断而不是 mtime：文件名就是 YYYY-MM-DD.txt
        （_save_transcript 的格式），而 mtime 会被同步网盘/备份工具刷新。
        认不出日期的文件一律不碰（可能是用户自己放进去的）。
        """
        # ☠️ 配置值必须容错。这个函数现在也从 _save_transcript 的跨天分支调，
        # 而那条路径在 _translation_worker 里、外面没有 try——config_local.py
        # 里写成 TRANSCRIPT_KEEP_DAYS = "30" 之类的话，一个 ValueError 就会把
        # 整个翻译 worker 打断（句对不上屏），症状和"配置写错"毫无关联
        try:
            days = int(getattr(config, "TRANSCRIPT_KEEP_DAYS", 0) or 0)
        except (TypeError, ValueError):
            print("⚠️  TRANSCRIPT_KEEP_DAYS 不是整数，本次不清理存档"
                  f"（当前值 {getattr(config, 'TRANSCRIPT_KEEP_DAYS', None)!r}）")
            return
        if days <= 0:
            return
        cutoff = time.time() - days * 86400
        removed = 0
        try:
            names = os.listdir(self._transcript_dir)
        except OSError:
            return
        for name in names:
            stem, ext = os.path.splitext(name)
            if ext.lower() != ".txt":
                continue
            try:
                ts = time.mktime(time.strptime(stem, "%Y-%m-%d"))
            except ValueError:
                continue  # 不是当天存档的命名，别动
            if ts >= cutoff:
                continue
            try:
                os.remove(os.path.join(self._transcript_dir, name))
                removed += 1
            except OSError:
                pass  # 被占用/只读：跳过，下次启动再试
        if removed:
            print(f"🧹 已清理 {removed} 个超过 {days} 天的字幕存档"
                  f"（保留期在 config.TRANSCRIPT_KEEP_DAYS，设 0 可关闭）")

    def _save_transcript(self, source_text, translation):
        """把一条字幕追加到当天的记录文件（失败一次就关闭，不刷屏）。

        translation 为空 = 这句没翻出来（Ollama 挂了/熔断中）。原文照写，
        只是不留那行空的译文——回看时能看出"这句当时没有中文"，而不是整句消失。

        ☠️ 跨天时要再跑一次 _prune_old_transcripts。以前它只在 __init__ 里调
        一次，而字幕程序的典型用法是**开机挂着不关**——也就是说设了
        TRANSCRIPT_KEEP_DAYS 的用户，只要不重启就永远不会真的清理，配置看着
        生效实际没生效。这是隐私措施不是省磁盘（存档是明文、抓的是系统全部
        声音），"以为在删其实没删"比不提供这个选项更糟。
        本函数只在 _tx_executor 这一个线程里被调，_transcript_day 不用加锁。
        """
        if not self._transcript_ok:
            return
        try:
            day = time.strftime("%Y-%m-%d")
            if day != self._transcript_day:
                self._transcript_day = day
                self._prune_old_transcripts()
            path = os.path.join(self._transcript_dir, day + ".txt")
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%H:%M:%S')}] {source_text}\n")
                if translation:
                    f.write(f"           {translation}\n")
                f.write("\n")
        except OSError as e:
            print(f"⚠️  字幕记录写入失败，记录功能关闭: {e}")
            self._transcript_ok = False
