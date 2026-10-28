"""字幕存档：每天一个明文文件 + 会话级 SRT / CSV 导出 + 保留期清理。

从 translator_queue.py 拆出来的（2026-08-12，继 lookup.py 之后的第二刀）。
拆它的理由和拆查词那次一样——不是"行数多"，而是这一块和主链路（识别→切句→
翻译队列）**一个锁都不共享**：它只碰 `_transcript_*` 与 `_session_*` 字段，
被调用点也只有翻译 worker 那两处。

☠️ 这不只是"存个日志"，是隐私面：本程序抓的是**系统全部声音**（可能含语音
通话），存档是**明文**、按天一个文件、`SAVE_TRANSCRIPT` 默认开着。保留期
（`TRANSCRIPT_KEEP_DAYS`）默认 0 = 永久保留，这是有意的默认（transcripts 是
拿来回看和学德语的，自动删掉用户攒的语料不该是默认），但代价要写清楚。
会话导出（SAVE_SESSION_SRT / SAVE_SESSION_CSV）由 SAVE_TRANSCRIPT 总门禁控制：
总存档关闭时，任何会话导出全部禁用。

☠️ 本 mixin **不自己 __init__**，下列状态由 `WhisperQueueTranslator.__init__`
建好（和 lookup.py / UI 那两个 mixin 是同一个约定）：

    self._transcript_ok       每日存档是否可用（目录建失败/写失败一次就置 False）
    self._transcript_dir      存档目录绝对路径（走 paths.repo_path，别用 __file__）
    self._session_export_ok   会话导出是否可用（失败一次置 False，不影响每日存档）
    self._session_start_time  会话开始时间戳（用于相对时间计算）
    self._session_id          会话文件名标识（YYYY-MM-DD_HHMMSS）
    self._session_prev_end    排序后最后一句的结束时间（秒），仅供诊断/兼容
    self._session_index       当前会话的行数（= SRT 最后一条的序号）
    self._session_entries     本会话的行，按句子 key 去重：key -> 行数据
    self._session_seq         没有 key 的句子/并列排序用的递增序号

类属性提供安全默认值，避免单测 __new__ 半成品实例抛 AttributeError。

☠️ 会话导出**整份重写**而不是逐条追加。句子可能因为翻译失败先跳过、重试成功
后才回来（`_tx_retry_queue`），也可能被用户手动重试——按到达顺序追加就会把
它的时间戳钳到后面几句之后，还可能写出重复的行。所以行先按 key 存进内存，
每次保存时按句子自身的音频起点排序、整份重排时间轴再写盘。CLAUDE.md §4 #20
量过，整份 checkpoint（含全部行）写入 2000 条约 2~4ms，这里每句重写一次
完全可接受。

线程约定：`_save_transcript` 只在 `_tx_executor` 这一个线程里被调用
（`_translation_worker` 的两个出口），所以 `_transcript_day` 与 `_session_*`
都不用加锁。加新的调用点之前先确认这一条还成立。
"""
import csv
from datetime import datetime
import io
import os
import time

import realtime_subtitle.config as config


def _srt_time(seconds: float) -> str:
    """把秒数格式化为 SRT 时间戳 HH:MM:SS,mmm。"""
    seconds = max(0.0, float(seconds))
    whole = int(seconds)
    millis = int(round((seconds - whole) * 1000))
    if millis >= 1000:
        whole += 1
        millis = 0
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def estimate_duration(text: str) -> float:
    """按文本长度估算句子朗读时长（秒），钳制在 1~8 秒。

    按自然语速约 12~15 字符/秒（~0.08s/字）估算，短句保底 1.0 秒，长句上限 8.0 秒。
    """
    clean = text.strip() if text else ""
    est = len(clean) * 0.08
    return max(1.0, min(8.0, est))


def _atomic_write(path: str, text: str, encoding: str):
    """整份重写一个文本文件：同目录临时文件 + os.replace，崩溃时不会留半截文件。

    临时文件必须和目标是同一目录（同一文件系统），否则 os.replace 可能退化成
    复制+删除，就不是原子操作了。
    """
    tmp = path + ".tmp"
    with open(tmp, "w", encoding=encoding, newline="") as f:
        f.write(text)
        f.flush()
    try:
        os.replace(tmp, path)
    except PermissionError:
        # Windows 上目标被别的程序独占打开（典型是 Excel 开着 CSV）时 replace 会被拒。
        # 别留下 .tmp 垃圾；由调用方决定这次是否跳过
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


class TranscriptMixin:
    """字幕存档与会话导出。状态由宿主类的 __init__ 提供，见模块 docstring。"""

    # 上一条存档写在哪一天（跨天要重跑保留期清理，见 _save_transcript）。
    # 放类属性而不是只在 __init__ 里赋值：单测习惯用 __new__ 造个壳只塞它关心的
    # 几个字段，__init__ 中途失败时也一样是半成品——存档路径不该因此炸
    _transcript_day = None

    # 会话导出状态（单次运行惰性创建，单 executor 线程无锁操作）
    _session_export_ok = True
    _session_start_time = None
    _session_id = None
    _session_prev_end = 0.0
    _session_index = 0
    # 用 None 而不是 {}：类属性是共享的，__new__ 造出来的壳必须各自拿一份
    _session_entries = None
    _session_seq = 0
    _session_srt_file = None
    _session_csv_file = None
    _session_csv_writer = None

    # ------------------------------------------------------------------
    # 字幕记录与保留期清理
    # ------------------------------------------------------------------
    def _prune_old_transcripts(self):
        """启动或跨天时清掉超过 TRANSCRIPT_KEEP_DAYS 天的存档（0 = 永久保留）。

        ⚠️ 这是隐私措施，不是省磁盘：本程序抓的是系统全部声音，存档是明文，
        而且 SAVE_TRANSCRIPT 默认开着。以前没有任何保留期，装多久就攒多久。

        按**文件名**里的日期判断而不是 mtime：
        - 每日存档文件名就是 YYYY-MM-DD.txt
        - 会话导出文件名是 sessions/YYYY-MM-DD_HHMMSS.srt 和 .csv
        mtime 会被同步网盘/备份工具刷新。认不出日期的文件一律不碰（可能是用户自己放进去的）。
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

        # 清理 sessions/ 下的会话导出文件
        sessions_dir = os.path.join(self._transcript_dir, "sessions")
        try:
            session_names = os.listdir(sessions_dir)
        except OSError:
            session_names = []

        # ☠️ 正在写的会话不能被清理：程序连着跑超过保留期（比如开了 30 天以上）
        # 时，启动/跨天那次清理会把当前会话已经写好的文件删掉，而文件句柄在
        # Windows 上还能继续追加——用户最后拿到的是一个开头缺一块的 SRT。
        current_session = getattr(self, "_session_id", None)

        for name in session_names:
            stem, ext = os.path.splitext(name)
            if ext.lower() not in (".srt", ".csv"):
                continue
            # ☠️ 只认会话导出的确切命名 %Y-%m-%d_%H%M%S。以前还接受纯日期
            # %Y-%m-%d，那和每日存档同名，很容易误删用户自己放进 sessions/
            # 的同名文件；认不出的命名一律不碰。
            try:
                parsed = time.strptime(stem, "%Y-%m-%d_%H%M%S")
            except ValueError:
                continue
            if time.strftime("%Y-%m-%d_%H%M%S", parsed) != stem:
                continue  # strptime 也接受不补零的写法，回写一遍才算真匹配
            if current_session and stem == current_session:
                continue
            ts = time.mktime(parsed)
            if ts >= cutoff:
                continue
            try:
                os.remove(os.path.join(sessions_dir, name))
                removed += 1
            except OSError:
                pass

        if removed:
            print(f"🧹 已清理 {removed} 个超过 {days} 天的字幕存档"
                  f"（保留期在 config.TRANSCRIPT_KEEP_DAYS，设 0 可关闭）")

    def _close_session_export(self):
        """关闭会话导出文件句柄（现在每次整份重写、不再常驻句柄，留作兼容/兜底）。"""
        for attr in ("_session_srt_file", "_session_csv_file"):
            f = getattr(self, attr, None)
            if f is not None:
                try:
                    f.close()
                except OSError:
                    pass
                setattr(self, attr, None)
        self._session_csv_writer = None

    def _write_session_file(self, path, text, encoding):
        """写一份会话文件；目标被占用就跳过这一次，不关掉导出。

        每次保存都是整份重写，所以跳过一次没有损失：文件解除占用后的下一次保存
        会把漏掉的行一并补上。用户一边看字幕一边拿 Excel 开着 CSV 是这个功能的
        正常用法，不能因此把 SRT 也一起关掉（其它 OSError 仍按"失败一次就关"处理）。
        """
        try:
            _atomic_write(path, text, encoding)
        except PermissionError as e:
            if not getattr(self, "_session_busy_warned", False):
                print(f"⚠️  会话导出文件被占用，暂时跳过，关掉占用它的程序后会自动补上: {e}")
                self._session_busy_warned = True

    def _render_session_exports(self, save_srt, save_csv):
        """按句子自身的音频起点排序后整份重写 SRT/CSV。

        排完序再算单调不重叠，而不是按"保存到达顺序"钳制：失败后重试成功的
        句子到达时，后面几句早就写过了，按到达顺序钳制会把它的 start 顶到
        它们后面。排序之后 prev_end 只来自真正排在它前面的句子。
        """
        sessions_dir = os.path.join(self._transcript_dir, "sessions")
        os.makedirs(sessions_dir, exist_ok=True)

        ordered = sorted(
            self._session_entries.values(),
            key=lambda e: (e["sort_start"], e["seq"]),
        )

        prev_end = 0.0
        srt_blocks = []
        csv_rows = []
        for idx, entry in enumerate(ordered, 1):
            start = max(prev_end, entry["start"])
            end = entry["end"]
            if end <= start:
                end = start + max(1.0, entry["dur"])
            prev_end = end

            if save_srt:
                body = f"{entry['src']}\n{entry['tgt']}" if entry["tgt"] else entry["src"]
                srt_blocks.append(
                    f"{idx}\n{_srt_time(start)} --> {_srt_time(end)}\n{body}\n\n"
                )
            if save_csv:
                csv_rows.append([
                    entry["wall_iso"],
                    f"{start:.3f}",
                    f"{end:.3f}",
                    entry["src_lang"],
                    entry["tgt_lang"],
                    entry["src"],
                    entry["tgt"],
                ])

        if save_srt:
            self._write_session_file(
                os.path.join(sessions_dir, f"{self._session_id}.srt"),
                "".join(srt_blocks),
                "utf-8",
            )
        if save_csv:
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow([
                "wall_time_iso", "start", "end",
                "source_lang", "target_lang",
                "source", "translation"
            ])
            writer.writerows(csv_rows)
            # utf-8-sig：带 BOM，Excel 双击打开才不会把中文看成乱码
            self._write_session_file(
                os.path.join(sessions_dir, f"{self._session_id}.csv"),
                buf.getvalue(),
                "utf-8-sig",
            )

        self._session_prev_end = prev_end
        self._session_index = len(ordered)

    def _save_transcript(self, source_text, translation, t0=None, t1=None,
                         source_lang=None, target_lang=None):
        """把一条字幕追加到当天的记录文件与会话级 SRT/CSV 导出（失败一次就关闭，不刷屏）。

        translation 为空 = 这句没翻出来（Ollama 挂了/熔断中）。原文照写，
        只是不留那行空的译文——回看时能看出"这句当时没有中文"，而不是整句消失。

        会话导出（sessions/YYYY-MM-DD_HHMMSS.srt 和 .csv）在首条字幕到达时惰性创建。
        SRT 双语排版（原文在上、译文在下）；CSV 包含 wall_time_iso,start,end,source_lang,
        target_lang,source,translation（utf-8-sig 带 BOM 保证 Excel 打开正常）。
        时间戳优先使用音频自身样本时钟 t0/t1，缺失时以墙钟写入时刻为 end，start 按字数估算（1~8秒）。

        ☠️ 行按句子 key 去重后整份重写（见模块 docstring）：重试回来的句子会
        替换同一 key 的行并回到它自己的时间位置，手动重试同一句也不会出现两行。
        每句写完即原子替换整个文件，进程异常退出最多丢掉正在写的这一条。

        ☠️ 跨天时要再跑一次 _prune_old_transcripts。
        本函数只在 _tx_executor 这一个线程里被调，_transcript_day 与会话导出状态均不用加锁。
        """
        if not getattr(config, "SAVE_TRANSCRIPT", False) or not getattr(self, "_transcript_ok", False):
            return

        now = time.time()

        # --------------------------------------------------------------
        # 1. 每日明文存档 (transcripts/YYYY-MM-DD.txt)
        # --------------------------------------------------------------
        try:
            day = time.strftime("%Y-%m-%d", time.localtime(now))
            if day != self._transcript_day:
                self._transcript_day = day
                self._prune_old_transcripts()
            path = os.path.join(self._transcript_dir, day + ".txt")
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%H:%M:%S', time.localtime(now))}] {source_text}\n")
                if translation:
                    f.write(f"           {translation}\n")
                f.write("\n")
        except OSError as e:
            print(f"⚠️  字幕记录写入失败，记录功能关闭: {e}")
            self._transcript_ok = False

        # --------------------------------------------------------------
        # 2. 会话级 SRT / CSV 导出 (transcripts/sessions/YYYY-MM-DD_HHMMSS.*)
        # --------------------------------------------------------------
        if not getattr(self, "_session_export_ok", True):
            return

        save_srt = bool(getattr(config, "SAVE_SESSION_SRT", True))
        save_csv = bool(getattr(config, "SAVE_SESSION_CSV", True))
        if not save_srt and not save_csv:
            return

        if getattr(self, "_session_start_time", None) is None:
            self._session_start_time = now
        if getattr(self, "_session_id", None) is None:
            self._session_id = time.strftime(
                "%Y-%m-%d_%H%M%S", time.localtime(self._session_start_time)
            )

        try:
            audio_t0 = t0 if t0 is not None else getattr(source_text, "t0", None)
            audio_t1 = t1 if t1 is not None else getattr(source_text, "t1", None)
            dur = estimate_duration(str(source_text))

            if audio_t0 is not None and audio_t1 is not None:
                raw_start = float(audio_t0)
                raw_end = float(audio_t1)
                if raw_end <= raw_start:
                    raw_end = raw_start + max(1.0, dur)
                # 排序用句子自己的音频起点（要求 4：不按到达顺序）
                sort_start = raw_start
            else:
                save_elapsed = max(0.0, now - float(self._session_start_time))
                raw_end = save_elapsed
                raw_start = max(0.0, save_elapsed - dur)
                if raw_end <= raw_start:
                    raw_end = raw_start + dur
                # 没有音频时钟时唯一可用的时间就是保存时刻
                sort_start = raw_end

            entries = getattr(self, "_session_entries", None)
            if not isinstance(entries, dict):
                entries = {}
                self._session_entries = entries

            key = getattr(source_text, "key", None)
            prev = entries.get(key) if key is not None else None
            if prev is not None:
                # 重试同一句：沿用原来的排列位置与墙钟时间，只更新内容
                seq = prev["seq"]
                wall_iso = prev["wall_iso"]
            else:
                seq = (getattr(self, "_session_seq", 0) or 0) + 1
                self._session_seq = seq
                if key is None:
                    key = f"__seq{seq}"  # 没有 key 的句子各自成行
                wall_iso = datetime.fromtimestamp(now).isoformat()

            src_lang = (
                source_lang
                or getattr(self, "source_language", None)
                or getattr(config, "SOURCE_LANGUAGE", "de")
            )
            tgt_lang = target_lang or getattr(self, "target_language", None)
            if not tgt_lang:
                try:
                    from realtime_subtitle.translate.language_switch import current_target_language
                    tgt_lang = current_target_language()
                except Exception:
                    tgt_lang = getattr(config, "TARGET_LANGUAGE", "zh")

            entries[key] = {
                "start": raw_start,
                "end": raw_end,
                "sort_start": sort_start,
                "dur": dur,
                "seq": seq,
                "wall_iso": wall_iso,
                "src_lang": str(src_lang),
                "tgt_lang": str(tgt_lang),
                "src": str(source_text).strip(),
                "tgt": str(translation).strip() if translation else "",
            }
            self._render_session_exports(save_srt, save_csv)

        except (OSError, ValueError) as e:
            print(f"⚠️  会话导出写入失败，导出功能关闭: {e}")
            self._session_export_ok = False
            self._close_session_export()
