"""会话级双语 SRT / CSV 导出与保留期清理的单元测试。"""
import csv
from datetime import datetime
import time

import pytest

import realtime_subtitle.config as config
from realtime_subtitle.translate.transcript import (
    _srt_time,
    estimate_duration,
)
from realtime_subtitle.translate.translator_queue import TimedSentence, WhisperQueueTranslator


def _create_test_translator(tmp_path, session_start_time=None, session_id=None):
    """构建用于测试的轻量 WhisperQueueTranslator 实例（不加载模型）。"""
    t = WhisperQueueTranslator.__new__(WhisperQueueTranslator)
    t._transcript_ok = True
    t._transcript_dir = str(tmp_path)
    t._transcript_day = None

    t._session_export_ok = True
    t._session_start_time = session_start_time
    t._session_id = session_id
    t._session_prev_end = 0.0
    t._session_index = 0
    t._session_entries = {}
    t._session_seq = 0
    t._session_srt_file = None
    t._session_csv_file = None
    t._session_csv_writer = None
    return t


def test_lazy_creation(tmp_path):
    """首条字幕前不建 sessions 目录与空文件；首条写入时才惰性创建。"""
    t = _create_test_translator(tmp_path, session_start_time=1000.0, session_id="2026-10-27_120000")
    sessions_dir = tmp_path / "sessions"
    assert not sessions_dir.exists(), "初始化时不应预先创建 sessions 目录"

    # 第一条保存触发惰性创建
    t._save_transcript("Hallo Welt.", "你好世界。")
    t._close_session_export()

    assert sessions_dir.is_dir(), "首条保存后应创建 sessions 目录"
    srt_file = sessions_dir / "2026-10-27_120000.srt"
    csv_file = sessions_dir / "2026-10-27_120000.csv"
    assert srt_file.is_file(), "应该生成 .srt 会话文件"
    assert csv_file.is_file(), "应该生成 .csv 会话文件"
    assert srt_file.stat().st_size > 0, "SRT 文件不应为空"
    assert csv_file.stat().st_size > 0, "CSV 文件不应为空"


def test_disabled_when_save_transcript_is_false(tmp_path, monkeypatch):
    """隐私门禁：SAVE_TRANSCRIPT = False 时，每日存档与会话导出全部禁用。"""
    monkeypatch.setattr(config, "SAVE_TRANSCRIPT", False, raising=False)
    t = _create_test_translator(tmp_path, session_start_time=1000.0, session_id="2026-10-27_120000")

    t._save_transcript("Geheime Unterhaltung.", "机密对话。")
    t._close_session_export()

    sessions_dir = tmp_path / "sessions"
    assert not sessions_dir.exists(), "SAVE_TRANSCRIPT 关闭时不应生成 sessions 目录"
    # 每日 txt 也不应写入
    assert not list(tmp_path.glob("*.txt")), "SAVE_TRANSCRIPT 关闭时不应写入每日存档"


def test_gated_by_individual_export_switches(tmp_path, monkeypatch):
    """SAVE_SESSION_SRT 与 SAVE_SESSION_CSV 单独开关测试。"""
    sessions_dir = tmp_path / "sessions"

    # 1. 仅开 CSV
    monkeypatch.setattr(config, "SAVE_TRANSCRIPT", True, raising=False)
    monkeypatch.setattr(config, "SAVE_SESSION_SRT", False, raising=False)
    monkeypatch.setattr(config, "SAVE_SESSION_CSV", True, raising=False)
    t1 = _create_test_translator(tmp_path, session_start_time=1000.0, session_id="session_csv_only")
    t1._save_transcript("Nur CSV.", "仅 CSV。")
    t1._close_session_export()

    assert (sessions_dir / "session_csv_only.csv").exists()
    assert not (sessions_dir / "session_csv_only.srt").exists()

    # 2. 仅开 SRT
    monkeypatch.setattr(config, "SAVE_SESSION_SRT", True, raising=False)
    monkeypatch.setattr(config, "SAVE_SESSION_CSV", False, raising=False)
    t2 = _create_test_translator(tmp_path, session_start_time=1000.0, session_id="session_srt_only")
    t2._save_transcript("Nur SRT.", "仅 SRT。")
    t2._close_session_export()

    assert (sessions_dir / "session_srt_only.srt").exists()
    assert not (sessions_dir / "session_srt_only.csv").exists()


def test_exact_srt_format(tmp_path):
    """精确验证 SRT 格式：序号、相对时间 HH:MM:SS,mmm --> HH:MM:SS,mmm、双语两行与空行分隔。"""
    t = _create_test_translator(tmp_path, session_start_time=1000.0, session_id="2026-10-27_120000")

    # 传入显式音频时间戳 (t0, t1)
    t._save_transcript("Erster Satz.", "第一句。", t0=1.2, t1=3.456)
    t._save_transcript("Zweiter Satz.", "第二句。", t0=3.5, t1=6.0)
    # 译文为空时（Ollama 故障/未翻译），仅保留原文行
    t._save_transcript("Dritter Satz ohne Übersetzung.", "", t0=6.1, t1=8.9)
    t._close_session_export()

    srt_path = tmp_path / "sessions" / "2026-10-27_120000.srt"
    content = srt_path.read_text(encoding="utf-8")

    expected = (
        "1\n"
        "00:00:01,200 --> 00:00:03,456\n"
        "Erster Satz.\n"
        "第一句。\n\n"
        "2\n"
        "00:00:03,500 --> 00:00:06,000\n"
        "Zweiter Satz.\n"
        "第二句。\n\n"
        "3\n"
        "00:00:06,100 --> 00:00:08,900\n"
        "Dritter Satz ohne Übersetzung.\n\n"
    )
    assert content == expected


def test_srt_time_formatter():
    """验证 _srt_time 时间戳格式化毫秒进位与零填充。"""
    assert _srt_time(0.0) == "00:00:00,000"
    assert _srt_time(1.234) == "00:00:01,234"
    assert _srt_time(61.5) == "00:01:01,500"
    assert _srt_time(3661.005) == "01:01:01,005"
    assert _srt_time(0.9999) == "00:00:01,000"


def test_timing_monotonic_and_non_overlapping_with_audio_timestamps(tmp_path):
    """当 ASR 返回重叠或倒退的音频时间戳时，强制保证单调且不重叠。"""
    t = _create_test_translator(tmp_path, session_start_time=1000.0, session_id="timing_test")

    # 1. 正常区间 [2.0, 5.0]
    t._save_transcript("Satz 1", "句子1", t0=2.0, t1=5.0)
    # 2. 重叠区间 t0=4.0 < 5.0，应被推到 5.0；t1=7.0 > 5.0
    t._save_transcript("Satz 2", "句子2", t0=4.0, t1=7.0)
    # 3. 完全倒退区间 t0=5.5, t1=6.5 <= prev_end(7.0)，应从 7.0 开始并至少持续 1 秒
    t._save_transcript("Satz 3", "句子3", t0=5.5, t1=6.5)
    t._close_session_export()

    srt_path = tmp_path / "sessions" / "timing_test.srt"
    content = srt_path.read_text(encoding="utf-8")

    lines = content.splitlines()
    # 检查时间戳行
    time_lines = [l for l in lines if "-->" in l]
    assert len(time_lines) == 3

    # 解析每个区间的 start 和 end
    def parse_time(ts_str):
        h, m, rest = ts_str.strip().split(":")
        s, ms = rest.split(",")
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0

    intervals = []
    for line in time_lines:
        s_str, e_str = line.split("-->")
        intervals.append((parse_time(s_str), parse_time(e_str)))

    # 验证非重叠与严格单调
    for i in range(len(intervals)):
        start, end = intervals[i]
        assert end > start, f"结束时间必须大于开始时间: {start} -> {end}"
        if i > 0:
            prev_start, prev_end = intervals[i - 1]
            assert start >= prev_end, f"当前开始时间 {start} 不能早于上一句结束时间 {prev_end}"


def test_timing_wall_clock_fallback_with_duration_clamped(tmp_path, monkeypatch):
    """无音频时间戳时，以墙钟保存时刻为 end，start = max(previous end, end - duration) 钳制在 1~8s。"""
    # 模拟时间流逝
    curr_time = 1000.0
    monkeypatch.setattr(time, "time", lambda: curr_time)

    t = _create_test_translator(tmp_path, session_start_time=1000.0, session_id="wall_clock_test")

    # 1. 20秒后写入短句（~1s 估算时长）
    curr_time = 1020.0
    t._save_transcript("Hi.", "嗨。")  # 文本极短，钳制在 1.0s
    assert t._session_prev_end == 20.0

    # 2. 25秒后写入长句（100字符，钳制在 8.0s）
    curr_time = 1025.0
    long_text = "Ein sehr langer deutscher Satz. " * 4  # 128 字符
    t._save_transcript(long_text, "很长的中文句子。")
    # end = 25.0, dur 估算被钳制在 8.0s, end - dur = 17.0 < prev_end(20.0), 所以 start = 20.0, end = 25.0
    t._close_session_export()

    srt_path = tmp_path / "sessions" / "wall_clock_test.srt"
    content = srt_path.read_text(encoding="utf-8")
    assert "00:00:19,000 --> 00:00:20,000" in content
    assert "00:00:20,000 --> 00:00:25,000" in content


def test_estimate_duration_bounds():
    """验证文本时长估算在 1.0~8.0 秒内钳制。"""
    assert estimate_duration("") == 1.0
    assert estimate_duration("A") == 1.0
    assert estimate_duration("Hallo") == 1.0
    assert estimate_duration("x" * 50) == pytest.approx(4.0)
    assert estimate_duration("x" * 100) == 8.0
    assert estimate_duration("x" * 200) == 8.0


def test_csv_round_trip_with_commas_quotes_newlines_chinese(tmp_path):
    """CSV utf-8-sig BOM 导出与包含逗号、双引号、单引号、换行符、中文的完整往返解析。"""
    t = _create_test_translator(tmp_path, session_start_time=1000.0, session_id="2026-10-27_csv_roundtrip")

    test_pairs = [
        # 包含逗号
        ("Hallo, wie geht's, mein Freund?", "你好，最近怎么样，我的朋友？"),
        # 包含双引号
        ('Er sagte: "Das ist genial!"', '他说：“这太棒了！”'),
        # 包含换行符
        ("Erste Zeile.\nZweite Zeile.", "第一行。\n第二行。"),
        # 包含德语特殊字符与中文标点
        ("Größe, Äpfel, Überraschung & Spaß!", "尺寸、苹果、惊喜与乐趣！"),
    ]

    for idx, (src, tgt) in enumerate(test_pairs):
        t._save_transcript(src, tgt, t0=float(idx * 3), t1=float(idx * 3 + 2.5),
                           source_lang="de", target_lang="zh")
    t._close_session_export()

    csv_path = tmp_path / "sessions" / "2026-10-27_csv_roundtrip.csv"
    raw_bytes = csv_path.read_bytes()

    # 1. 验证带有 UTF-8 BOM 标头 (utf-8-sig)，保证 Excel 识别
    assert raw_bytes.startswith(b"\xef\xbb\xbf"), "CSV 必须以 UTF-8-sig BOM 标头开头"

    # 2. 用 csv.DictReader 往返读取并核对内容
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = list(csv.DictReader(f))

    assert len(reader) == len(test_pairs)

    expected_cols = ["wall_time_iso", "start", "end", "source_lang", "target_lang", "source", "translation"]
    assert list(reader[0].keys()) == expected_cols

    for row, (src, tgt) in zip(reader, test_pairs):
        # 验证 ISO 时间格式
        iso_str = row["wall_time_iso"]
        parsed_dt = datetime.fromisoformat(iso_str)
        assert isinstance(parsed_dt, datetime)

        assert row["source_lang"] == "de"
        assert row["target_lang"] == "zh"
        # 验证包含逗号、双引号、换行符的原始文本未被破坏
        assert row["source"] == src
        assert row["translation"] == tgt
        assert float(row["end"]) > float(row["start"])


def test_timed_sentence_attributes_detected_automatically(tmp_path):
    """当 source_text 是携带 t0/t1 的 TimedSentence 时，无需显式传参也能提取时间戳。"""
    t = _create_test_translator(tmp_path, session_start_time=1000.0, session_id="timed_sentence_test")

    timed_src = TimedSentence("Automatische Zeit.", t0=5.0, t1=8.2)
    t._save_transcript(timed_src, "自动时间戳。")
    t._close_session_export()

    srt_path = tmp_path / "sessions" / "timed_sentence_test.srt"
    content = srt_path.read_text(encoding="utf-8")
    assert "00:00:05,000 --> 00:00:08,200" in content


def test_robustness_write_error_disables_export_once_with_warning(tmp_path, monkeypatch, capsys):
    """写入失败时打印一次警告并关闭导出，不崩溃且后续不再尝试。"""
    from realtime_subtitle.translate import transcript as transcript_module

    t = _create_test_translator(tmp_path, session_start_time=1000.0, session_id="robustness_test")

    # 首次写入正常
    t._save_transcript("Satz 1", "句子1")

    # 人为使整份重写失败（模拟磁盘故障：换文件那一步抛 OSError）
    def broken_replace(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(transcript_module.os, "replace", broken_replace)

    # 第二次写入触发异常捕获
    t._save_transcript("Satz 2", "句子2")
    captured = capsys.readouterr()
    assert "⚠️  会话导出写入失败，导出功能关闭" in captured.out
    assert t._session_export_ok is False

    # 第三次调用直接被 _session_export_ok 拦截，不产生二次警告
    t._save_transcript("Satz 3", "句子3")
    captured_again = capsys.readouterr()
    assert "会话导出写入失败" not in captured_again.out


def test_locked_csv_skips_once_and_catches_up_without_disabling(tmp_path, monkeypatch, capsys):
    """CSV 被 Excel 占着（replace 抛 PermissionError）：SRT 照写、导出不关，解锁后补齐。"""
    from realtime_subtitle.translate import transcript as transcript_module

    t = _create_test_translator(tmp_path, session_start_time=1000.0, session_id="locked")
    real_replace = transcript_module.os.replace

    def locked_csv(src, dst):
        if dst.endswith(".csv"):
            raise PermissionError(13, "Permission denied", dst)
        return real_replace(src, dst)

    monkeypatch.setattr(transcript_module.os, "replace", locked_csv)
    t._save_transcript(TimedSentence("Satz 1", t0=1.0, t1=2.0, key="k1"), "句子1")
    t._save_transcript(TimedSentence("Satz 2", t0=3.0, t1=4.0, key="k2"), "句子2")
    out = capsys.readouterr().out
    assert out.count("会话导出文件被占用") == 1
    assert t._session_export_ok is True
    sessions = tmp_path / "sessions"
    assert "Satz 2" in (sessions / "locked.srt").read_text(encoding="utf-8")
    assert not (sessions / "locked.csv").exists()
    assert not list(sessions.glob("*.tmp"))

    monkeypatch.setattr(transcript_module.os, "replace", real_replace)
    t._save_transcript(TimedSentence("Satz 3", t0=5.0, t1=6.0, key="k3"), "句子3")
    with open(sessions / "locked.csv", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    assert [r[5] for r in rows[1:]] == ["Satz 1", "Satz 2", "Satz 3"]


def test_prune_old_transcripts_prunes_session_files(tmp_path, monkeypatch):
    """保留期清理：sessions/ 下超过 TRANSCRIPT_KEEP_DAYS 的 .srt 与 .csv 连同每日 .txt 一并删除；不认得的文件保留。"""
    day_sec = 86400
    now = time.time()
    old_date = time.strftime("%Y-%m-%d", time.localtime(now - 45 * day_sec))
    recent_date = time.strftime("%Y-%m-%d", time.localtime(now - 5 * day_sec))

    # 创建每日 txt
    (tmp_path / f"{old_date}.txt").write_text("old daily", encoding="utf-8")
    (tmp_path / f"{recent_date}.txt").write_text("recent daily", encoding="utf-8")
    (tmp_path / "custom_notes.txt").write_text("notes", encoding="utf-8")

    # 创建 sessions 目录及文件
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    old_session_srt = sessions_dir / f"{old_date}_100000.srt"
    old_session_csv = sessions_dir / f"{old_date}_100000.csv"
    recent_session_srt = sessions_dir / f"{recent_date}_120000.srt"
    recent_session_csv = sessions_dir / f"{recent_date}_120000.csv"
    unrecognized_srt = sessions_dir / "my_custom_session.srt"
    unrecognized_csv = sessions_dir / "important_table.csv"
    unrecognized_txt = sessions_dir / f"{old_date}_100000.txt"

    for p in (old_session_srt, old_session_csv, recent_session_srt, recent_session_csv,
              unrecognized_srt, unrecognized_csv, unrecognized_txt):
        p.write_text("content", encoding="utf-8")

    t = _create_test_translator(tmp_path)

    # 1. 保留期 30 天：旧文件应被删除
    monkeypatch.setattr(config, "TRANSCRIPT_KEEP_DAYS", 30, raising=False)
    t._prune_old_transcripts()

    # 每日文件验证
    assert not (tmp_path / f"{old_date}.txt").exists(), "超过30天的每日txt应被删除"
    assert (tmp_path / f"{recent_date}.txt").exists(), "近期的每日txt应保留"
    assert (tmp_path / "custom_notes.txt").exists(), "无法识别日期的文件应保留"

    # 会话文件验证
    assert not old_session_srt.exists(), "超过30天的会话srt应被删除"
    assert not old_session_csv.exists(), "超过30天的会话csv应被删除"
    assert recent_session_srt.exists(), "近期的会话srt应保留"
    assert recent_session_csv.exists(), "近期的会话csv应保留"
    assert unrecognized_srt.exists(), "未按日期命名的srt应保留"
    assert unrecognized_csv.exists(), "未按日期命名的csv应保留"
    assert unrecognized_txt.exists(), "sessions/ 下的非 .srt/.csv 应保留不碰"

    # 2. TRANSCRIPT_KEEP_DAYS = 0 永久保留，不做任何清理
    monkeypatch.setattr(config, "TRANSCRIPT_KEEP_DAYS", 0, raising=False)
    # 再造一个旧文件
    another_old = sessions_dir / f"{old_date}_220000.srt"
    another_old.write_text("test", encoding="utf-8")
    t._prune_old_transcripts()
    assert another_old.exists(), "KEEP_DAYS=0 时不应清理任何存档"


def _srt_blocks(path):
    """把 SRT 拆成 [(序号, 起止行, 正文), ...]，方便断言顺序。"""
    blocks = []
    for block in path.read_text(encoding="utf-8").strip().split("\n\n"):
        lines = block.splitlines()
        blocks.append((int(lines[0]), lines[1], "\n".join(lines[2:])))
    return blocks


def test_retried_sentence_keeps_original_time_order(tmp_path):
    """先失败、重试成功后才回来的句子回到自己的时间位置，而不是被顶到队尾。

    回归：以前重试成功同样走 _save_transcript，start = max(prev_end, t0)，
    而 prev_end 已经被后来那几句推得很高——重试句于是带着伪造过的时间戳
    排到了它们后面。
    """
    t = _create_test_translator(tmp_path, session_start_time=1000.0,
                                session_id="retry_order")

    # 第二、三句先成功写入（第一句还在重试队列里）
    t._save_transcript(TimedSentence("Zweiter Satz.", t0=4.0, t1=6.0, key="k2"),
                       "第二句。")
    t._save_transcript(TimedSentence("Dritter Satz.", t0=6.5, t1=8.0, key="k3"),
                       "第三句。")
    # 重试成功的第一句带着自己的原始音频时间回来
    t._save_transcript(TimedSentence("Erster Satz.", t0=1.0, t1=3.0, key="k1"),
                       "第一句。")

    srt_path = tmp_path / "sessions" / "retry_order.srt"
    blocks = _srt_blocks(srt_path)
    assert [b[2].splitlines()[0] for b in blocks] == [
        "Erster Satz.", "Zweiter Satz.", "Dritter Satz."
    ], "应按句子自身的音频起点排序，而不是按保存到达顺序"
    assert [b[1] for b in blocks] == [
        "00:00:01,000 --> 00:00:03,000",
        "00:00:04,000 --> 00:00:06,000",
        "00:00:06,500 --> 00:00:08,000",
    ], "重试句必须保留原始时间戳，不被钳制到后面几句之后"

    with open(tmp_path / "sessions" / "retry_order.csv", "r",
              encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert [r["source"] for r in rows] == [
        "Erster Satz.", "Zweiter Satz.", "Dritter Satz."
    ]
    assert [float(r["start"]) for r in rows] == [1.0, 4.0, 6.5]


def test_manual_retry_replaces_instead_of_duplicating(tmp_path):
    """用户手动重试终态失败句：同一个 key 覆盖原来那行空译文，不新增第二行。"""
    t = _create_test_translator(tmp_path, session_start_time=1000.0,
                                session_id="manual_retry")
    sentence = TimedSentence("Der Satz.", t0=1.0, t1=2.5, key="k1")

    # 终态失败：已经按"只有原文"存过一行
    t._save_transcript(sentence, "")
    # 用户点「未翻译 ⟳」后重试成功
    t._save_transcript(sentence, "这句话。")

    srt_path = tmp_path / "sessions" / "manual_retry.srt"
    blocks = _srt_blocks(srt_path)
    assert len(blocks) == 1, "重试同一句不应在 SRT 里留下两行"
    assert blocks[0][2] == "Der Satz.\n这句话。", "译文应替换进原来那一行"

    with open(tmp_path / "sessions" / "manual_retry.csv", "r",
              encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1, "重试同一句不应在 CSV 里留下两行"
    assert rows[0]["translation"] == "这句话。"


def test_prune_keeps_active_session_files(tmp_path, monkeypatch):
    """保留期清理不能删掉本次运行正在写的会话文件。

    回归：程序连续跑超过 TRANSCRIPT_KEEP_DAYS 天后，启动/跨天那次清理会把
    当前会话已经写好的 srt/csv 删掉（Windows 上句柄还能继续写，用户最后
    拿到的是缺开头的文件）。
    """
    day_sec = 86400
    now = time.time()
    old_date = time.strftime("%Y-%m-%d", time.localtime(now - 45 * day_sec))

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    active_id = f"{old_date}_100000"
    active_srt = sessions_dir / f"{active_id}.srt"
    active_csv = sessions_dir / f"{active_id}.csv"
    stale_srt = sessions_dir / f"{old_date}_110000.srt"
    for p in (active_srt, active_csv, stale_srt):
        p.write_text("content", encoding="utf-8")

    t = _create_test_translator(tmp_path, session_id=active_id)
    monkeypatch.setattr(config, "TRANSCRIPT_KEEP_DAYS", 30, raising=False)
    t._prune_old_transcripts()

    assert active_srt.exists(), "当前会话的 srt 不能被清理"
    assert active_csv.exists(), "当前会话的 csv 不能被清理"
    assert not stale_srt.exists(), "同一批里非当前会话的旧文件仍应删除"


def test_prune_ignores_plain_date_stem_in_sessions(tmp_path, monkeypatch):
    """sessions/ 下只认 %Y-%m-%d_%H%M%S，纯日期 stem 不当作会话文件删除。"""
    day_sec = 86400
    now = time.time()
    old_date = time.strftime("%Y-%m-%d", time.localtime(now - 45 * day_sec))

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    plain_srt = sessions_dir / f"{old_date}.srt"
    plain_csv = sessions_dir / f"{old_date}.csv"
    exact_srt = sessions_dir / f"{old_date}_100000.srt"
    for p in (plain_srt, plain_csv, exact_srt):
        p.write_text("content", encoding="utf-8")

    t = _create_test_translator(tmp_path, session_id="2026-01-01_000000")
    monkeypatch.setattr(config, "TRANSCRIPT_KEEP_DAYS", 30, raising=False)
    t._prune_old_transcripts()

    assert plain_srt.exists(), "纯日期 stem 不在会话命名规则里，不能删"
    assert plain_csv.exists(), "纯日期 stem 不在会话命名规则里，不能删"
    assert not exact_srt.exists(), "确切的会话命名仍应被正常清理"
