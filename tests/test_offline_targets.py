"""批次 2：请求模式 vs 检测结果、缓存校验、目标语言与字幕模式。

☠️ 全部用替身下载器/替身 ASR/替身翻译，**没有真实下载、没有真实模型、
没有 GPU**。这些用例证明的是管线接线和缓存判据，不能据此声称任何平台
（小红书等）已经验收。
"""
import json
from types import SimpleNamespace

import pytest

import realtime_subtitle.offline as offline
from realtime_subtitle.offline import build_srt


def _fake_pipeline(monkeypatch, tmp_path, detected="zh", text="你好"):
    """装好一整套替身，返回 (extracts, asr_args, translated) 三个记录列表。"""
    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=False):
            return {"id": "demo", "title": "Demo", "extractor": "youtube"}

    video = tmp_path / "demo.mp4"
    video.write_bytes(b"video")
    extracts, asr_args, translated = [], [], []
    monkeypatch.setattr(offline, "_load_yt_dlp",
                        lambda: SimpleNamespace(YoutubeDL=FakeYoutubeDL))
    monkeypatch.setattr(
        offline, "download_video",
        lambda url, job_dir, max_height, info=None: (
            {"id": "demo", "title": "Demo", "extractor": "youtube"}, video))
    monkeypatch.setattr(offline, "_extract_audio",
                        lambda video, audio: extracts.append(1))

    def fake_transcribe(audio, source_language):
        asr_args.append(source_language)
        return ([{"start": 0.0, "end": 1.0, "text": text}], detected, 1.0)

    monkeypatch.setattr(offline, "transcribe_audio", fake_transcribe)

    def fake_translate(rows, source, target, **kw):
        # 和真的 translate_segments 一样跳过已翻好的行（见
        # test_translate_resumes_completed_rows）：只记"真的调了模型"的那次，
        # 这样"命中缓存"和"重翻一遍"在断言里分得开
        todo = [r for r in rows if not (r.get("translation") or "").strip()]
        if todo:
            translated.append((source, target, len(todo)))
        for row in todo:
            row["translation"] = "<%s>%s" % (target, row["text"])

    monkeypatch.setattr(offline, "translate_segments", fake_translate)
    return extracts, asr_args, translated


# ======================================================================
# B04：用户请求的源语言模式 vs 实际检测到的语言
# ======================================================================


def test_auto_result_is_not_reused_for_forced_language(tmp_path, monkeypatch):
    """auto 检测出 de ≠ 用户强制 de：两次是不同的解码，必须重新识别。"""
    extracts, asr_args, _ = _fake_pipeline(monkeypatch, tmp_path,
                                           detected="de", text="Hallo")
    out = tmp_path / "out"

    first = offline.process_url("https://example.test/demo", out, summary=False)
    assert first["requested_source_language"] == "auto"
    assert first["detected_source_language"] == "de"

    second = offline.process_url("https://example.test/demo", out,
                                 source_language="de", summary=False)

    assert asr_args == ["auto", "de"], "强制 de 必须真的再跑一次 ASR"
    assert len(extracts) == 2
    assert second["requested_source_language"] == "de"


def test_same_request_mode_reuses_asr(tmp_path, monkeypatch):
    extracts, asr_args, _ = _fake_pipeline(monkeypatch, tmp_path,
                                           detected="de", text="Hallo")
    out = tmp_path / "out"

    offline.process_url("https://example.test/demo", out, summary=False)
    offline.process_url("https://example.test/demo", out, summary=False)

    assert asr_args == ["auto"], "同一套请求参数应当复用，不重跑 ASR"
    assert len(extracts) == 1


def test_asr_fingerprint_keys_on_request_not_detection(monkeypatch):
    from realtime_subtitle import config

    assert offline.asr_fingerprint("auto") != offline.asr_fingerprint("de")
    base = offline.asr_fingerprint("de")
    monkeypatch.setattr(config, "WHISPER_BEAM_SIZE", 99)
    assert offline.asr_fingerprint("de") != base


def test_v1_checkpoint_is_not_reusable():
    """旧 schema 没记请求模式，无从判断能不能复用 → 重新识别一次。"""
    legacy = {
        "version": 1, "video_id": "demo", "source_language": "de",
        "asr_done": True, "rows": [{"start": 0, "end": 1, "text": "Hallo"}],
        "asr_fingerprint": offline.asr_fingerprint("de"),
    }
    assert offline.checkpoint_asr_usable(legacy, "demo", "de") is False


# ======================================================================
# B05：缓存时间轴校验（坏缓存作废，不许悄悄夹紧）
# ======================================================================


@pytest.mark.parametrize("row, why", [
    ({"start": 9, "end": -1, "text": "坏"}, "反向时间轴"),
    ({"start": -3, "end": 5, "text": "坏"}, "负起点"),
    ({"start": 5, "end": 5, "text": "坏"}, "零长度"),
    ({"start": "x", "end": 5, "text": "坏"}, "非数字"),
    ({"start": 0, "end": "Infinity", "text": "坏"}, "无穷"),
])
def test_broken_timeline_invalidates_checkpoint(tmp_path, row, why):
    path = tmp_path / "ckpt.json"
    body = json.dumps({
        "version": offline.CHECKPOINT_VERSION, "video_id": "demo",
        "requested_source_language": "auto", "rows": [row], "asr_done": True,
    }).replace('"Infinity"', "Infinity")
    path.write_text(body, encoding="utf-8")

    assert offline.load_checkpoint(path) is None, why


@pytest.mark.parametrize("bad", ["abc", -5])
def test_broken_duration_invalidates_checkpoint(tmp_path, bad):
    path = tmp_path / "ckpt.json"
    path.write_text(json.dumps({
        "version": offline.CHECKPOINT_VERSION, "video_id": "demo",
        "requested_source_language": "auto", "duration": bad,
        "rows": [{"start": 0, "end": 1, "text": "ok"}], "asr_done": True,
    }), encoding="utf-8")

    assert offline.load_checkpoint(path) is None


def test_overlapping_subtitles_stay_valid(tmp_path):
    """合理字幕本来就会重叠（上一条没消失下一条已开始），不许一律禁止。"""
    path = tmp_path / "ckpt.json"
    path.write_text(json.dumps({
        "version": offline.CHECKPOINT_VERSION, "video_id": "demo",
        "requested_source_language": "auto", "duration": 10,
        "rows": [{"start": 0, "end": 3, "text": "a"},
                 {"start": 2, "end": 5, "text": "b"}],
        "asr_done": True,
    }), encoding="utf-8")

    assert offline.load_checkpoint(path) is not None


def test_bad_checkpoint_is_rejected_not_clamped(tmp_path, capsys):
    path = tmp_path / "ckpt.json"
    path.write_text(json.dumps({
        "version": offline.CHECKPOINT_VERSION, "video_id": "demo",
        "requested_source_language": "auto",
        "rows": [{"start": 9, "end": -1, "text": "坏"}], "asr_done": True,
    }), encoding="utf-8")

    assert offline.load_checkpoint(path) is None
    assert "缓存" in capsys.readouterr().err, "坏缓存要说清为什么坏"


# ======================================================================
# 目标语言与字幕模式
# ======================================================================


def test_chinese_video_defaults_to_english(tmp_path, monkeypatch):
    _fake_pipeline(monkeypatch, tmp_path, detected="zh", text="你好")

    result = offline.process_url("https://example.test/demo", tmp_path / "out",
                                 summary=False)

    assert result["target_language"] == "en"
    assert result["bilingual_srt"].name == "demo.zh-en.bilingual.srt"


def test_explicit_german_target_wins_over_detection(tmp_path, monkeypatch):
    _fake_pipeline(monkeypatch, tmp_path, detected="zh", text="你好")

    result = offline.process_url("https://example.test/demo", tmp_path / "out",
                                 summary=False, target_language="de")

    assert result["target_language"] == "de"
    assert "<de>你好" in result["bilingual_srt"].read_text(encoding="utf-8-sig")


def test_subtitle_modes_render_the_right_lines(tmp_path, monkeypatch):
    _fake_pipeline(monkeypatch, tmp_path, detected="zh", text="你好")

    result = offline.process_url("https://example.test/demo", tmp_path / "out",
                                 summary=False, subtitle_mode="target")

    target = result["target_srt"].read_text(encoding="utf-8-sig")
    bilingual = result["bilingual_srt"].read_text(encoding="utf-8-sig")
    source = result["source_srt"].read_text(encoding="utf-8-sig")

    assert "<en>你好" in target
    assert "你好\n<en>" not in target, "单语只要译文，不要原文那一行"
    assert "你好\n<en>你好" in bilingual
    assert "<en>" not in source
    assert result["subtitle"] == result["target_srt"]


def test_build_srt_modes():
    rows = [{"start": 0, "end": 1, "text": "你好", "translation": "Hello"}]

    assert "Hello" in build_srt(rows, mode="target")
    assert "你好" not in build_srt(rows, mode="target")
    assert build_srt(rows, mode="source") == build_srt(rows, bilingual=False)
    assert "你好\nHello" in build_srt(rows, mode="bilingual")
    # 缺译文时 target 模式退回原文，不产出空字幕块
    assert "你好" in build_srt([{"start": 0, "end": 1, "text": "你好"}], mode="target")


def test_switching_target_reuses_asr_and_keeps_both_translations(tmp_path, monkeypatch):
    extracts, asr_args, translated = _fake_pipeline(
        monkeypatch, tmp_path, detected="zh", text="你好")
    out = tmp_path / "out"

    english = offline.process_url("https://example.test/demo", out, summary=False)
    german = offline.process_url("https://example.test/demo", out, summary=False,
                                 target_language="de")
    back = offline.process_url("https://example.test/demo", out, summary=False,
                               target_language="en")

    assert asr_args == ["auto"], "换目标语言不该重跑 ASR"
    assert len(extracts) == 1
    # 英文翻一次、德文翻一次；切回英文命中缓存，不再翻第三次
    assert translated == [("zh", "en", 1), ("zh", "de", 1)]
    assert english["bilingual_srt"] != german["bilingual_srt"]
    assert english["bilingual_srt"].is_file() and german["bilingual_srt"].is_file()
    assert "<en>你好" in back["bilingual_srt"].read_text(encoding="utf-8-sig")


def test_switching_mode_does_not_retranslate(tmp_path, monkeypatch):
    _, _, translated = _fake_pipeline(monkeypatch, tmp_path, detected="zh", text="你好")
    out = tmp_path / "out"

    offline.process_url("https://example.test/demo", out, summary=False,
                        subtitle_mode="bilingual")
    offline.process_url("https://example.test/demo", out, summary=False,
                        subtitle_mode="target")

    assert len(translated) == 1, "字幕模式只影响渲染，不该进缓存身份"


def test_same_source_and_target_skips_translation(tmp_path, monkeypatch):
    _, _, translated = _fake_pipeline(monkeypatch, tmp_path, detected="zh", text="你好")

    result = offline.process_url("https://example.test/demo", tmp_path / "out",
                                 summary=False, target_language="zh")

    assert translated == [], "源和目标相同就没什么可翻的"
    # 没有译文就没有译文/双语那两份成果——返回值里不许指向不存在的文件
    assert result["bilingual_srt"] is None
    assert result["target_srt"] is None
    assert result["subtitle"] == result["source_srt"]
    body = result["source_srt"].read_text(encoding="utf-8-sig")
    assert body.count("你好") == 1, "同语种不该重复两行"


# ======================================================================
# 非法参数在下载之前就被拒
# ======================================================================


@pytest.mark.parametrize("kwargs", [
    {"target_language": "英语"},
    {"target_language": "e"},
    {"subtitle_mode": "monolingual"},
    {"source_language": "zh_CN!"},
])
def test_bad_arguments_fail_before_downloading(tmp_path, monkeypatch, kwargs):
    def boom(*args, **kw):
        raise AssertionError("非法参数不该走到解析/下载这一步")

    monkeypatch.setattr(offline, "_load_yt_dlp", boom)
    monkeypatch.setattr(offline, "download_video", boom)

    with pytest.raises(offline.OfflineSubtitleError):
        offline.process_url("https://example.test/demo", tmp_path / "out", **kwargs)


def test_cli_passes_target_and_mode_through(monkeypatch):
    seen = {}
    monkeypatch.setattr(offline, "process_input",
                        lambda *a, **kw: seen.update(args=a, kwargs=kw))

    assert offline.main([
        "https://example.test/demo", "--target-language", "de",
        "--subtitle-mode", "target", "--no-summary",
    ]) == 0

    assert seen["kwargs"]["target_language"] == "de"
    assert seen["kwargs"]["subtitle_mode"] == "target"


def test_cli_rejects_unknown_mode():
    with pytest.raises(SystemExit):
        offline.main(["https://example.test/demo", "--subtitle-mode", "monolingual"])


# ======================================================================
# PowerShell 入口（静态检查，不启动 PowerShell）
# ======================================================================


def _download_ps1():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    return (root / "scripts" / "windows" / "download_subtitle.ps1").read_text(
        encoding="utf-8-sig")


def test_ps1_offers_mode_and_target_and_passes_them_as_args():
    text = _download_ps1()

    for flag in ("--target-language", "--subtitle-mode", "--source-language"):
        assert flag in text, flag
    assert "单语（只要译文）" in text and "双语" in text
    assert "英语" in text and "德语" in text
    # 选项编号不能直接当语言码：菜单里的 "2" 要映射成 de
    assert '"2" { $TargetLanguage = "de" }' in text


def test_ps1_never_builds_a_shell_string():
    """地址来自剪贴板，只能走参数数组，不许拼命令行。"""
    text = _download_ps1()
    # 只看代码，不看注释——注释里正要写清楚为什么不许用它
    code = "\n".join(line for line in text.splitlines()
                     if not line.lstrip().startswith("#"))

    assert "Invoke-Expression" not in code
    assert "@CliArgs" in code
    assert "&&" not in code, "Windows PowerShell 5.1 不认 &&"


def test_ps1_asks_when_the_clipboard_has_several_links():
    text = _download_ps1()

    assert "Get-UrlChoice" in text
    assert "多个链接" in text
